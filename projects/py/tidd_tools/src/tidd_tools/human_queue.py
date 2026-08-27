"""`tidd human-queue` サブコマンド（Issue #1577）.

🙋 needs-human-input ラベル付き open Issue を経過日数降順で一覧する。

終了コード:
- 0: 正常終了（0 件含む）
- 1: GitHub API 呼び出し失敗
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)

_LABEL = "🙋 needs-human-input"
_DEFAULT_LIMIT = 50


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """argparse サブコマンド登録."""
    parser = subparsers.add_parser(
        "human-queue",
        help="🙋 needs-human-input ラベル付き open Issue を経過日数降順で一覧する",
        description=__doc__,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        metavar="N",
        help=f"取得する Issue 数の上限（デフォルト: {_DEFAULT_LIMIT}）",
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    """CLI エントリポイント."""
    try:
        issues = _fetch_human_input_issues(limit=getattr(args, "limit", _DEFAULT_LIMIT))
    except GhCommandError as exc:
        print(f"gh api failed: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"gh api failed: JSON parse error: {exc}", file=sys.stderr)
        return 1

    if not issues:
        print("滞留なし")
        return 0

    sorted_issues = _sort_by_elapsed_desc(issues)
    _print_issues(sorted_issues)
    return 0


# ── 内部関数 ──────────────────────────────────────────────────────────────────


def _fetch_human_input_issues(limit: int = _DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """gh issue list で 🙋 needs-human-input ラベル付き open Issue を取得する."""
    args = [
        "issue",
        "list",
        "--state",
        "open",
        "--label",
        _LABEL,
        "--json",
        "number,title,createdAt,body",
        "--limit",
        str(limit),
    ]
    result = run_subprocess(["gh", *args])
    if result.returncode != 0:
        raise GhCommandError(args, result.returncode, result.stderr or "")
    data = json.loads(result.stdout or "[]")
    if not isinstance(data, list):
        raise GhCommandError(args, 0, f"expected list, got {type(data).__name__}")
    return data


def _sort_by_elapsed_desc(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """経過日数の降順（古い順）でソートして返す."""
    return sorted(issues, key=lambda i: _elapsed_days(i.get("createdAt", "")), reverse=True)


def _elapsed_days(created_at: str) -> int:
    """ISO 8601 文字列から現在までの経過日数を返す."""
    try:
        # GitHub の API は "Z" 末尾のため fromisoformat の前処理が必要（Python < 3.11 互換は不要だが念のため）
        ts = created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        delta = datetime.now(UTC) - dt
        return max(0, delta.days)
    except (ValueError, TypeError):
        return 0


def _extract_decision_line(body: str | None) -> str | None:
    """Issue 本文から「## 判断してほしいこと」セクションの冒頭 1 行を返す.

    セクションが存在しない場合・内容が空の場合は None を返す。
    """
    if not body:
        return None
    in_section = False
    for line in body.splitlines():
        if in_section:
            # 次のセクション（## で始まる行）に到達したら終了
            if line.startswith("##"):
                return None
            stripped = line.strip()
            if stripped:
                return stripped
        elif line.strip() == "## 判断してほしいこと":
            in_section = True
    return None


def _print_issues(issues: list[dict[str, Any]]) -> None:
    """Issue 一覧を標準出力に出力する."""
    for issue in issues:
        number = issue.get("number", "?")
        title = issue.get("title", "（タイトルなし）")
        days = _elapsed_days(issue.get("createdAt", ""))
        decision_line = _extract_decision_line(issue.get("body"))
        print(f"#{number}  {title}  （{days}日）")
        if decision_line:
            print(f"  → {decision_line}")
