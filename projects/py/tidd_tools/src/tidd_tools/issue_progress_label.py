"""Issue 着手中を示す `🔧 in-progress` ラベルを GitHub API で管理する（Issue #2804）.

`tidd issue-next-state check-liveness` はローカルファイルベースの liveness 機構であり、
別マシン（自宅・オフィス等）で動く別セッションからは原理上見えない。GitHub Issue の
ラベルはマシンをまたいで共有される数少ない状態のため、ラベルで着手中状態を
可視化・排除する。

付け忘れ（acquire 側）は機械強制する: `tidd issue-next-state init <N>`（`run_cli` の
`init` アクション）実行時に本モジュールの `add_in_progress_label` が呼ばれる。
外し忘れ（release 側）は「実害がほとんどない」との判断（#2804）により軽量実装とし、
`tidd issue-next-state clear <N>`（`run_cli` の `clear` アクション）実行時に
`remove_in_progress_label` を呼ぶだけに留める（TTL 自動失効・定期監査は対象外）。

サブコマンド:
- `check-in-progress-label <N>` Issue に `🔧 in-progress` ラベルが付いているか判定する
    exit 0 = ラベルなし（着手可能）、exit 1 = ラベルあり（他セッションが作業中の可能性）
    フェイルセーフ: GitHub API 取得失敗時は exit 0（ブロックしない）

ラベル付与・除去は `ai_review/subcommands.py` の `_add_needs_human_merge_label` /
`_remove_needs_human_merge_label`（Issue #1329）と同じ REST API パターンを踏襲する。
`gh issue edit --add-label` は内部で GraphQL 経由の over-fetch により `read:org` scope を
要求するため、`repo` scope のみで通る `POST /repos/{repo}/issues/{n}/labels` /
`DELETE /repos/{repo}/issues/{n}/labels/{name}` を使う。失敗しても処理をブロックせず
stderr に WARN を残すだけに留める（label-pr.py と同様の hook 失敗原則）。

環境変数:
- `ISSUE_NEXT_STATE_ROOT` が設定されている場合はテスト隔離目的（`issue_next_state.py`
  のテストが state ファイル書き込み先を tmp_path に切り替えるために必ず設定する）と
  みなし、`add_in_progress_label` / `remove_in_progress_label` の GitHub API 呼び出しを
  スキップする。これにより既存の subprocess CLI テスト（`test_issue_next_state.py` 等）が
  実リポジトリの Issue に誤ってラベルを付与・除去することを防ぐ。
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import urllib.parse

from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.gh_client import issue_view, repo_name_with_owner

logger = logging.getLogger(__name__)

IN_PROGRESS_LABEL = "🔧 in-progress"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "check-in-progress-label",
        help="Issue に in-progress ラベルが付いているか判定する（exit 0=なし/1=あり）",
        description=__doc__,
    )
    parser.add_argument("issue", type=int, help="対象 Issue 番号")
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    repo = os.environ.get("REPO") or None
    if has_in_progress_label(args.issue, repo=repo):
        print(
            f"Issue #{args.issue} には既に '{IN_PROGRESS_LABEL}' ラベルが付与されています。"
            "他セッションが作業中の可能性があります。",
            file=sys.stdout,
        )
        return 1
    return 0


# ── 判定 ────────────────────────────────────────────────────────────────────


def has_in_progress_label(issue_num: int, repo: str | None = None) -> bool:
    """Issue に `🔧 in-progress` ラベルが付いているか判定する.

    取得失敗時はフェイルセーフで False を返す（着手をブロックしない）。
    """
    try:
        data = issue_view(issue_num, repo=repo, fields=("labels",))
    except GhCommandError as exc:
        logger.warning("Issue #%s のラベル取得に失敗しました: %s", issue_num, exc)
        return False
    labels = data.get("labels")
    if not isinstance(labels, list):
        return False
    return any(isinstance(entry, dict) and entry.get("name") == IN_PROGRESS_LABEL for entry in labels)


# ── 付与・除去 ──────────────────────────────────────────────────────────────


def _is_test_isolated() -> bool:
    """テスト隔離目的の env var が設定されているか（GitHub API 呼び出しをスキップする条件）."""
    return bool(os.environ.get("ISSUE_NEXT_STATE_ROOT"))


def add_in_progress_label(issue_num: int, repo: str | None = None) -> None:
    """Issue に `🔧 in-progress` ラベルを付与する."""
    if _is_test_isolated():
        return
    resolved_repo = repo or repo_name_with_owner()
    if not resolved_repo:
        print(
            f"==> WARN: repo 解決に失敗したため Issue #{issue_num} への "
            f"'{IN_PROGRESS_LABEL}' ラベル付与をスキップしました",
            file=sys.stderr,
        )
        return
    _run_label_api(
        ["-X", "POST", f"/repos/{resolved_repo}/issues/{issue_num}/labels", "-f", f"labels[]={IN_PROGRESS_LABEL}"],
        issue_num,
        "付与",
    )


def remove_in_progress_label(issue_num: int, repo: str | None = None) -> None:
    """Issue から `🔧 in-progress` ラベルを除去する."""
    if _is_test_isolated():
        return
    resolved_repo = repo or repo_name_with_owner()
    if not resolved_repo:
        print(
            f"==> WARN: repo 解決に失敗したため Issue #{issue_num} からの "
            f"'{IN_PROGRESS_LABEL}' ラベル除去をスキップしました",
            file=sys.stderr,
        )
        return
    encoded_label = urllib.parse.quote(IN_PROGRESS_LABEL, safe="")
    _run_label_api(
        ["-X", "DELETE", f"/repos/{resolved_repo}/issues/{issue_num}/labels/{encoded_label}"],
        issue_num,
        "除去",
    )


def _run_label_api(api_args: list[str], issue_num: int, action_label: str) -> None:
    env = dict(os.environ)
    try:
        proc = subprocess.run(  # noqa: S603
            ["gh", "api", *api_args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(
            f"==> WARN: Issue #{issue_num} への '{IN_PROGRESS_LABEL}' ラベル{action_label}に失敗しました: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return
    if proc.returncode != 0:
        stderr_head = (proc.stderr or "").strip().splitlines()[:2]
        detail = " | ".join(stderr_head) if stderr_head else f"exit={proc.returncode}"
        print(
            f"==> WARN: Issue #{issue_num} への '{IN_PROGRESS_LABEL}' ラベル{action_label}に失敗しました: {detail}",
            file=sys.stderr,
        )
        return
    print(f"==> Issue #{issue_num} に '{IN_PROGRESS_LABEL}' ラベルを{action_label}しました", file=sys.stderr)
