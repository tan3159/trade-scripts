"""tidd audit-mutmut-review: mutmut 生存 mutant と AI review verdict を相関する（Issue #1300）.

mutmut の週次結果から survived mutant のファイル・行番号を抽出し、`git blame` で
該当行の最終変更 PR を特定して、当該 PR の ai-review verdict が APPROVE だった場合に
「AI review 見逃し疑い」として GitHub Issue を自動起票する。

**入力フォーマット:**

以下いずれかの JSON ファイル形式を受け付ける（`--results-file` で指定・省略時は
`mutants/mutmut-mutants.json` を探す）:

1. **フラット形式:** ``[{"status": "survived", "file": "path", "line": N}, ...]``
2. **mutmut aggregated stats:** ``{"total": N, "killed": N, "survived": N, ...}``
   → aggregated 情報のみで per-mutant がないため、`mutants/` ディレクトリの
   `mutmut-results.txt` を並行で読む（`--results-text` で指定）

**重複起票防止:** 同じ PR に対する Issue が OPEN 状態で既にあれば新規起票しない。

**Usage:**

    tidd audit-mutmut-review --results-file /path/to/mutants.json [--dry-run]

CircleCI 週次 job から:

    uv run --project projects/py/tidd_tools python -m tidd_tools audit-mutmut-review \\
        --results-file mutants/mutmut-mutants.json

stdlib のみ使用（gh CLI を subprocess 経由で呼ぶ）。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# mutmut results テキスト形式のパターン。例:
#   "src/tidd_tools/foo.py::mutant_name (survived)"
#   "survived (35): src/tidd_tools/foo.py:42"
_RESULTS_TEXT_PATTERN = re.compile(
    r"(?P<status>survived|timeout|suspicious)[\s:(]+.*?(?P<file>[\w/.\-]+\.py):(?P<line>\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SurvivedMutant:
    """個別の survived mutant（file:line で識別）."""

    file: str
    line: int
    status: str = "survived"


def parse_json_report(path: Path) -> list[SurvivedMutant]:
    """フラット JSON 形式から survived mutants を抽出する.

    許容フォーマット:
        [{"status": "survived", "file": "path", "line": N}, ...]
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("JSON parse 失敗: %s (%s)", path, exc)
        return []
    if not isinstance(data, list):
        return []
    result: list[SurvivedMutant] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).lower()
        if status != "survived":
            continue
        file_ = item.get("file")
        line = item.get("line")
        if not isinstance(file_, str) or not isinstance(line, int):
            continue
        result.append(SurvivedMutant(file=file_, line=line, status=status))
    return result


def parse_results_text(path: Path) -> list[SurvivedMutant]:
    """mutmut results テキスト形式から survived mutants を抽出する.

    形式は mutmut 3.x の text output を想定するが、file:line が読める限り
    どの形式でも拾う（正規表現でベストエフォート）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("results text 読み込み失敗: %s (%s)", path, exc)
        return []
    result: list[SurvivedMutant] = []
    for match in _RESULTS_TEXT_PATTERN.finditer(text):
        status = match.group("status").lower()
        if status != "survived":
            continue
        result.append(
            SurvivedMutant(
                file=match.group("file"),
                line=int(match.group("line")),
                status=status,
            )
        )
    return result


def _resolve_repo_path(file: str, repo_root: Path) -> str | None:
    """mutmut が返すパスを repo_root からの相対 path に解決する.

    mutmut のパスは以下いずれかで返される場合がある:
    - repo_root 相対: `projects/py/tidd_tools/src/tidd_tools/verdict.py`
    - サブプロジェクト相対: `src/tidd_tools/verdict.py`（`projects/py/tidd_tools/` から実行時）
    - basename のみ: `verdict.py`

    repo_root からの存在チェックで解決できたパスを返す。見つからなければ None。
    """
    # そのまま試す
    if (repo_root / file).is_file():
        return file
    # 既知のサブプロジェクト prefix を付ける
    for prefix in ("projects/py/tidd_tools/", "projects/py/publish/", "projects/py/example/"):
        candidate = prefix + file
        if (repo_root / candidate).is_file():
            return candidate
    # 見つからない
    return None


def find_last_pr_for_line(file: str, line: int, repo_root: Path) -> int | None:
    """`git blame` で該当行の最終変更 commit を特定し、その commit を含む PR 番号を返す.

    ``git blame -L <line>,<line> -- <file>`` → commit sha 取得 →
    ``gh api /repos/{owner}/{repo}/commits/{sha}/pulls`` → PR 番号取得

    mutmut のパスは repo_root 相対でない場合があるため、`_resolve_repo_path` で解決する
    （Issue #1300 レビュー指摘）。
    """
    resolved = _resolve_repo_path(file, repo_root)
    if resolved is None:
        logger.debug("path 解決失敗: %s (repo_root=%s)", file, repo_root)
        return None
    try:
        # `--` でオプションインジェクション対策（file 名がハイフン始まりでも安全）
        blame_proc = subprocess.run(  # noqa: S603
            ["git", "blame", "-L", f"{line},{line}", "--porcelain", "--", resolved],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("git blame 失敗: %s", exc)
        return None
    if blame_proc.returncode != 0 or not blame_proc.stdout:
        return None
    # porcelain 出力の先頭行が "<sha> ..." の形式
    first_line = blame_proc.stdout.splitlines()[0] if blame_proc.stdout else ""
    parts = first_line.split()
    if not parts:
        return None
    sha = parts[0]
    if not re.match(r"^[0-9a-f]{7,40}$", sha):
        return None

    # gh api で commit を含む PR を検索する
    try:
        pr_proc = subprocess.run(  # noqa: S603
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/commits/{sha}/pulls",
                "-q",
                ".[0].number",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("gh api 失敗: %s", exc)
        return None
    if pr_proc.returncode != 0 or not pr_proc.stdout.strip():
        return None
    try:
        return int(pr_proc.stdout.strip())
    except ValueError:
        return None


def get_ai_review_verdict(pr_num: int) -> str | None:
    """PR の ai-review verdict を bot コメントから抽出する.

    ai-review は "VERDICT: APPROVE" または "VERDICT: REQUEST_CHANGES" を含む
    コメントを投稿する。最新のものを取り出す。
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [
                "gh",
                "pr",
                "view",
                str(pr_num),
                "--json",
                "comments",
                "-q",
                ".comments[] | .body",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    verdict_re = re.compile(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)")
    last_verdict: str | None = None
    for line in proc.stdout.splitlines():
        m = verdict_re.search(line)
        if m:
            last_verdict = m.group(1)
    return last_verdict


def has_open_dedup_issue(pr_num: int) -> bool:
    """同じ PR に対する OPEN Issue が既にあるか確認する（重複起票防止）.

    検索クエリはコロンを含むため GitHub search parser の誤判定を避けるため
    フレーズを ``"..."`` で囲む（Issue #1300 レビュー指摘）。
    """
    # PR 番号だけでフィルタして title / body を含む OPEN Issue を検索する。
    # コロン混じりのフレーズは search parser で誤判定されるので avoid する。
    search_query = f'"PR #{pr_num}" in:title'
    try:
        proc = subprocess.run(  # noqa: S603
            [
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--search",
                search_query,
                "--json",
                "number,title",
                "-q",
                # audit 起票 title は "AI review 見逃し疑い: PR #<N>" 形式なので二段フィルタ
                f'.[] | select(.title | contains("AI review 見逃し疑い") and contains("PR #{pr_num}")) | .number',
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return bool(proc.stdout.strip())


class GhSubprocessError(RuntimeError):
    """Issue #1453: gh subprocess の非ゼロ終了・実行不可を fail-loud で伝播する例外.

    旧 `create_miss_issue()` は `logger.warning` のみで `return None` していたため、
    mutmut 生存 mutant が検出されているのに Issue 起票 silent skip → CI job 緑のまま
    mutation score 悪化を見過ごすリスクがあった（監査 §2-3）。
    本例外を `run()` エントリポイントで捕捉して exit 1 に昇格させる。
    """


def create_miss_issue(pr_num: int, mutants: list[SurvivedMutant]) -> int | None:
    """「AI review 見逃し疑い」Issue を起票する。起票された Issue 番号を返す.

    Issue #1453: gh subprocess の非ゼロ終了 / 実行不可を `GhSubprocessError` として raise する。
    exit code 4（authentication required）と他の非ゼロは stderr に区別できる文字列を含めて
    raise することで CircleCI job step が failed 状態になる。
    """
    if not mutants:
        return None
    title = f"🤖 fix: AI review 見逃し疑い: PR #{pr_num} の {mutants[0].file}:{mutants[0].line}"
    mutant_lines = "\n".join(f"- `{m.file}:{m.line}` ({m.status})" for m in mutants[:20])
    body = (
        "## 背景\n\n"
        f"mutmut 週次実行で生存した mutant が PR #{pr_num} の変更範囲に紐づいており、"
        f"当該 PR の AI review verdict が `APPROVE` だった。"
        "テストが弱く AI review も見逃した可能性がある（監査 §2-5 H 案・Issue #1300 で自動起票）。\n\n"
        f"**Pain:** テスト強度不足の箇所を AI review が誤 APPROVE していた恐れがある。\n\n"
        f"**壁打ち起点:** Issue #1300 の自動起票\n\n"
        "## やること\n\n"
        f"- [ ] PR #{pr_num} の変更内容を確認する\n"
        "- [ ] 生存 mutant の位置に対応するテストを追加または強化する\n"
        "- [ ] mutmut を再実行して当該 mutant が killed になることを確認する\n"
        "- [ ] AI review のプロンプト・チェック観点にフィードバックがあれば `.claude/rules/` に反映する\n\n"
        "## 参照\n\n"
        f"- PR #{pr_num}: 変更元\n"
        "- Issue #1286: mutmut 週次導入\n"
        "- Issue #1300: 本自動起票の実装 Issue\n"
        "- `docs/reference/mutmut-review-correlation.md`\n\n"
        "## 生存 mutant 一覧\n\n"
        f"{mutant_lines}\n"
    )
    try:
        proc = subprocess.run(  # noqa: S603
            [
                "gh",
                "issue",
                "create",
                "--title",
                title,
                "--body",
                body,
                "--label",
                "type: fix,priority: medium",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GhSubprocessError(f"gh subprocess failed: {exc}") from exc
    if proc.returncode != 0:
        stderr_snippet = (proc.stderr or "")[:400]
        if proc.returncode == 4:
            # gh CLI exit code 4: authentication required
            raise GhSubprocessError(f"gh subprocess failed: authentication failed (exit code 4): {stderr_snippet}")
        raise GhSubprocessError(f"gh subprocess failed: non-zero exit code {proc.returncode}: {stderr_snippet}")
    # gh issue create の出力は URL。末尾の数字を抜く。
    m = re.search(r"/issues/(\d+)", proc.stdout)
    return int(m.group(1)) if m else None


def run(
    *,
    results_file: Path | None,
    results_text: Path | None,
    repo_root: Path,
    dry_run: bool,
) -> int:
    """audit-mutmut-review のメインフロー."""
    mutants: list[SurvivedMutant] = []
    if results_file and results_file.is_file():
        mutants.extend(parse_json_report(results_file))
    if results_text and results_text.is_file():
        mutants.extend(parse_results_text(results_text))
    if not mutants:
        print(
            "no survived mutants found in results (nothing to audit)",
            file=sys.stderr,
        )
        return 0

    # file:line で dedup（同じ mutant が複数フォーマットから読み込まれた場合）
    unique_mutants = list({(m.file, m.line): m for m in mutants}.values())

    # file:line ごとに PR を特定
    pr_to_mutants: dict[int, list[SurvivedMutant]] = {}
    for mutant in unique_mutants:
        pr_num = find_last_pr_for_line(mutant.file, mutant.line, repo_root)
        if pr_num is None:
            continue
        pr_to_mutants.setdefault(pr_num, []).append(mutant)

    if not pr_to_mutants:
        print("no PRs identified for survived mutants", file=sys.stderr)
        return 0

    # PR ごとに verdict 確認 → APPROVE なら Issue 起票
    misses_found = 0
    gh_errors = 0  # Issue #1453: gh subprocess 失敗を集計して exit 1 に昇格
    for pr_num, ml in sorted(pr_to_mutants.items()):
        verdict = get_ai_review_verdict(pr_num)
        if verdict != "APPROVE":
            # Issue #1300 Gherkin Scenario 2: 「stdout に "no misses detected" 相当が出力される」に沿って
            # 判定結果は stdout に、進捗ログは stderr に分離する。
            print(f"PR #{pr_num}: verdict={verdict} (skip)")
            continue

        # 重複起票チェック
        if has_open_dedup_issue(pr_num):
            # Gherkin Scenario 3: 「stdout に "existing open issue for PR #NNNN" が出力される」
            print(f"existing open issue for PR #{pr_num}")
            continue

        if dry_run:
            print(f"[dry-run] would create Issue for PR #{pr_num} ({len(ml)} survived mutants)")
            misses_found += 1
            continue

        try:
            issue_num = create_miss_issue(pr_num, ml)
        except GhSubprocessError as exc:
            # Issue #1453: silent skip 廃止。error として集計し exit 1 で返す。
            print(f"ERROR: PR #{pr_num}: {exc}", file=sys.stderr)
            gh_errors += 1
            continue
        if issue_num:
            print(f"created Issue #{issue_num} for PR #{pr_num} ({len(ml)} survived mutants)")
            misses_found += 1

    if misses_found == 0:
        # Gherkin Scenario 2: stdout に "no misses detected" が出力される
        print("no misses detected")
    else:
        print(f"detected {misses_found} miss(es)")
    if gh_errors > 0:
        # Issue #1453: 1 件でも gh subprocess エラーがあれば exit 1（旧 silent skip の廃止）
        return 1
    return 0


def _add_arguments(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--results-file",
        type=Path,
        default=None,
        help="mutmut per-mutant JSON ファイル（フラット形式）",
    )
    subparser.add_argument(
        "--results-text",
        type=Path,
        default=None,
        help="mutmut results text 出力ファイル（text 形式）",
    )
    subparser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="リポジトリルート（既定: カレントディレクトリ）",
    )
    subparser.add_argument(
        "--dry-run",
        action="store_true",
        help="Issue 起票せず、起票候補のみを stderr に出力する",
    )


def _handle(args: argparse.Namespace) -> int:
    if not args.results_file and not args.results_text:
        # 既定パスを試す
        default = Path("mutants") / "mutmut-mutants.json"
        if default.is_file():
            args.results_file = default
        else:
            print(
                "ERROR: --results-file or --results-text が必要です（既定 mutants/mutmut-mutants.json も見つからない）",
                file=sys.stderr,
            )
            return 2
    return run(
        results_file=args.results_file,
        results_text=args.results_text,
        repo_root=args.repo_root,
        dry_run=args.dry_run,
    )


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """tidd_tools エントリーポイント（`__main__.py` から呼ばれる）."""
    parser = subparsers.add_parser(
        "audit-mutmut-review",
        help="mutmut 生存 mutant と AI review verdict を相関して見逃し疑いを Issue 化する（Issue #1300）",
    )
    _add_arguments(parser)
    parser.set_defaults(func=_handle)
