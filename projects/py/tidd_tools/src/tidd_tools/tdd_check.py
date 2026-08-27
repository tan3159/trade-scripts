"""`tidd tdd-check <PR番号>` サブコマンド（Issue #2895）.

`.claude/hooks/require-red-first.py` の TDD RED-first 順序判定と同一ロジックを
指定 PR に対して実行する。`/issue-next` の subagent 完了報告検証（STEP2 検証3）が
これまで prose で手動近似していた判定を機械的に置き換えるための CLI。

判定ロジック（ファイルパス分類・ブランチ prefix skip・commit 順序判定）は
`.claude/hooks/_lib/tdd_order_check.py` を単一の真実源として動的 import で参照する
（`.claude/hooks/require-red-first.py` hook と共有。ロジックを本モジュールで
再定義してはならない。再定義すると Issue #2895 が修正した prose 手動近似との
乖離が別の形で再発する）。

終了コード:
- 0: TDD 未実施の疑いなし（branch prefix skip・override marker bypass 含む）
- 1: TDD 未実施の疑いあり（判定理由を標準エラー出力に出力）
- 2: 実行エラー（PR 取得失敗・git fetch 失敗等）
"""

from __future__ import annotations

import argparse
import importlib
import sys
import types

from tidd_tools.shared import gh_client
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.paths import resolve_repo_root
from tidd_tools.shared.subprocess_runner import run as run_subprocess


def _load_lib(module_name: str) -> types.ModuleType:
    """`.claude/hooks/_lib/<module_name>.py` を動的 import する（単一ソース化・Issue #2895）.

    tidd_tools は project 側なので、リポジトリルート起点の相対 import は使えない。
    `.claude/hooks/require-red-first.py` hook と同一の判定ロジックを共有するため、
    動的 import で橋渡しする（`tidd_tools.lint_hardcoded_repo._load_hardcoded_repo_lib`
    と同型・Issue #2944）。
    """
    repo_root = resolve_repo_root()
    lib_dir = repo_root / ".claude" / "hooks" / "_lib"
    if not lib_dir.is_dir():
        raise FileNotFoundError(f"_lib ディレクトリが見つかりません: {lib_dir}")
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    return importlib.import_module(module_name)


_tdd_order_check_lib = _load_lib("tdd_order_check")
_override_markers_lib = _load_lib("override_markers")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "tdd-check",
        help="指定 PR の TDD RED-first 順序疑いを require-red-first.py と同じロジックで判定する",
        description=__doc__,
    )
    parser.add_argument(
        "pr_number",
        type=int,
        help="判定対象の PR 番号",
    )
    add_common_flags(parser, dry_run_supported=False)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    pr_number = args.pr_number

    try:
        pr = gh_client.pr_view(pr_number, fields=("headRefName", "body"))
    except GhCommandError as exc:
        print(f"エラー: PR #{pr_number} の取得に失敗しました: {exc}", file=sys.stderr)
        return 2

    branch = pr.get("headRefName") or ""
    body = pr.get("body") or ""

    prefix = branch.split("/", 1)[0] if branch else ""
    if prefix in _tdd_order_check_lib.SKIP_BRANCH_PREFIXES:
        return 0

    if _override_markers_lib.has_override_marker(body, "allow-single-commit"):
        return 0
    if _override_markers_lib.has_override_marker(body, "commit-order"):
        return 0

    repo_root = resolve_repo_root()
    # destination なし refspec（`git fetch origin <branch>`）は remote.origin.fetch の
    # 設定内容（single-branch clone 等）次第で origin/<branch> tracking ref を更新しない
    # ことがある（FETCH_HEAD のみ更新）。destination 付き refspec で明示的に指定し、
    # 環境に依らず origin/<branch> を確実に更新する（PR #3006 codex レビュー指摘）。
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    fetch_result = run_subprocess(["git", "fetch", "origin", refspec], cwd=repo_root)
    if fetch_result.returncode != 0:
        print(
            f"エラー: git fetch origin {branch} に失敗しました: {fetch_result.stderr}",
            file=sys.stderr,
        )
        return 2

    error = _tdd_order_check_lib.check_tdd_order(
        base_ref="origin/main",
        head_ref=f"origin/{branch}",
        cwd=str(repo_root),
    )
    if error is not None:
        print(f"tidd tdd-check: {error}", file=sys.stderr)
        return 1
    return 0
