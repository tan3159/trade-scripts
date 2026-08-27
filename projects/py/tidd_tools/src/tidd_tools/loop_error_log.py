"""`tidd loop-error-log` サブコマンド（旧 `scripts/loop-error-log.sh` の Python 移植）.

自律ループ（ai-review / issue-next）の異常系を JSONL ログに記録する。

JSONL レコード:
    {"timestamp": ISO8601, "repo": str, "pr_number": str, "step": str, "error": str, "source": str}

機密情報は `[REDACTED]` に置換してから書き込む（API キー・Bearer トークン・JWT 等）。

環境変数:
- `LOOP_ERROR_LOG_DIR` ログ出力先（デフォルト: `~/.cache/loop-error-log`）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.paths import cache_dir as _cache_dir

logger = logging.getLogger(__name__)

DEFAULT_LOG_DIRNAME = "loop-error-log"
DEFAULT_SOURCE = "tidd_tools.loop_error_log"
_REMOTE_REPO_RE = re.compile(r"github\.com[/:]([^/ :]+/[^/]+?)(?:\.git)?/?$")
_SSH_ALIAS_REPO_RE = re.compile(r"^[^@/\s]+@[^:/\s]+:([^/]+/[^/]+?)(?:\.git)?/?$")


# 機密情報パターン（旧 sh と同等）
_REDACTION_RULES: list[tuple[re.Pattern[str], str]] = [
    # Anthropic API キー
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"), "[REDACTED]"),
    # Google API キー (GCP / Gemini): AIzaSy- プレフィックス + 33 文字
    (re.compile(r"AIzaSy[A-Za-z0-9_-]{33}"), "[REDACTED]"),
    # GitHub トークン: ghp_ / ghs_ / ghr_ プレフィックス
    (re.compile(r"gh[psr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    # GitLab パーソナルアクセストークン
    (re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    # Bearer トークン
    (re.compile(r"[Bb]earer [A-Za-z0-9._+/=-]{10,}"), "Bearer [REDACTED]"),
    # token=<value>
    (re.compile(r"token=[A-Za-z0-9._+/=-]{8,}"), "token=[REDACTED]"),
    # Authorization ヘッダー値
    (
        re.compile(r"[Aa]uthorization:[ \t]*[A-Za-z0-9._-]+(?:[ \t]+[A-Za-z0-9+/=._-]{4,})?"),
        "Authorization: [REDACTED]",
    ),
    # JWT 形式（3 つの Base64 ブロックがドットで区切られる）
    (
        re.compile(r"[A-Za-z0-9+/=_-]{4,}\.[A-Za-z0-9+/=_-]{4,}\.[A-Za-z0-9+/=_-]{10,}"),
        "[REDACTED]",
    ),
]


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "loop-error-log",
        help="自律ループ異常系を JSONL ログに記録する（旧 scripts/loop-error-log.sh）",
        description=__doc__,
    )
    parser.add_argument("--pr", dest="pr_number", default="", help="PR 番号")
    parser.add_argument("--step", required=True, help="ステップ名（必須）")
    parser.add_argument("--error", dest="error_message", required=True, help="エラーメッセージ（必須）")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="ソース名（デフォルト: モジュール名）")
    parser.add_argument(
        "--log-dir",
        dest="log_dir",
        default=None,
        help="ログ出力先（デフォルト: $LOOP_ERROR_LOG_DIR or ~/.cache/loop-error-log）",
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir or os.environ.get("LOOP_ERROR_LOG_DIR") or str(_cache_dir() / DEFAULT_LOG_DIRNAME))
    log_dir.mkdir(parents=True, exist_ok=True)

    sanitized = sanitize(args.error_message)
    record = {
        "timestamp": _now_iso(),
        "repo": _current_repo(),
        "pr_number": args.pr_number or "",
        "step": args.step,
        "error": sanitized,
        "source": args.source,
    }
    log_file = log_dir / f"errors-{_today_utc()}.jsonl"
    if args.dry_run:
        print(f"==> dry-run: would append to {log_file}", file=sys.stderr)
        if args.json_output:
            print(json.dumps(record, ensure_ascii=False))
        return 0
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"==> ループエラーログ記録完了: {log_file}", file=sys.stderr)
    if args.json_output:
        print(json.dumps(record, ensure_ascii=False))
    return 0


# ── 公開 API ────────────────────────────────────────────────────────────────


def sanitize(text: str) -> str:
    """機密情報パターンを `[REDACTED]` に置換する."""
    out = text
    for pattern, replacement in _REDACTION_RULES:
        out = pattern.sub(replacement, out)
    return out


def record(
    step: str,
    error_message: str,
    *,
    pr_number: str = "",
    source: str = DEFAULT_SOURCE,
    log_dir: Path | None = None,
) -> Path:
    """ループエラーを JSONL ログに記録する（プログラマティック API）.

    Issue #2536: core.py が恒久的な exit 3 を検出したときに直接呼び出す。
    機密情報は sanitize() を通してから書き込む。

    Returns:
        書き込んだログファイルの Path。
    """
    dir_ = log_dir or Path(os.environ.get("LOOP_ERROR_LOG_DIR") or str(_cache_dir() / DEFAULT_LOG_DIRNAME))
    dir_.mkdir(parents=True, exist_ok=True)

    sanitized = sanitize(error_message)
    entry = {
        "timestamp": _now_iso(),
        "repo": _current_repo(),
        "pr_number": pr_number or "",
        "step": step,
        "error": sanitized,
        "source": source,
    }
    log_file = dir_ / f"errors-{_today_utc()}.jsonl"
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"==> ループエラーログ記録完了: {log_file}", file=sys.stderr)
    return log_file


# ── 補助 ────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _current_repo() -> str | None:
    """origin の GitHub URL から現在のリポジトリ名を解決する（失敗時は None）。"""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except Exception:  # noqa: BLE001 - ログ記録は本処理を妨げない
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    match = _REMOTE_REPO_RE.search(url) or _SSH_ALIAS_REPO_RE.match(url)
    return match.group(1) if match else None
