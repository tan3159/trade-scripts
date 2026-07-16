#!/usr/bin/env python3
"""stale-while-revalidate 用のバックグラウンド refresh helper (Issue #1393).

`gh_cache.get_issue_or_stale_with_bg_refresh()` が subprocess.Popen で detach 起動する。
stale cache hit 時に gh subprocess で Issue を再取得して cache を更新する。

失敗時は silent（stale cache が既に呼び出し側に返っているため hook の成功は保たれる）。

#1969: rate limit ガード（#1463 から移設）を追加。remaining < 5 の場合は fetch を控える。
require-issue.py 側では stale hit 経路で rate limit 照会しない（高速性維持のため）。

stdlib のみ使用。

環境変数:
    TIDD_GH_CACHE_DB_PATH: テスト用に DB path を差し替える（省略時は既定パス）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Issue #1969: quota 保護（#1463 から移設）。BG refresh 側で rate limit を確認する
_RATE_LIMIT_NEAR_EXCEEDED = 5


def _rate_limit_remaining() -> int | None:
    """`gh api rate_limit` で core remaining を返す。取得失敗時は None."""
    try:
        result = subprocess.run(
            ["gh", "api", "rate_limit", "--jq", ".resources.core.remaining"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    stripped = result.stdout.strip()
    return int(stripped) if stripped.isdigit() else None


def _fetch_issue(issue_number: int) -> dict | None:
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--json", "number,title,state"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 1
    try:
        issue_number = int(argv[1])
    except ValueError:
        return 1

    override_path = os.environ.get("TIDD_GH_CACHE_DB_PATH")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from _lib import gh_cache  # noqa: WPS433

    if override_path:
        gh_cache._DB_PATH = Path(override_path)

    # Issue #1969: quota 保護（#1463）を BG 側へ移設。remaining < 5 なら fetch を控える
    remaining = _rate_limit_remaining()
    if remaining is not None and remaining < _RATE_LIMIT_NEAR_EXCEEDED:
        return 0

    data = _fetch_issue(issue_number)
    if data is None:
        return 1
    gh_cache.upsert_issue(issue_number, data)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
