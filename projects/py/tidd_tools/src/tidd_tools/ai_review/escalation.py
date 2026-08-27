"""エスカレーション処理（旧 ai-review.sh §6）.

公開 API:
- :func:`post_escalation_comment` — PR にエスカレーション理由をコメント投稿
- :func:`notify_slack` — Slack Webhook へ通知（``SLACK_WEBHOOK_URL`` 設定時のみ）
- :func:`record_loop_error` — ``tidd_tools.loop_error_log`` 経由で異常系ログを書き込む
- :func:`handle_escalation` — 上記をまとめて呼ぶオーケストレーター
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from tidd_tools.ai_review.post_review import post_review
from tidd_tools.ai_review.state_dir import resolve_state_dir as _resolve_state_dir
from tidd_tools.ai_review.timing import save_timing
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)

_SUMMARY_RE = re.compile(r"^#+ サマリー\s*\n(.*?)(?=^#+ 指摘事項|^#+ |\Z)", re.MULTILINE | re.DOTALL)


def _extract_summary(review_output: str) -> str:
    if not review_output:
        return ""
    m = _SUMMARY_RE.search(review_output)
    if not m:
        return ""
    return m.group(1).strip()


def post_escalation_comment(
    pr_num: str,
    repo: str,
    reason: str,
    attempt: int,
    review_summary: str,
    app_token: str = "",
) -> None:
    """PR にエスカレーション理由を 1 件のコメントとして投稿する."""
    body = (
        "## AIレビューが解決できませんでした\n\n"
        f"**詰まった理由:** {reason}\n"
        f"**試行回数:** {attempt}回\n"
        "**人間の判断が必要です。**\n\n"
        "### 最後のレビューサマリー\n\n"
        f"{review_summary}\n\n"
        "---\n"
        "- このままマージする場合: PR をマージしてください\n"
        "- 再設計が必要な場合: PR を閉じて Issue を見直してください\n"
    )
    env = dict(os.environ)
    if app_token:
        env["GH_TOKEN"] = app_token
    proc = subprocess.run(  # noqa: S603
        ["gh", "pr", "comment", str(pr_num), "--repo", repo, "--body", body],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        print("WARN: エスカレーションコメントの投稿に失敗しました。", file=sys.stderr)


def notify_slack(pr_num: str, reason: str, *, webhook_url: str | None = None) -> None:
    """``SLACK_WEBHOOK_URL`` が設定されていれば Slack に通知する."""
    url = webhook_url if webhook_url is not None else os.environ.get("SLACK_WEBHOOK_URL", "")
    if not url:
        return
    payload = json.dumps(
        {"text": f"PR #{pr_num} が詰まっています。人間の判断が必要です。理由: {reason}"},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 — URL は呼び出し元が制御
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310
            pass
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        logger.debug("Slack 通知失敗: %r", exc)
        print("WARN: Slack 通知に失敗しました。", file=sys.stderr)


def record_loop_error(
    pr_num: str,
    step: str,
    error_message: str,
    *,
    repo_root: Path | None = None,
) -> None:
    """``tidd_tools loop-error-log`` を呼んで JSONL ログを残す.

    ログ記録の失敗は無視する（旧 sh ``_record_loop_error`` の ``|| true`` 相当）。
    """
    root = repo_root or Path.cwd()
    project_dir = root / "projects" / "py" / "tidd_tools"
    if not project_dir.is_dir():
        return
    try:
        run_subprocess(
            [
                "uv",
                "run",
                "--project",
                str(project_dir),
                "python",
                "-m",
                "tidd_tools",
                "loop-error-log",
                "--pr",
                str(pr_num),
                "--step",
                step,
                "--error",
                error_message,
                "--source",
                "ai-review.py",
            ],
            capture=True,
        )
    except (OSError, RuntimeError) as exc:
        logger.debug("loop-error-log 呼び出し失敗: %s", exc)


def _state_dir(pr_num: str) -> Path:
    return _resolve_state_dir(pr_num)


def handle_escalation(
    pr_num: str,
    repo: str,
    reason: str,
    attempt: int,
    review_output: str,
    review_body: str,
    app_token: str = "",
    *,
    repo_root: Path | None = None,
    started_at: datetime | None = None,
) -> int:
    """エスカレーション一連の処理を実行し、最終的に exit code 2 相当を返す.

    1. ``post_review`` で REQUEST_CHANGES を投稿
    2. ``post_escalation_comment`` でサマリー付きコメントを投稿
    3. ``notify_slack`` で Slack に通知
    4. ``${STATE_DIR}/escalated`` を作成（``--continue-with-verdict`` の誤実行防止）

    Note: エスカレーション（試行回数上限・同一指摘繰り返し）は設計上の正常系フローであり
    バグではないため、``record_loop_error`` を呼ばない（Issue #2160 / #1750 同一パターン）。

    旧 timing.json の ``review_total_duration`` 記録は #2936 で撤去した
    （所要時間は統一日誌の airview marks / ai-review-verdict の started_at・ended_at で算出）。
    """
    print(f"==> エスカレーション: {reason}", file=sys.stderr)

    post_review(pr_num, repo, "REQUEST_CHANGES", review_body, app_token)

    summary = _extract_summary(review_output)
    post_escalation_comment(pr_num, repo, reason, attempt, summary, app_token)
    notify_slack(pr_num, reason)

    state = _state_dir(pr_num)
    try:
        state.mkdir(parents=True, exist_ok=True)
        (state / "escalated").touch()
    except OSError as exc:
        logger.debug("escalated フラグ作成失敗: %s", exc)

    # Issue #1751: verdict センチネルファイルを書き込んで detect_orphan_review が
    # COMPLETED を返すようにする。書き込みがなければ次のセッションで INTERRUPTED と
    # 判定され attempt=1 で再実行→ 同じ 3 回エスカレーションが繰り返す無限ループになる。
    save_timing(
        pr_num,
        "ESCALATED",
        "escalation",
        state_dir=state,
        started_at=started_at,
    )
    return 2
