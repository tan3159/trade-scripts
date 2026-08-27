"""`tidd extract-feature` サブコマンド（Issue #1283）.

GitHub Issue の ``## 振る舞い`` セクションを読み取り、pytest-bdd 互換の
``.feature`` ファイルとして出力する。既定の出力先は
``projects/py/<project>/tests/features/issue-<N>.feature``（Issue #2258:
``projects/py/`` 配下のプロジェクト構成から自動解決する。詳細は
``_resolve_default_project_root`` を参照）。

**使い方:**

.. code-block:: console

    tidd extract-feature 1234
    # → projects/py/<project>/tests/features/issue-1234.feature 生成
    # （projects/py/tidd_tools が存在する場合はそこに、単一プロジェクト構成の
    #   consumer では自動検出したプロジェクトに出力する）

**設計判断:**

- Issue 本文は ``gh issue view <N> --json body -q .body`` で取得する
- ``## 振る舞い`` セクションのみを抽出。以外は捨てる
- pytest-bdd は "Feature:" / "Scenario:" / Given/When/Then/And/But をそのまま解釈するため
  Markdown のコードブロック外の Gherkin 記法をコピーするだけで良い
- Feature 見出しが検出できない場合は Issue 番号ベースで補完する
- 既定出力先は `projects/py/tidd_tools` があればそれを優先し、なければ
  `projects/py/` 配下の単一プロジェクトを自動選択する（複数存在する場合は
  `--out-dir` / `--step-defs-dir` の明示指定を要求する。Issue #2258）

stdlib のみ使用。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from tidd_tools.shared import gh_client
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.subprocess_runner import run as run_subprocess

_BEHAVIOR_HEADER_RE = re.compile(r"^##\s*振る舞い\s*$", re.MULTILINE)
_FEATURE_LINE_RE = re.compile(r"^\s*Feature:", re.MULTILINE)
_SCENARIO_LINE_RE = re.compile(r"^\s*Scenario:", re.MULTILINE)

# Issue #2890: Issue本文で Gherkin キーワードが Markdown 見出し（`### Scenario: ...` 等）
# として書かれている場合に、通常の `Scenario: ...` 行へ正規化するための regex。
_MARKDOWN_HEADING_KEYWORD_RE = re.compile(
    r"^#{1,6}[ \t]*(Feature|Scenario Outline|Scenario|Background|Examples):",
    re.MULTILINE,
)

# Issue #1473: Gherkin schema validation
_GHERKIN_STEP_RE = re.compile(r"^\s*(Given|When|Then|And|But)\s+", re.MULTILINE)

# Issue #1545: step_defs skeleton 生成用
_STEP_LINE_RE = re.compile(r"^\s*(Given|When|Then|And|But)\s+(.+?)\s*$", re.MULTILINE)


def validate_gherkin_schema(feature_content: str) -> list[str]:
    """Issue #1473: Gherkin の基本 schema を検証する.

    非エンジニア向け（監査 §6-7）に「読める」エラーメッセージを返す。

    現在の検証項目:
    - 各 Scenario が最低 1 つの Given, When, Then を含む
      （When 句なし Scenario は Then への遷移が観測できない）
    - Given / When / Then / And / But 以外の非空行が混入していない
    - Given が 3 回以上連続していない (agy 誤配置の警告)

    Returns:
        エラーメッセージのリスト（空なら valid）
    """
    errors: list[str] = []
    scenarios = re.split(r"^\s*Scenario:", feature_content, flags=re.MULTILINE)
    if len(scenarios) <= 1:
        return ["Gherkin schema エラー: Scenario がありません"]

    for i, sc in enumerate(scenarios[1:], start=1):
        steps: list[str] = []
        lines = sc.split("\n")
        # 1 行目は Scenario 名（例: "When なし" のように step 名を含んでも scenario name として扱う）
        for line in lines[1:]:
            m = _GHERKIN_STEP_RE.match(line)
            if m:
                steps.append(m.group(1))

        if not steps:
            errors.append(
                f"Scenario {i}: Gherkin schema エラー: step が 1 つもありません "
                "(Given / When / Then を最低 1 つずつ書いてください)"
            )
            continue

        # Given / When / Then の存在チェック（And / But はカウントに含めない・
        # ただし直前 step の種別を継承する既存慣行を尊重）
        # 継承した step 判定: And/But は直前の Given/When/Then を継承する
        primary_types: set[str] = set()
        current_primary: str | None = None
        for s in steps:
            if s in ("Given", "When", "Then"):
                current_primary = s
                primary_types.add(s)
            elif s in ("And", "But") and current_primary:
                primary_types.add(current_primary)

        if "When" not in primary_types:
            errors.append(
                f"Scenario {i}: Gherkin schema エラー: Scenario には When 句が必要です "
                "(操作を明示しない Scenario は Then への遷移が観測不能)"
            )
        if "Then" not in primary_types:
            errors.append(
                f"Scenario {i}: Gherkin schema エラー: Scenario には Then 句が必要です (期待結果を書いてください)"
            )

        # Given / When / Then が 3 回以上連続しているかチェック（誤配置検知）
        for target in ("Given", "When", "Then"):
            consecutive = 0
            max_consecutive = 0
            for s in steps:
                if s == target:
                    consecutive += 1
                    max_consecutive = max(max_consecutive, consecutive)
                else:
                    consecutive = 0
            if max_consecutive >= 3:
                errors.append(
                    f"Scenario {i}: Gherkin schema エラー: {target} が連続しています "
                    f"({max_consecutive} 回)。連続する前提は `And` を使ってください "
                    f"(例: Given foo / And bar / And baz)"
                )
    return errors


def _find_repo_root() -> Path:
    """リポジトリルートを返す（Issue #2224）.

    `__file__` 起点は uv tool install（site-packages 配下）でクラッシュするため、
    CWD 優先の `shared.paths.resolve_repo_root` に統一する。
    """
    from tidd_tools.shared.paths import resolve_repo_root

    return resolve_repo_root()


def _resolve_default_project_root(repo_root: Path) -> Path | None:
    """既定出力先のベースとなる `projects/py/<project>` ディレクトリを解決する（Issue #2258）.

    - `projects/py/tidd_tools` が存在する場合（handbook 自身）はそれを優先して返す
    - それ以外は `projects/py/` 配下の単一ディレクトリを自動選択する
    - `projects/py/` が存在しない、または複数ディレクトリが存在し一意に決まらない場合は
      `None` を返す（呼び出し側で `--out-dir` / `--step-defs-dir` の明示指定を要求する）
    """
    py_dir = repo_root / "projects" / "py"
    tidd_tools_dir = py_dir / "tidd_tools"
    if tidd_tools_dir.is_dir():
        return tidd_tools_dir
    if not py_dir.is_dir():
        return None
    candidates = sorted(p for p in py_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    if len(candidates) == 1:
        return candidates[0]
    return None


def _fetch_issue_body(issue_number: str, repo: str | None) -> str | None:
    """gh issue view で Issue 本文を取得する.

    テスト用に ``EXTRACT_FEATURE_TEST_BODY`` 環境変数で override 可能。
    """
    override = os.environ.get("EXTRACT_FEATURE_TEST_BODY")
    if override is not None:
        return override
    try:
        data = gh_client.issue_view(issue_number, repo=repo, fields=("body",))
    except GhCommandError:
        return None
    body = data.get("body")
    return body if isinstance(body, str) else None


def _extract_behavior_section(body: str) -> str | None:
    """Issue 本文から ``## 振る舞い`` セクション（次の ``## `` まで）を抽出する."""
    match = _BEHAVIOR_HEADER_RE.search(body)
    if not match:
        return None
    start = match.end()
    # 次の ## ヘッダを探す
    next_header = re.search(r"^##\s+", body[start:], re.MULTILINE)
    section = body[start : start + next_header.start()] if next_header else body[start:]
    return section.strip("\n")


def _strip_markdown_heading_hashes(text: str) -> str:
    """Markdown 見出し（`### Scenario: ...` 等）の ``#`` プレフィックスを取り除く（Issue #2890）.

    issue-creation-guide.md の合格例のように Gherkin キーワードが Markdown 見出し
    （`### Scenario: ...`）として書かれる Issue 本文に対応する。Gherkin パーサは
    ``#`` プレフィックス付きの行をキーワードとして認識しないため、通常の
    ``Scenario: ...`` 行に正規化してから後続処理（Scenario 存在チェック・
    .feature 生成）に渡す。
    """
    return _MARKDOWN_HEADING_KEYWORD_RE.sub(lambda m: f"{m.group(1)}:", text)


def _normalize_gherkin_indentation(content: str) -> str:
    """.gherkin-lintrc 準拠のインデントに正規化する（Issue #1847）.

    - Feature: 0 スペース（変更しない）
    - Scenario: / Background: / Examples: → 2 スペース
    - Given / When / Then / And / But → 4 スペース
    """
    _scenario_level = ("Scenario:", "Scenario Outline:", "Background:", "Examples:")
    _step_level = ("Given ", "When ", "Then ", "And ", "But ")

    lines = content.splitlines(keepends=True)
    result = []
    for line in lines:
        stripped = line.lstrip()
        # Scenario-level → 2 spaces
        for kw in _scenario_level:
            if stripped.startswith(kw):
                line = "  " + stripped
                break
        else:
            # Step-level → 4 spaces
            for kw in _step_level:
                if stripped.startswith(kw):
                    line = "    " + stripped
                    break
        result.append(line)
    return "".join(result)


def _to_feature_content(section: str, issue_number: str) -> str:
    """`## 振る舞い` セクションの Gherkin テキストを .feature ファイル形式に整形する.

    - Feature: 行がなければ Issue 番号ベースで補う
    - Markdown 見出し（`### Scenario: ...` 等）を通常の Gherkin 行に正規化する（Issue #2890）
    - コードフェンス（```gherkin ... ```）があれば内側だけ取り出す
    - .gherkin-lintrc 準拠のインデント（Scenario: 2sp, Step: 4sp）に正規化する
    """
    # Issue #2890: 正規化前に Markdown 見出し（### Scenario: 等）の有無を判定しておく。
    # 見出しはフェンス外に書かれる（issue-creation-guide.md の合格例フォーマット）ため、
    # 単一フェンスの内側だけを取り出す従来ロジックだと見出し・後続 Scenario を失う。
    has_markdown_heading = bool(_MARKDOWN_HEADING_KEYWORD_RE.search(section))
    section = _strip_markdown_heading_hashes(section)

    # コードフェンス除去（Issue #1972）
    if has_markdown_heading:
        # Issue #2890: 見出し + 個別 ```gherkin ...``` フェンスの合格例フォーマット。
        # フェンス区切り行のみを取り除き、周辺テキスト（見出し等）は保持する。
        section = "\n".join(line for line in section.splitlines() if not line.strip().startswith("```"))
    else:
        # 閉じフェンスが存在する場合: DOTALL マッチで内側のテキストのみを取り出す
        fence_match = re.search(r"```(?:gherkin|feature)?\s*(.*?)\s*```", section, re.DOTALL)
        if fence_match:
            section = fence_match.group(1)
        else:
            # 閉じフェンスが存在しない場合（## やること ヘッダーで切り取られた等）:
            # 行ベースでフェンス行（``` で始まる行）を除去する
            section = "\n".join(line for line in section.splitlines() if not line.strip().startswith("```"))
    section = section.strip()
    if not _FEATURE_LINE_RE.search(section):
        section = f"Feature: Issue #{issue_number}\n\n{section}"
    # .gherkin-lintrc 準拠のインデントに正規化する
    section = _normalize_gherkin_indentation(section)
    # 末尾改行を保証
    if not section.endswith("\n"):
        section += "\n"
    return section


def _parse_steps_with_primary_type(feature_content: str) -> list[tuple[str, str]]:
    """Issue #1545: .feature 内容から (primary_type, step_text) のリストを返す.

    - primary_type は "given" / "when" / "then" のいずれか（decorator 名）
    - And / But は直前の Given/When/Then を継承する
    - 出現順を保持しつつ (primary_type, step_text) の重複は除去する
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    current_primary: str | None = None
    for match in _STEP_LINE_RE.finditer(feature_content):
        kind = match.group(1)
        text = match.group(2).strip()
        if kind in ("Given", "When", "Then"):
            current_primary = kind.lower()
        elif kind in ("And", "But"):
            if current_primary is None:
                continue  # 継承元がない And / But は無視（無効な Gherkin）
        else:
            continue
        assert current_primary is not None
        key = (current_primary, text)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _generate_step_defs_skeleton(feature_content: str, issue_number: str) -> str:
    """Issue #1545: `.feature` 内容から pytest-bdd step_defs skeleton を生成する.

    各 step は pending marker（pytest.xfail）で pending 状態にし、実装完了後に
    ヒトが marker を消して真の実装に置き換える。

    生成物は Python として compile 可能で、pytest-bdd の scenarios() で
    `../features/issue-<N>.feature` を宣言する。
    """
    steps = _parse_steps_with_primary_type(feature_content)
    pending_call = f'pytest.xfail("step_defs 未実装: Issue #{issue_number}")'
    lines: list[str] = []
    lines.append(f'"""Issue #{issue_number} の step_defs skeleton (`tidd extract-feature` 自動生成).')
    lines.append("")
    lines.append("各 step は pytest 標準の pending marker で保留状態。実装完了時に marker を")
    lines.append("消して真の実装に置き換える。詳細: docs/reference/pytest-bdd-workflow.md")
    lines.append('"""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("import pytest")
    lines.append("from pytest_bdd import given, scenarios, then, when")
    lines.append("")
    lines.append(f'scenarios("../features/issue-{issue_number}.feature")')
    lines.append("")
    for idx, (primary, text) in enumerate(steps):
        # decorator 引数として syntax エラーにならないよう二重引用符をエスケープ
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        # Issue #1747: 関数定義間は 2 行空行（ruff/pep8 の top-level 間隔要件）
        lines.append("")
        lines.append("")
        lines.append(f'@{primary}("{escaped}")')
        lines.append(f"def _step_{primary}_{idx}() -> None:")
        lines.append(f"    {pending_call}")
    raw = "\n".join(lines) + "\n"
    # Issue #1747: 行長分割・quote 種類・空行数の最終正規化は ruff に委ねる。
    # ruff 未導入環境では未整形のまま返す（機能自体は生成物のとおり動くため graceful degradation）。
    return _ruff_format_stdin(raw)


def _ruff_format_stdin(source: str) -> str:
    """`ruff format -` に stdin で流して正規化済みの source を返す（Issue #1747）.

    ruff が呼べない/失敗する場合は入力をそのまま返す（extract-feature の主機能を
    ruff の可用性に依存させないため）。
    """
    try:
        result = run_subprocess(
            [sys.executable, "-m", "ruff", "format", "-"],
            input=source,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return source
    if result.returncode != 0 or not result.stdout:
        return source
    return result.stdout


def extract_feature(
    issue_number: str,
    out_dir: Path,
    *,
    repo: str | None = None,
    strict: bool = False,
    step_defs_dir: Path | None = None,
    force: bool = False,
) -> tuple[int, str]:
    """Issue の振る舞いセクションを ``.feature`` ファイルに出力する.

    Issue #1473: ``strict=True`` で gherkin schema check を実行し、失敗時は exit 1。
    Issue #1545: ``step_defs_dir`` を指定すると同時に step_defs skeleton を生成する。
        既存 step_defs は上書きせず skip する。``force=True`` で上書き。

    Returns:
        (exit_code, message) タプル
    """
    body = _fetch_issue_body(issue_number, repo)
    if body is None:
        return 1, f"ERROR: Issue #{issue_number} の本文取得に失敗しました"

    section = _extract_behavior_section(body)
    if section is None or not section.strip():
        return 1, f"Issue #{issue_number} に ## 振る舞い セクションがありません"

    # Issue #2890: Markdown 見出し形式（### Scenario: 等）にも対応するため、存在チェックは
    # 正規化した文字列で行う。`section` 自体は正規化前のまま `_to_feature_content` に渡し、
    # 見出しがフェンス外にあるかどうかの判定を委ねる（フェンス抽出戦略の分岐に必要）。
    if not _SCENARIO_LINE_RE.search(_strip_markdown_heading_hashes(section)):
        return 1, f"Issue #{issue_number} の ## 振る舞い セクションに Scenario がありません"

    feature_content = _to_feature_content(section, issue_number)

    # Issue #1473: strict モードで Gherkin schema check を実行
    if strict:
        schema_errors = validate_gherkin_schema(feature_content)
        if schema_errors:
            return 1, (
                f"ERROR: Issue #{issue_number} の Gherkin が schema 違反です:\n"
                + "\n".join(f"  {e}" for e in schema_errors)
                + "\n\n  ヒント: pytest-bdd は Given (前提) → When (操作) → Then (結果) の"
                "3 段構成を要求します。Issue 本文の ## 振る舞い セクションを見直してください。"
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"issue-{issue_number}.feature"
    out_path.write_text(feature_content, encoding="utf-8")

    # Issue #1545: step_defs skeleton の生成
    if step_defs_dir is not None:
        step_defs_dir.mkdir(parents=True, exist_ok=True)
        skeleton_path = step_defs_dir / f"test_issue_{issue_number}.py"
        if skeleton_path.exists() and not force:
            print(
                f"step_defs は既存のため skip: {skeleton_path.name} (--force で上書き可)",
                file=sys.stderr,
            )
        else:
            skeleton = _generate_step_defs_skeleton(feature_content, issue_number)
            skeleton_path.write_text(skeleton, encoding="utf-8")

    return 0, str(out_path)


def run_cli(args: argparse.Namespace) -> int:
    issue_number = str(args.issue_number)
    if not issue_number.isdigit():
        print(f"ERROR: Issue 番号は数字である必要があります: {issue_number}", file=sys.stderr)
        return 1
    repo = os.environ.get("REPO")
    repo_root = _find_repo_root()

    # Issue #2258: --out-dir / --step-defs-dir 省略時のみ既定プロジェクトの解決が必要。
    needs_default_project = not args.out_dir or (not args.step_defs_dir and not args.no_step_defs)
    default_project_root = _resolve_default_project_root(repo_root) if needs_default_project else None
    if needs_default_project and default_project_root is None:
        print(
            f"ERROR: {repo_root / 'projects' / 'py'} 配下のプロジェクトを一意に特定できません "
            "(0 件または複数件検出)。--out-dir / --step-defs-dir で出力先を明示指定してください",
            file=sys.stderr,
        )
        return 1

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        assert default_project_root is not None  # needs_default_project により保証済み
        out_dir = default_project_root / "tests" / "features"
    if args.step_defs_dir:
        step_defs_dir: Path | None = Path(args.step_defs_dir)
    elif args.no_step_defs:
        step_defs_dir = None
    else:
        assert default_project_root is not None  # needs_default_project により保証済み
        step_defs_dir = default_project_root / "tests" / "step_defs"
    strict = bool(getattr(args, "strict", False))
    force = bool(getattr(args, "force", False))
    code, message = extract_feature(
        issue_number,
        out_dir,
        repo=repo,
        strict=strict,
        step_defs_dir=step_defs_dir,
        force=force,
    )
    if code == 0:
        print(message)
    else:
        print(message, file=sys.stderr)
    return code


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "extract-feature",
        help="Issue の ## 振る舞い セクションを .feature ファイルに変換する",
        description=("GitHub Issue の ## 振る舞い セクションを pytest-bdd 互換の .feature ファイルとして出力する。"),
    )
    parser.add_argument("issue_number", help="対象 Issue 番号（例: 1234）")
    parser.add_argument(
        "--out-dir",
        default=None,
        help=(
            "出力先ディレクトリ（既定: projects/py/tidd_tools があればそこ、"
            "なければ projects/py/ 配下の単一プロジェクトを自動解決。Issue #2258）"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Issue #1473: Gherkin schema check を実行し、違反時は exit 1 で終了する",
    )
    parser.add_argument(
        "--step-defs-dir",
        default=None,
        help=(
            "Issue #1545: step_defs skeleton の出力先"
            "（既定: --out-dir と同じプロジェクトの tests/step_defs/。Issue #2258）"
        ),
    )
    parser.add_argument(
        "--no-step-defs",
        action="store_true",
        help="Issue #1545: step_defs skeleton を生成しない（.feature のみ）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Issue #1545: 既存 step_defs skeleton を上書きする",
    )
    parser.set_defaults(func=run_cli)
