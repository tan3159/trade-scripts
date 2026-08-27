"""ai-review 同一 SHA 重複レビュー投稿防止（Issue #2419）.

PR の head SHA に対して ``ai-reviewer-gaia-plan[bot]`` が既にレビューを投稿済みなら
スキップし、前回 VERDICT に対応する exit code を返す。

公開 API:
- :class:`ReviewCacheResult` — チェック結果データクラス
- :func:`check_existing_review` — 既存レビュー確認 + スキップ判定
- :func:`_fetch_pr_reviews` — GitHub API でレビュー一覧を取得（テスト用に公開）
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# レビューを投稿する bot アカウント名（[bot] suffix 付き / なし 両対応）
_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "ai-reviewer-gaia-plan[bot]",
        "ai-reviewer-gaia-plan",
    }
)

# VERDICT → exit code マッピング（issue やること gate を通らず直接返す）
_VERDICT_EXIT_CODE: dict[str, int] = {
    "APPROVE": 0,
    "REQUEST_CHANGES": 1,
}


@dataclass(frozen=True)
class ReviewCacheResult:
    """既存レビューが見つかった場合のキャッシュ結果."""

    hit: bool
    """True なら投稿済みレビューが見つかった（スキップすべき）."""
    verdict: str
    """既存レビューから抽出した VERDICT（APPROVE / REQUEST_CHANGES）."""
    exit_code: int
    """前回 VERDICT に対応する exit code（0=APPROVE / 1=REQUEST_CHANGES）."""
    commit_id: str
    """既存レビューが投稿された commit SHA."""


def _fetch_pr_reviews(pr_num: str, repo: str, token: str) -> list[dict[str, Any]]:
    """GitHub API で PR のレビュー一覧を取得する.

    ``gh api repos/{owner}/{repo}/pulls/{N}/reviews`` を呼び出し、
    解析済みの dict リストを返す。失敗時は空リストを返す。
    """
    import json

    env_override = {}
    if token:
        env_override["GH_TOKEN"] = token

    import os

    env = dict(os.environ, **env_override)

    try:
        proc = subprocess.run(  # noqa: S603
            [
                "gh",
                "api",
                f"repos/{repo}/pulls/{pr_num}/reviews",
                "--paginate",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
            env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("_fetch_pr_reviews: gh api 呼び出し失敗: %s", exc)
        return []

    if proc.returncode != 0:
        logger.debug("_fetch_pr_reviews: gh api exit %s: %s", proc.returncode, proc.stderr.strip())
        return []

    try:
        reviews = json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        logger.debug("_fetch_pr_reviews: JSON 解析失敗: %s", exc)
        return []

    if not isinstance(reviews, list):
        logger.debug("_fetch_pr_reviews: 予期しない型: %s", type(reviews))
        return []

    return reviews


def _extract_verdict_from_body(body: str) -> str:
    """レビュー本文から VERDICT を抽出する.

    ``parse_verdict`` と同一ロジック（re import 回避のためここで定義）。
    """
    import re

    m = re.search(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)", body)
    return m.group(1) if m else ""


def last_reviewed_sha(pr_num: str, repo: str, token: str) -> str:
    """PR に bot が投稿した最後のレビューの commit SHA を返す（見つからない場合は空文字）.

    Issue #3636: ``check_no_new_commit_gate`` が「前回レビュー以降に新コミットが
    存在するか」を判定するために使う。``_fetch_pr_reviews`` は時系列（古い→新しい）
    で返すため、末尾から bot のレビューを探して最初に見つかった ``commit_id`` が
    直近のレビュー SHA になる。
    """
    reviews = _fetch_pr_reviews(pr_num, repo, token)
    for review in reversed(reviews):
        user = review.get("user") or {}
        if user.get("login", "") in _BOT_LOGINS:
            return str(review.get("commit_id", ""))
    return ""


def check_existing_review(
    pr_num: str,
    repo: str,
    head_sha: str,
    token: str,
    *,
    skip_cache: bool = False,
) -> ReviewCacheResult | None:
    """既存レビューを確認し、同一 SHA に投稿済みなら :class:`ReviewCacheResult` を返す.

    スキップ判定しない場合（escape hatch 有効・既存レビューなし・VERDICT 不明）は ``None`` を返す。
    スキップ時は stderr に WARN を出力する。

    Args:
        pr_num: PR 番号（文字列）。
        repo: ``owner/repo`` 形式のリポジトリ名。
        head_sha: PR の head commit SHA。
        token: GitHub API トークン（空文字可）。
        skip_cache: ``AI_REVIEW_SKIP_REVIEW_CACHE=1`` 相当の escape hatch フラグ。

    Returns:
        投稿済みレビューが見つかり有効な VERDICT を抽出できた場合は :class:`ReviewCacheResult`。
        それ以外は ``None``（フル実行へ）。
    """
    if skip_cache:
        logger.debug("check_existing_review: AI_REVIEW_SKIP_REVIEW_CACHE=1 → スキップ判定なし")
        return None

    if not head_sha:
        logger.debug("check_existing_review: head_sha が空 → スキップ判定なし")
        return None

    reviews = _fetch_pr_reviews(pr_num, repo, token)

    for review in reviews:
        user = review.get("user") or {}
        login = user.get("login", "")
        commit_id = review.get("commit_id", "")
        body = review.get("body", "") or ""

        if login not in _BOT_LOGINS:
            continue
        if commit_id != head_sha:
            continue

        # 同一 SHA・bot のレビューが存在する
        verdict = _extract_verdict_from_body(body)
        if not verdict:
            logger.debug(
                "check_existing_review: commit_id=%s の既存レビューに VERDICT なし → フル実行",
                head_sha,
            )
            return None

        exit_code = _VERDICT_EXIT_CODE.get(verdict, 1)

        print(
            f"WARN: PR #{pr_num} の head SHA {head_sha[:12]}... に対して"
            f" {login} が既にレビューを投稿済みです。"
            f" 前回 VERDICT={verdict} を再利用してスキップします（AI_REVIEW_SKIP_REVIEW_CACHE=1 で無効化）。",
            file=sys.stderr,
        )

        return ReviewCacheResult(
            hit=True,
            verdict=verdict,
            exit_code=exit_code,
            commit_id=commit_id,
        )

    return None
