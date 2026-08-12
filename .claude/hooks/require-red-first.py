#!/usr/bin/env python3
"""PreToolUse hook: `gh pr create` 前に TDD の RED-GREEN 順序を機械強制する（Issue #1285）.

**背景（監査 §1-2）:** `.claude/rules/workflow.md:221` の RED 手順は「テストが失敗することを
確認する」の 1 行だけで、誰が・どこに記録するか未定義。PR 中央値マージ 12 分の速度は
TDG（テスト先行）を skip している疑いがあり、順序を機械強制しないと TDD の
RED-GREEN-REFACTOR サイクルが崩れる。

**動作:**

- `gh pr create` の Bash 呼び出しを検知
- `git log origin/main..HEAD` で PR 内の全 commit を解析
- 実装ファイル（`projects/py/*/src/**` / `projects/gas/*/Code.js`）の最古 commit が
  テストファイル（`projects/py/*/tests/**` / `projects/gas/*/tests/**`）の最古 commit
  より前なら **exit 2 でブロック**
- 単一 commit で両方が混在している場合も違反扱いにする
- `type: docs` / `research` / `refactor` / `ci` / `build` はブランチ名前綴で skip
- PR ボディに ``<!-- allow-single-commit: <理由> -->`` があれば bypass する

Issue #2443: worktree 盲目対策 — `_lib.hook_io.resolve_target_cwd` で対象リポジトリの
CWD を解決してから git を実行する（コマンド解析 → payload `cwd` → プロセス CWD）。

Issue #2630: RED 実証ステップ — ``require-red-first-proof`` キーが有効な場合、
テストファイルの初出コミット時点で対象テストが 1 件以上 FAILED であることを確認する。
全件 pass の場合は exit 2 でブロック。detached checkout が失敗した場合はフォールバック。

Issue #2895: TDD 順序判定ロジック（ファイルパス分類・ブランチ prefix skip・commit 順序判定）は
`_lib/tdd_order_check.py` を単一の真実源として参照する（`tidd tdd-check` CLI と共有）。
本ファイルは git subprocess 呼び出しの対象 CWD（`_TARGET_CWD`）を解決する薄いラッパーのみを持ち、
判定ロジック本体は `_lib/tdd_order_check.py` 側にある。

stdlib のみ使用。
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import tdd_order_check as _tdd_order_check
from _lib.bypass_audit import record_bypass as _record_bypass
from _lib.gh_command import extract_pr_body as _extract_body_from_command
from _lib.gh_command import is_gh_pr_create as _is_gh_pr_create
from _lib.hook_io import (
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
    resolve_target_cwd,
)
from _lib.override_markers import (
    extract_reason,
    find_invalid_syntax,
    has_override_marker,
)

_GH_PR_CREATE_RE = re.compile(r"(^|&&|;|\|)\s*gh pr create(\s|$)")
_SKIP_BRANCH_PREFIXES = _tdd_order_check.SKIP_BRANCH_PREFIXES

# Issue #2443: git 実行の対象 CWD（_main() で resolve_target_cwd により設定される）。
# None のままならプロセス CWD（従来挙動）。
_TARGET_CWD: str | None = None


def _current_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
            cwd=_TARGET_CWD,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _classify_file(path: str) -> str | None:
    """ファイルパスを 'impl' or 'test' に分類する（該当しなければ None）.

    Issue #2895: 判定ロジックは `_lib/tdd_order_check.py` を単一の真実源とする。
    """
    return _tdd_order_check.classify_file(path)


def _get_commits_with_files() -> list[tuple[str, set[str]]]:
    """origin/main..HEAD の各 commit と含まれるファイルパスセットを返す（古い順）."""
    return _tdd_order_check.get_commits_with_files(cwd=_TARGET_CWD)


def _get_file_first_add_commit(file_path: str) -> str | None:
    """Issue #1458: 指定 file が最初に追加された commit hash を返す（origin/main..HEAD 範囲）."""
    return _tdd_order_check.get_file_first_add_commit(file_path, cwd=_TARGET_CWD)


def _get_commit_index_map() -> dict[str, int]:
    """origin/main..HEAD の commit hash → 順序 index (古い順) の map を返す."""
    return _tdd_order_check.get_commit_index_map(cwd=_TARGET_CWD)


def _check_tdd_order() -> str | None:
    """RED-GREEN 順序をチェックし、違反があればエラーメッセージを返す（正常時は None）.

    Issue #2895: 純粋な判定ロジックは `_lib/tdd_order_check.evaluate_tdd_order` に委譲する。
    本関数はテストで mock 可能な自身のラッパー関数（`_get_commits_with_files` 等）を
    経由して commits データ・lookup 関数を渡す薄いオーケストレーションのみを担う。

    委譲先が返すブロックメッセージ（Issue #1616 で非エンジニア向けに言い換え済み）:
    「テストファイルと実装ファイルを 1 つのコミットに同時に追加しています。
    先にテストのみをコミットしてから、次のコミットで実装を追加してください。」
    """
    commits = _get_commits_with_files()
    return _tdd_order_check.evaluate_tdd_order(
        commits,
        get_commit_index_map=_get_commit_index_map,
        get_file_first_add_commit=_get_file_first_add_commit,
    )


# ── Issue #2630: RED 実証ステップ ──────────────────────────────────────────────


def _collect_new_test_files() -> list[str]:
    """PR 内で新規追加されたテストファイルの一覧を返す（origin/main..HEAD 範囲）."""
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--reverse",
                "--diff-filter=A",
                "--name-only",
                "--format=",
                "origin/main..HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            cwd=_TARGET_CWD,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [
        p
        for p in result.stdout.strip().splitlines()
        if p and _classify_file(p) == "test"
    ]


def _find_earliest_test_commit(test_files: list[str]) -> str | None:
    """テストファイル群の中で最も古い初出コミット hash を返す（なければ None）."""
    idx_map = _get_commit_index_map()
    earliest_idx: int | None = None
    earliest_sha: str | None = None
    for f in test_files:
        sha = _get_file_first_add_commit(f)
        if sha is None:
            continue
        i = idx_map.get(sha)
        if i is None:
            continue
        if earliest_idx is None or i < earliest_idx:
            earliest_idx = i
            earliest_sha = sha
    return earliest_sha


def _detect_test_runner(test_files: list[str]) -> str:
    """テストファイルのパターンからテストランナーを判定する.

    Returns:
        "pytest" / "jest" / "unknown"
    """
    for f in test_files:
        if re.match(r"^projects/py/", f):
            return "pytest"
        if re.match(r"^projects/gas/", f):
            return "jest"
    return "unknown"


def _run_tests_at_commit(
    commit_sha: str,
    test_files: list[str],
    runner: str,
) -> str | None:
    """指定コミット時点でテストファイルを対象とするテストを実行し、FAILED があれば None（=RED）を返す.

    - FAILED が 1 件以上 → None（RED 確認済み・通過）
    - 全件 pass → エラーメッセージを返す（偽の RED → ブロック）
    - git エラー・タイムアウト・実行失敗 → None（フォールバック・ブロックしない）

    実装: ``git worktree add --detach <tmpdir> <sha>`` で一時 worktree を作成してテスト実行。
    完了後に ``git worktree remove --force <tmpdir>`` で削除する。
    """
    if runner == "unknown":
        return None  # テストランナーが不明 → フォールバック

    with tempfile.TemporaryDirectory(prefix="tidd-red-proof-") as tmp_dir:
        # 1. detached checkout
        try:
            checkout_result = subprocess.run(
                ["git", "worktree", "add", "--detach", tmp_dir, commit_sha],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
                cwd=_TARGET_CWD,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None  # フォールバック

        if checkout_result.returncode != 0:
            # worktree add が失敗した場合は TemporaryDirectory の cleanup が走る前に
            # 既存ディレクトリが git に登録されていない状態でクリーンアップが起こるだけ
            return None  # フォールバック

        try:
            # 2. テスト実行（target のテストファイルのみ）
            if runner == "pytest":
                # .venv は tmp_dir にはないため、_TARGET_CWD の venv python を使う
                venv_python = _find_venv_python()
                if venv_python is None:
                    return None  # フォールバック
                # pyproject.toml のある最上位から実行
                pyproject = _find_pyproject(tmp_dir)
                run_cwd = pyproject if pyproject else tmp_dir
                test_args = [
                    str(venv_python),
                    "-m",
                    "pytest",
                    "--tb=no",
                    "-q",
                ] + [str(Path(tmp_dir) / f) for f in test_files]
                try:
                    run_result = subprocess.run(
                        test_args,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=120,
                        cwd=run_cwd,
                    )
                except subprocess.TimeoutExpired:
                    return None  # フォールバック
            elif runner == "jest":
                # npm/npx が利用可能か確認してから実行
                node_cwd = _find_jest_cwd(tmp_dir, test_files)
                if node_cwd is None:
                    return None  # フォールバック
                jest_paths = [
                    str(Path(tmp_dir) / f)
                    for f in test_files
                    if re.match(r"^projects/gas/", f)
                ]
                if not jest_paths:
                    return None  # フォールバック
                try:
                    run_result = subprocess.run(
                        [
                            "npx",
                            "jest",
                            "--no-coverage",
                            "--passWithNoTests",
                            "--",
                            *jest_paths,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=120,
                        cwd=node_cwd,
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    return None  # フォールバック
            else:
                return None  # フォールバック

            # 3. 結果判定: テストが 1 件以上 FAILED → RED OK
            if run_result.returncode != 0:
                # 少なくとも 1 件失敗 → 真の RED
                sys.stderr.write(
                    f"require-red-first-proof: RED 実証済み（初出コミット {commit_sha[:8]} 時点でテスト失敗）。\n"
                )
                return None  # 通過

            # 全件 pass → 偽の RED（空テスト・assert True 等）
            return (
                f"RED 実証に失敗しました。テスト初出コミット時点で全テストが pass しています\n"
                f"（初出コミット: {commit_sha[:8]}）。\n"
                "テストが実装前に必ず失敗することを確認してからコミットしてください。\n"
                "（空テスト・assert True のような中身のないテストは TDD ゲートをすり抜けます）"
            )
        finally:
            # 4. worktree cleanup（失敗しても継続）
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", tmp_dir],
                    capture_output=True,
                    check=False,
                    timeout=10,
                    cwd=_TARGET_CWD,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass


def _find_venv_python() -> Path | None:
    """pytest を実行できる python インタープリタを探して返す（なければ None）.

    探索順:
    1. ``_TARGET_CWD`` 配下の ``.venv/bin/python3`` （本番環境）
    2. 現在のプロセスの ``sys.executable`` が ``.venv`` 内にある場合はそれを使用
       （テスト環境や hook プロセス自体が venv で起動されている場合）

    pytest モジュールが存在しない python は除外する（import check）。
    """
    base = Path(_TARGET_CWD) if _TARGET_CWD else Path.cwd()
    candidates = [
        base / ".venv" / "bin" / "python3",
        base / ".venv" / "bin" / "python",
    ]
    for p in candidates:
        if p.is_file():
            return p
    # フォールバック: 現プロセスの python 自体が venv 内にある場合
    current_python = Path(sys.executable)
    if ".venv" in current_python.parts:
        return current_python
    return None


def _find_pyproject(tmp_dir: str) -> str | None:
    """tmp_dir 内で pyproject.toml を持つ最上位ディレクトリを返す."""
    # projects/py/<project>/ の pyproject.toml を探す
    for candidate in Path(tmp_dir).glob("projects/py/*/pyproject.toml"):
        return str(candidate.parent)
    return None


def _find_jest_cwd(tmp_dir: str, test_files: list[str]) -> str | None:
    """Jest テスト対象の gas プロジェクトディレクトリを返す（package.json がある場所）."""
    for f in test_files:
        m = re.match(r"^(projects/gas/[^/]+)/", f)
        if m:
            project_dir = Path(tmp_dir) / m.group(1)
            if (project_dir / "package.json").exists():
                return str(project_dir)
    return None


def _collect_new_impl_files() -> list[str]:
    """PR 内で新規追加された実装ファイルの一覧を返す（origin/main..HEAD 範囲）."""
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--reverse",
                "--diff-filter=A",
                "--name-only",
                "--format=",
                "origin/main..HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            cwd=_TARGET_CWD,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [
        p
        for p in result.stdout.strip().splitlines()
        if p and _classify_file(p) == "impl"
    ]


def _check_red_proof() -> str | None:
    """RED 実証ステップ: テスト初出コミット時点でテストが失敗することを確認する.

    テストファイルと実装ファイルの両方が PR で新規追加されている場合のみ実行する。
    テストのみの PR（実装なし）は TDD の RED 実証対象外として skip する。

    Returns:
        None → 通過（RED 実証済み / 対象なし / フォールバック）
        str  → エラーメッセージ（全件 pass → ブロック）
    """
    test_files = _collect_new_test_files()
    if not test_files:
        return None  # 新規テストファイルなし → skip

    impl_files = _collect_new_impl_files()
    if not impl_files:
        return None  # 実装ファイルなし（テストのみの PR） → skip

    earliest_sha = _find_earliest_test_commit(test_files)
    if earliest_sha is None:
        return None  # コミットが特定できない → フォールバック

    runner = _detect_test_runner(test_files)
    return _run_tests_at_commit(earliest_sha, test_files, runner)


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")  # Issue #1364
    if get_tool_name(payload) != "Bash":
        return 0

    command = get_command(payload)
    if not _is_gh_pr_create(command):
        return 0

    # Issue #2443: hook プロセスはセッション CWD（メイン checkout）で動くため、
    # worktree のコミット順序を検査できるよう対象リポジトリの CWD を解決する。
    global _TARGET_CWD
    _TARGET_CWD = resolve_target_cwd(payload, command)

    # branch prefix で skip 対象を判定
    branch = _current_branch()
    prefix = branch.split("/", 1)[0] if branch else ""
    if prefix in _SKIP_BRANCH_PREFIXES:
        return 0

    # Issue #1460: 共通 helper で PR body の allow-single-commit マーカー bypass 判定
    body = _extract_body_from_command(command)
    invalid_markers = find_invalid_syntax(body, ["allow-single-commit"])
    if invalid_markers:
        sys.stderr.write(
            f"Blocked: override marker の書式が不正です: "
            f"{', '.join(invalid_markers)}\n"
            "正しい書式: <!-- allow-single-commit: <理由> -->\n"
        )
        return 2
    if has_override_marker(body, "allow-single-commit"):
        # Issue #1625: バイパス使用を audit log に記録
        reason = extract_reason(body, "allow-single-commit")
        _record_bypass(event="allow-single-commit", reason=reason)
        return 0
    # Issue #1458: commit-order marker で cherry-pick / rebase 経由の順序改竄 bypass
    if has_override_marker(body, "commit-order"):
        return 0

    error = _check_tdd_order()
    if error is not None:
        sys.stderr.write(
            f"require-red-first.py: {error}\n"
            "分割不能な正当な理由がある場合は、PR 本文（PR ボディ）に以下を追加してください:\n"
            "  <!-- allow-single-commit: <理由> -->\n"
            "詳細: docs/reference/hooks.md#require-red-firstpy\n"
        )
        return 2

    # Issue #2630: RED 実証ステップ（コミット順序が正常な場合のみ実行）
    if is_hook_enabled("require-red-first-proof"):
        proof_error = _check_red_proof()
        if proof_error is not None:
            sys.stderr.write(
                f"require-red-first.py: RED 実証に失敗しました。テスト初出コミット時点で全テストが pass しています\n"
                f"詳細: {proof_error}\n"
                "詳細: docs/reference/hooks.md#require-red-firstpy\n"
            )
            return 2

    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("require-red-first"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
