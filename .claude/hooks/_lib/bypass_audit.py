"""バイパス監査ログ記録ヘルパ (Issue #1625).

hook がバイパス経路を通ったとき ``record_bypass()`` を呼ぶと
``~/.cache/tidd/bypass-audit.jsonl`` に JSON Lines 形式で 1 行 append される。

集計は `tidd weekly-audit bypass-summary` サブコマンドで行う。

**設計方針:**
- stdlib のみ（hook は依存追加禁止）
- 記録失敗（disk full 等）は silent skip（hook 本体の block 判定に影響しない）
- 環境変数 ``BYPASS_AUDIT_LOG`` でログファイルパスを上書き可（テスト用）
- append の atomic 性は OS の O_APPEND に委ねる（1 行 <= PIPE_BUF なので競合しない）
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
from pathlib import Path
from typing import Any

_BYPASS_LOG_ENV = "BYPASS_AUDIT_LOG"
_DEFAULT_LOG_PATH = Path.home() / ".cache" / "tidd" / "bypass-audit.jsonl"


def _log_path() -> Path:
    override = os.environ.get(_BYPASS_LOG_ENV)
    if override:
        return Path(override)
    return _DEFAULT_LOG_PATH


def _get_current_pr_number() -> int | None:
    """現在の worktree に対応する PR 番号を best-effort で取得する.

    `gh pr view --json number --jq .number` を使って PR 番号を取得する。
    取得できない場合は None を返す（silent fail）。
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "number", "--jq", ".number"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def record_bypass(
    *,
    event: str,
    reason: str | None,
    pr_number: int | None = None,
    _log_file: Path | None = None,
    _auto_pr: bool = True,
) -> None:
    """バイパスイベントを audit log に追記する.

    Args:
        event: バイパス種別。例: ``"allow-test-update"``・``"allow-single-commit"``
        reason: バイパスマーカーの理由文字列（コロン後の値）。取得不能なら None。
        pr_number: PR 番号（取得できる場合のみ）。None のとき _auto_pr=True なら
            ``gh pr view`` で自動取得を試みる。
        _log_file: テスト用ログファイルパス上書き。None のときは env var / デフォルトを使う。
        _auto_pr: True のとき pr_number が None であれば ``_get_current_pr_number()`` で
            best-effort 取得を行う。テスト時は False を推奨（gh subprocess 呼び出し回避）。

    副作用:
        ログファイルが存在しなければ作成する（親ディレクトリも含む）。
        書き込み失敗は silent skip（hook 本体の判定に影響しない）。
    """
    out = _log_file if _log_file is not None else _log_path()
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # pr_number が未指定のとき best-effort で取得
    if pr_number is None and _auto_pr:
        pr_number = _get_current_pr_number()

    payload: dict[str, Any] = {
        "event": event,
        "timestamp": ts,
    }
    if pr_number is not None:
        payload["pr"] = pr_number
    if reason is not None:
        payload["reason"] = reason

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        with out.open("a", encoding="utf-8") as f:
            f.write(serialized + "\n")
    except (OSError, TypeError, ValueError):
        # disk full / permission denied / JSON エラー等は silent skip
        return
