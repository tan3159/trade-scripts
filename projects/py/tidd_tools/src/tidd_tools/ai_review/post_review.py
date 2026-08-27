"""GitHub レビュー投稿 + 指数バックオフリトライ（旧 ai-review.sh ``_gh_with_retry`` / ``post_review``）.

公開 API:
- :func:`gh_with_retry` — 任意の gh コマンドを指数バックオフ付きで実行する汎用ラッパー
- :func:`post_review` — bot トークンで ``gh pr review --approve / --request-changes`` を投稿
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tidd_tools.ai_review.state_dir import resolve_state_dir as _resolve_state_dir
from tidd_tools.retry import retry_with_backoff

logger = logging.getLogger(__name__)

DEFAULT_GH_RETRY_MAX = 3
_VERDICT_LINE_RE = re.compile(r"^VERDICT:\s*(?:APPROVE|REQUEST_CHANGES)\s*$", re.MULTILINE)


def ensure_verdict_in_review_body(review_body: str, verdict: str) -> str:
    """レビュー本文の判定行を投稿する verdict に正規化する.

    backend 生出力に依存せず、正式レビュー・コメントフォールバックのどちらにも
    機械監査可能な canonical ``VERDICT`` 行を残す（Issue #3838）。
    """
    body_without_verdict = _VERDICT_LINE_RE.sub("", review_body).lstrip("\n")
    return f"VERDICT: {verdict}\n\n{body_without_verdict}".rstrip() + "\n"


def gh_with_retry(
    args: list[str],
    *,
    max_retries: int | None = None,
    env: dict[str, str] | None = None,
    sleep_func: Any | None = None,
) -> subprocess.CompletedProcess[str]:
    """gh コマンドを指数バックオフ付きで実行する.

    Args:
        args: ``["pr", "review", ...]`` のような gh サブコマンド引数（先頭 ``gh`` は付けない）
        max_retries: 上書き値（None なら ``AI_REVIEW_GH_RETRY_MAX`` または 3）
        env: 子プロセスの環境変数
        sleep_func: テストで差し替え可能な sleep 実装

    Returns:
        最終試行の :class:`subprocess.CompletedProcess`
    """
    retry_cap = (
        max_retries
        if max_retries is not None
        else int(os.environ.get("AI_REVIEW_GH_RETRY_MAX", str(DEFAULT_GH_RETRY_MAX)))
    )

    def _run_gh() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
            timeout=60,
            errors="replace",
        )

    def _on_retry(attempt: int, wait: float, result: Any, exc: BaseException | None) -> None:
        proc: subprocess.CompletedProcess[str] = result
        print(
            f"==> GitHub API エラー（終了コード: {proc.returncode}）。{wait}s 後にリトライします"
            f"（{attempt + 1}/{retry_cap}）。",
            file=sys.stderr,
        )

    def _on_exhausted(last_result: Any, last_exc: BaseException | None) -> subprocess.CompletedProcess[str]:
        proc: subprocess.CompletedProcess[str] = last_result
        print(
            f"==> GitHub API が最大リトライ回数（{retry_cap}回）後もエラーで失敗しました"
            f"（終了コード: {proc.returncode}）。",
            file=sys.stderr,
        )
        return proc

    result: subprocess.CompletedProcess[str] = retry_with_backoff(
        _run_gh,
        max_retries=retry_cap,
        is_success=lambda proc: proc.returncode == 0,
        on_retry=_on_retry,
        on_exhausted=_on_exhausted,
        sleep_func=sleep_func or time.sleep,
    )
    return result


def _state_dir(pr_num: str) -> Path:
    return _resolve_state_dir(pr_num)


def _record_post_review_error(pr_num: str, message: str) -> None:
    state = _state_dir(pr_num)
    try:
        state.mkdir(parents=True, exist_ok=True)
        (state / "post-review-error").write_text(message + "\n", encoding="utf-8")
    except OSError as exc:
        logger.debug("post-review-error 書き込み失敗: %s", exc)


def post_review(
    pr_num: str,
    repo: str,
    verdict: str,
    review_body: str,
    app_token: str,
    *,
    max_retries: int | None = None,
    as_formal_review: bool = True,
) -> bool:
    """GitHub にレビューを投稿する.

    旧 sh の挙動:
    - ``app_token`` が空: ``gh pr review`` をスキップして ``gh pr comment`` にフォールバック
    - ``gh pr review`` 失敗時: ``gh pr comment`` にフォールバック（Issue #747）
    - すべて失敗したら ``${STATE_DIR}/post-review-error`` を書き残す

    Args:
        as_formal_review: ``True``（デフォルト）の場合は ``gh pr review --approve/--request-changes``
            による正式レビューを試み、失敗時のみ ``gh pr comment`` にフォールバックする。
            ``False`` の場合は ``gh pr review`` を試みず常に ``gh pr comment`` のみで投稿する。
            parser critical PR の consensus 確定前の中間コメント（primary early post）で使用する（Issue #2523）。

    Returns:
        ``True`` if the review was posted successfully, ``False`` otherwise.
        既存呼び出し元（core.py）は戻り値を無視しても従来と同等の挙動になる（fail-open）。
    """
    gh_args = ["--repo", repo]
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    normalized_body = ensure_verdict_in_review_body(review_body, verdict)
    comment_body = f"**[AIレビュー: {verdict}]**\n\n{normalized_body}"

    if not app_token:
        print(
            "WARN: app_token が空です。gh pr review をスキップして gh pr comment で VERDICT を投稿します。",
            file=sys.stderr,
        )
        comment_proc = gh_with_retry(
            ["pr", "comment", str(pr_num), *gh_args, "--body", comment_body],
            max_retries=max_retries,
        )
        if comment_proc.returncode != 0:
            print(
                f"WARN: gh pr comment も失敗しました（終了コード: {comment_proc.returncode}）。"
                "post-review-error に記録します。",
                file=sys.stderr,
            )
            _record_post_review_error(
                str(pr_num),
                f"post_review failed (app_token empty, gh pr comment exit {comment_proc.returncode}) at {timestamp}",
            )
            return False
        return True

    env = dict(os.environ)
    env["GH_TOKEN"] = app_token

    # as_formal_review=False の場合は gh pr review を試みず常に gh pr comment のみで投稿する。
    # consensus 確定前の中間コメント（parser critical PR の primary early post）で使用する（Issue #2523）。
    # この経路では review_body をそのまま投稿本文として使う（verdict ヘッダーは付加しない）。
    # _post_primary_review から呼ばれる際は body（review_output + backend_footer）が
    # review_body として渡されるため、二重フォーマット防止のため comment_body を使わない。
    if not as_formal_review:
        comment_proc = gh_with_retry(
            ["pr", "comment", str(pr_num), *gh_args, "--body", normalized_body],
            max_retries=max_retries,
            env=env,
        )
        if comment_proc.returncode != 0:
            print(
                f"WARN: gh pr comment に失敗しました（終了コード: {comment_proc.returncode}）。"
                "post-review-error に記録します。",
                file=sys.stderr,
            )
            _record_post_review_error(
                str(pr_num),
                "post_review failed (as_formal_review=False, "
                f"gh pr comment exit {comment_proc.returncode}) at {timestamp}",
            )
            return False
        return True

    review_args = ["pr", "review", str(pr_num), *gh_args, "--body", normalized_body]
    if verdict == "APPROVE":
        review_args.append("--approve")
    else:
        review_args.append("--request-changes")

    review_proc = gh_with_retry(review_args, max_retries=max_retries, env=env)
    if review_proc.returncode == 0:
        return True

    print(
        f"WARN: gh pr review 失敗（終了コード: {review_proc.returncode}）。gh pr comment にフォールバックします。",
        file=sys.stderr,
    )
    comment_proc = gh_with_retry(
        ["pr", "comment", str(pr_num), *gh_args, "--body", comment_body],
        max_retries=max_retries,
        env=env,
    )
    if comment_proc.returncode != 0:
        print(
            f"WARN: gh pr comment も失敗しました（終了コード: {comment_proc.returncode}）。"
            "post-review-error に記録します。",
            file=sys.stderr,
        )
        _record_post_review_error(
            str(pr_num),
            f"post_review failed (gh pr review exit {review_proc.returncode}, "
            f"gh pr comment exit {comment_proc.returncode}) at {timestamp}",
        )
        return False
    return True
