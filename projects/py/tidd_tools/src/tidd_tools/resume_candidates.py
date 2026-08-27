"""`tidd resume-candidates` サブコマンド（Issue #2375）.

needs-human-merge ラベル付き open PR を resume 候補として出力する。
consensus 不一致（secondary が REQUEST_CHANGES を返した）PR は除外する（Issue #2655）。

終了コード:
- 0: 正常終了（0 件含む）
- 1: GitHub API 呼び出し失敗
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.paths import cache_dir as _cache_dir
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)

_LABEL = "needs-human-merge"
_DEFAULT_LIMIT = 50


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """argparse サブコマンド登録."""
    parser = subparsers.add_parser(
        "resume-candidates",
        help="needs-human-merge ラベル付き open PR を resume 候補として出力する（Issue #2375）",
        description=__doc__,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        metavar="N",
        help=f"取得する PR 数の上限（デフォルト: {_DEFAULT_LIMIT}）",
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    """CLI エントリポイント."""
    try:
        prs = _fetch_needs_human_merge_prs(limit=getattr(args, "limit", _DEFAULT_LIMIT))
    except GhCommandError as exc:
        print(f"gh api failed: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"gh api failed: JSON parse error: {exc}", file=sys.stderr)
        return 1

    if not prs:
        print("候補なし: needs-human-merge ラベル付き open PR はありません")
        return 0

    state_root = _get_state_root()
    eligible_prs = _filter_consensus_failed(prs, state_root)
    if not eligible_prs:
        print("候補なし: needs-human-merge ラベル付き open PR はありません")
        return 0

    _print_prs(eligible_prs)
    return 0


# ── 内部関数 ──────────────────────────────────────────────────────────────────


def _get_state_root() -> Path:
    """consensus.json を格納するキャッシュ親ディレクトリを返す.

    ``~/.cache/ai-dev-handbook/ai-reviewer/`` 直下に ``pr-<N>/consensus.json`` が置かれる。
    テスト時はモンキーパッチで差し替える。
    """
    return _cache_dir() / "ai-reviewer"


def _is_consensus_failed(pr_number: int, state_root: Path) -> bool:
    """consensus.json の verdict が REQUEST_CHANGES であれば True を返す（Issue #2655）.

    ファイルが存在しない・解析失敗・verdict が REQUEST_CHANGES 以外の場合は False。
    """
    consensus_path = state_root / f"pr-{pr_number}" / "consensus.json"
    try:
        raw = consensus_path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    return data.get("verdict") == "REQUEST_CHANGES"


def _filter_consensus_failed(
    prs: list[dict[str, Any]],
    state_root: Path,
) -> list[dict[str, Any]]:
    """consensus 不一致 PR を除外し、除外した PR について stderr にメッセージを出力する（Issue #2655）."""
    eligible: list[dict[str, Any]] = []
    for pr in prs:
        number = pr.get("number")
        if isinstance(number, int) and _is_consensus_failed(number, state_root):
            print(
                f"PR #{number} は consensus 不一致のため resume 対象外（consensus.json verdict=REQUEST_CHANGES）",
                file=sys.stderr,
            )
        else:
            eligible.append(pr)
    return eligible


def _fetch_needs_human_merge_prs(limit: int = _DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """gh pr list で needs-human-merge ラベル付き open PR を取得する."""
    args = [
        "pr",
        "list",
        "--state",
        "open",
        "--label",
        _LABEL,
        "--json",
        "number,title,labels,createdAt",
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


def _print_prs(prs: list[dict[str, Any]]) -> None:
    """PR 一覧を標準出力に出力する."""
    for pr in prs:
        number = pr.get("number", "?")
        title = pr.get("title", "（タイトルなし）")
        print(f"#{number}  {title}")
