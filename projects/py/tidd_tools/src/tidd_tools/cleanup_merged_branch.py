"""tidd cleanup-merged-branch サブコマンド (#2370).

マージ済みブランチのみを安全に削除する専用サブコマンド。
`block-dangerous-git.py` の `_check_branch_delete_safe()` 相当の安全性判定ロジックを
共有関数として持ち、以下の 4 条件をすべて満たす場合のみ削除を実行する:

1. ブランチ名に コマンド連結文字（; && ||）を含まない
2. ブランチ名が単一（スペース等を含まない）
3. 該当ブランチに紐付く PR が MERGED 状態
4. ローカル HEAD が PR の headRefOid と一致する

不安全な場合は stderr にエラーを出力して exit 1。

このモジュールの関数（`_gh_pr_state_and_head` / `_local_head_sha` /
`_git_worktree_remove` / `_git_branch_delete`）はテストでモックできる粒度で
分離している。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys

from tidd_tools.shared.issue_body import extract_closes_issues

# コマンド連結パターン（block-dangerous-git.py と同じ基準）
_COMMAND_CHAIN_RE = re.compile(r"(;|&&|\|\|)")


def _is_branch_name_safe(branch: str) -> bool:
    """ブランチ名にコマンド連結文字やスペースが含まれないことを確認する."""
    if _COMMAND_CHAIN_RE.search(branch):
        return False
    # スペース・タブが含まれていたら複数ブランチ指定と判断してブロック
    return not re.search(r"\s", branch)


def _gh_pr_state_and_head(branch: str) -> tuple[str, str] | tuple[None, None]:
    """gh CLI でブランチに紐付く PR の state と headRefOid を取得する.

    取得失敗（PR なし・タイムアウト等）時は (None, None) を返す。
    `--` を挟んで branch_name をオプションとして解釈されないようにする（引数インジェクション対策）。
    """
    try:
        state_result = subprocess.run(
            ["gh", "pr", "view", "--json", "state", "-q", ".state", "--", branch],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
        head_result = subprocess.run(
            ["gh", "pr", "view", "--json", "headRefOid", "-q", ".headRefOid", "--", branch],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None

    if state_result.returncode != 0 or head_result.returncode != 0:
        return None, None

    state = state_result.stdout.strip()
    head_oid = head_result.stdout.strip()
    return (state, head_oid) if state and head_oid else (None, None)


def _local_head_sha(branch: str) -> str | None:
    """ローカルブランチの HEAD SHA を取得する."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _remote_head_sha(branch: str) -> str | None:
    """リモートブランチ（origin/<branch>）の HEAD SHA を取得する."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", branch],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    return lines[0].split("\t", 1)[0] or None


def _gh_pr_number(branch: str) -> str | None:
    """ブランチに紐付く PR 番号を取得する（取得失敗時は None）."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "number", "-q", ".number", "--", branch],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _repo_root() -> str | None:
    """git rev-parse --show-toplevel でリポジトリルートを解決する（#3637）.

    実行 CWD に依存せず、git の toplevel 解決で consumer リポジトリルートを
    特定する。解決失敗時は None（判定処理は fail-open で何も出力しない）。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _emit_coderabbit_screening_notice(branch: str) -> None:
    """CodeRabbit マージ後スクリーニングの要否を stderr に通知する（#3637）.

    リポジトリルート直下に `.coderabbit.yaml` が存在する場合のみ、
    `coderabbit-screening: required (PR #<N>)` を 1 行出力する。
    存在しない場合は何も出力しない（CodeRabbit 未導入 consumer への影響ゼロ）。
    判定処理の失敗（toplevel 解決失敗等）は握りつぶし、exit code を変えない。
    """
    try:
        root = _repo_root()
        if root is None:
            return
        coderabbit_file = os.path.join(root, ".coderabbit.yaml")
        if not os.path.exists(coderabbit_file):
            return
        pr_num = _gh_pr_number(branch) or "?"
        sys.stderr.write(f"coderabbit-screening: required (PR #{pr_num})\n")
    except Exception:
        # 判定処理の失敗は削除処理の成否に引きずらせない
        return


def _gh_pr_body(branch: str) -> str:
    """ブランチに紐付く PR の body を取得する（取得失敗時は空文字）."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "body", "-q", ".body", "--", branch],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _gh_pr_related_issue_open(branch: str) -> bool:
    """PR body が closes する Issue のうち 1 つでも OPEN なら True を返す.

    PR が MERGED でも関連 Issue がまだ OPEN なブランチは作業継続の可能性が
    あるため削除しない（#3398）。PR body が空・取得失敗時は False。
    """
    body = _gh_pr_body(branch)
    for issue_no in extract_closes_issues(body):
        try:
            result = subprocess.run(
                ["gh", "issue", "view", str(issue_no), "--json", "state", "-q", ".state"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.strip() == "OPEN":
            return True
    return False


def _git_worktree_remove(branch: str) -> int:
    """worktree がある場合は削除する（worktree がなければ no-op で 0 を返す）."""
    try:
        # worktree list で branch に対応する worktree パスを特定する
        list_result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0  # worktree 操作失敗は非致命的

    if list_result.returncode != 0:
        return 0

    # `git worktree list --porcelain` の出力から対象 branch の worktree パスを探す
    # フォーマット:
    #   worktree /path/to/worktree
    #   HEAD <sha>
    #   branch refs/heads/<branch>
    target_path: str | None = None
    lines = list_result.stdout.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith("worktree "):
            worktree_path = lines[i][len("worktree ") :].strip()
            # 以降の行から branch を探す
            j = i + 1
            while j < len(lines) and lines[j].strip():
                if lines[j] == f"branch refs/heads/{branch}":
                    target_path = worktree_path
                    break
                j += 1
            if target_path:
                break
        i += 1

    if target_path is None:
        # worktree が存在しない場合は no-op
        return 0

    try:
        remove_result = subprocess.run(
            ["git", "worktree", "remove", target_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1

    return remove_result.returncode


def _git_branch_delete(branch: str) -> int:
    """ローカルブランチを強制削除する."""
    try:
        result = subprocess.run(
            ["git", "branch", "-D", "--", branch],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1
    return result.returncode


def _record_cleanup_done_and_refresh_summary(branch: str) -> None:
    """後処理フェーズ終端（step6-cleanup-done）を自己記録し、既存サマリコメントを更新する（#3556）.

    PR 本文の ``closes #N`` から Issue 番号を解決し、統一日誌へ冪等記録する。
    ``closes #N`` が解決できない場合は記録せず何もしない（例外も投げない）。
    記録成功後、既存サマリコメントがあれば再生成して上書き更新する
    （`merge_summary.refresh_summary_comment`・新規投稿はしない）。
    """
    from tidd_tools import merge_summary, timing_log
    from tidd_tools.shared.issue_body import extract_closes_issues

    pr_body = _gh_pr_body(branch)
    issue_nums = extract_closes_issues(pr_body)
    if not issue_nums:
        return
    timing_log.record_event_once_safe(f"issue-{issue_nums[0]}", "step6-cleanup-done", "point", "cleanup-merged-branch")
    pr_num = _gh_pr_number(branch)
    if pr_num:
        merge_summary.refresh_summary_comment(pr_num)


def check_branch_delete_safe(branch: str) -> bool:
    """マージ済みブランチを安全に削除できるか判定する共有関数.

    block-dangerous-git.py の _check_branch_delete_safe() と同等のロジックを
    tidd_tools 側に共有関数として実装したもの。

    Returns:
        True  — 削除安全（4 条件すべてを満たす）
        False — 削除不安全（1 条件以上が未達）
    """
    if not _is_branch_name_safe(branch):
        return False

    pr_state, pr_head = _gh_pr_state_and_head(branch)
    if pr_state != "MERGED":
        return False

    local_head = _local_head_sha(branch)
    return bool(pr_head and local_head and pr_head == local_head)


def check_remote_branch_delete_safe(branch: str) -> bool:
    """リモートブランチを安全に削除できるか判定する共有関数（#3398）.

    `check_branch_delete_safe()` の安全条件（コマンド非連結・PR MERGED・
    HEAD 一致）をリモートブランチ（origin/<branch>）に適用したもの。

    Returns:
        True  — 削除安全（PR MERGED + リモート HEAD が PR の headRefOid と一致）
        False — 削除不安全
    """
    if not _is_branch_name_safe(branch):
        return False

    pr_state, pr_head = _gh_pr_state_and_head(branch)
    if pr_state != "MERGED":
        return False

    remote_head = _remote_head_sha(branch)
    return bool(pr_head and remote_head and pr_head == remote_head)


def cleanup_merged_branch(branch: str) -> int:
    """マージ済みブランチを worktree 削除 + ブランチ削除する.

    Returns:
        0 — 成功
        1 — 安全条件を満たさない（削除しない）または削除に失敗
    """
    if not _is_branch_name_safe(branch):
        sys.stderr.write(
            f"ERROR: ブランチ名が安全でない（コマンド連結文字またはスペースを含む）: {branch!r}\n"
            "安全条件: コマンド非連結・単一ブランチ・PR MERGED・HEAD一致\n"
        )
        return 1

    pr_state, pr_head = _gh_pr_state_and_head(branch)

    if pr_state != "MERGED":
        actual_state = pr_state if pr_state else "不明（PR なし・取得失敗）"
        sys.stderr.write(
            f"ERROR: PR が MERGED でない: state={actual_state}\n"
            "安全条件: コマンド非連結・単一ブランチ・PR MERGED・HEAD一致\n"
            f"ブランチ: {branch!r}\n"
        )
        return 1

    local_head = _local_head_sha(branch)
    if not (pr_head and local_head and pr_head == local_head):
        sys.stderr.write(
            f"ERROR: ローカル HEAD が PR の headRefOid と一致しない\n"
            f"  PR headRefOid: {pr_head!r}\n"
            f"  local HEAD:    {local_head!r}\n"
            "安全条件: コマンド非連結・単一ブランチ・PR MERGED・HEAD一致\n"
            f"ブランチ: {branch!r}\n"
        )
        return 1

    # worktree 削除（worktree がなければ no-op）
    worktree_rc = _git_worktree_remove(branch)
    if worktree_rc != 0:
        sys.stderr.write(f"WARN: git worktree remove に失敗（{worktree_rc}）。ブランチ削除を続行します。\n")

    # ブランチ削除
    branch_rc = _git_branch_delete(branch)
    if branch_rc != 0:
        sys.stderr.write(f"ERROR: git branch -D に失敗（{branch_rc}）: {branch!r}\n")
        return 1

    # CodeRabbit マージ後スクリーニングの要否通知（#3637）。
    # 判定処理の失敗が削除処理の exit code に影響しないよう握りつぶす。
    with contextlib.suppress(Exception):
        _emit_coderabbit_screening_notice(branch)

    # 後処理フェーズ終端の自己記録 + 既存サマリコメントの上書き更新（#3556）。
    # 計測記録・サマリ更新の失敗が削除処理の exit code に影響してはならないため
    # 例外は握りつぶす（record_event_once_safe / refresh_summary_comment は
    # いずれも fail-open だが、防御として二重に保護する）。
    with contextlib.suppress(Exception):
        _record_cleanup_done_and_refresh_summary(branch)

    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """tidd cleanup-merged-branch サブコマンドを登録する."""
    p = subparsers.add_parser(
        "cleanup-merged-branch",
        help="マージ済みブランチを安全に削除する（worktree remove + branch -D）",
        description=(
            "PR が MERGED でローカル HEAD が PR の headRefOid と一致する場合のみ"
            "worktree 削除とブランチ削除を実行する。"
            "安全条件を満たさない場合は exit 1 で終了する。"
        ),
    )
    p.add_argument("branch", help="削除するブランチ名")
    p.add_argument(
        "--check-only",
        action="store_true",
        default=False,
        help="安全性チェックのみ実行（削除はしない）。block-dangerous-git.py hook から委譲で使用。",
    )
    p.set_defaults(func=_handle)


def _handle(args: argparse.Namespace) -> int:
    if args.check_only:
        return 0 if check_branch_delete_safe(args.branch) else 1
    return cleanup_merged_branch(args.branch)
