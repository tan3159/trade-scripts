"""tidd sweep-merged-branches サブコマンド (#3398).

PR マージ後にローカル・リモートの全ブランチ（main 除く・アクティブな
worktree 使用中は除外）をスイープし、安全条件を満たすものだけを自動削除する。

安全条件は `cleanup_merged_branch.check_branch_delete_safe()` /
`check_remote_branch_delete_safe()` を再利用する（DRY 原則・#3398）:

1. ブランチ名にコマンド連結文字（; && ||）やスペースを含まない
2. 該当ブランチに紐付く PR が MERGED 状態
3. ローカル（またはリモート）HEAD が PR の headRefOid と一致する

加えて、PR body が closes する関連 Issue がまだ OPEN のブランチは削除しない。
安全条件を満たさないブランチは削除せず、stderr に非ブロッキングで一覧を
レポートして exit 0 で終了する。
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from tidd_tools.cleanup_merged_branch import (
    _gh_pr_related_issue_open,
    _git_branch_delete,
    check_branch_delete_safe,
    check_remote_branch_delete_safe,
)


def _get_active_worktree_branches() -> set[str]:
    """`git worktree list` でアクティブに使用中のブランチ名一覧を返す."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()

    if result.returncode != 0:
        return set()

    branches: set[str] = set()
    for line in result.stdout.splitlines():
        if line.startswith("branch refs/heads/"):
            branches.add(line[len("branch refs/heads/") :])
    return branches


def _list_local_branches() -> list[str]:
    """ローカルブランチ一覧を取得する（main は除く）."""
    try:
        result = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    return [b for b in result.stdout.splitlines() if b and b != "main"]


def _list_remote_branches() -> list[str]:
    """リモートブランチ一覧を取得する（origin/main は除く）."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    branches: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        _, _, ref = line.partition("\t")
        branch = ref.removeprefix("refs/heads/")
        if branch and branch != "main":
            branches.append(branch)
    return branches


def _git_push_origin_delete(branch: str) -> int:
    """リモートブランチを削除する（git push origin --delete <branch> 相当）."""
    try:
        result = subprocess.run(
            ["git", "push", "origin", "--delete", "--", branch],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1
    return result.returncode


def _skip_reason(branch: str, *, remote: bool) -> str | None:
    """ブランチを削除しない理由を返す（削除してよい場合は None）."""
    if remote:
        if not check_remote_branch_delete_safe(branch):
            return "安全条件未達（PR が MERGED でない・対応 PR なし・リモート HEAD 不一致）"
    elif not check_branch_delete_safe(branch):
        return "安全条件未達（PR が MERGED でない・対応 PR なし・ローカル HEAD 不一致）"

    if _gh_pr_related_issue_open(branch):
        return "関連 Issue が OPEN"
    return None


def sweep_all() -> None:
    """ローカル・リモート全ブランチをスイープし、安全条件を満たすものを自動削除する.

    安全条件を満たさないブランチは削除せず stderr に一覧を出力する。
    常に exit 0（非ブロッキング）を前提とする。
    """
    active = _get_active_worktree_branches()
    skipped: list[str] = []

    for branch in _list_local_branches():
        if branch in active:
            continue
        reason = _skip_reason(branch, remote=False)
        if reason is None:
            if _git_branch_delete(branch) == 0:
                sys.stderr.write(f"sweep-merged-branches: deleted local branch: {branch}\n")
            else:
                sys.stderr.write(f"sweep-merged-branches: WARN: failed to delete local branch: {branch}\n")
        else:
            skipped.append(f"{branch}（{reason}）")

    for branch in _list_remote_branches():
        if branch in active:
            continue
        reason = _skip_reason(branch, remote=True)
        if reason is None:
            if _git_push_origin_delete(branch) == 0:
                sys.stderr.write(f"sweep-merged-branches: deleted remote branch: {branch}\n")
            else:
                sys.stderr.write(f"sweep-merged-branches: WARN: failed to delete remote branch: {branch}\n")
        else:
            skipped.append(f"{branch}（{reason}）")

    if skipped:
        unique = sorted(set(skipped))
        sys.stderr.write("sweep-merged-branches: skipped (safe condition not met): " + "; ".join(unique) + "\n")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """tidd sweep-merged-branches サブコマンドを登録する."""
    p = subparsers.add_parser(
        "sweep-merged-branches",
        help=(
            "ローカル・リモートの全ブランチ（main 除く）をスイープし、"
            "安全条件（PR MERGED + HEAD 一致 + 関連 Issue 非 OPEN）を満たすものを自動削除する"
        ),
        description=(
            "PR マージ後のブランチ蓄積を解消するスイープコマンド。"
            "安全条件を満たさないブランチは削除せず stderr にレポートする。"
            "常に exit 0（非ブロッキング）。"
        ),
    )
    p.set_defaults(func=_handle)


def _handle(args: argparse.Namespace) -> int:
    sweep_all()
    return 0
