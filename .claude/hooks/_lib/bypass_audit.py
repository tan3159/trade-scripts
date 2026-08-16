"""バイパス監査ログ記録ヘルパ (Issue #1625).

hook がバイパス経路を通ったとき ``record_bypass()`` を呼ぶと
``shared/paths.cache_dir() / "bypass-audit.jsonl"`` 相当のパスに JSON Lines 形式で
1 行 append される（Issue #2950: reader 側 `tidd_tools.weekly_audit` とパスを統一）。

集計は `tidd weekly-audit bypass-summary` サブコマンドで行う。

**設計方針:**
- stdlib のみ（hook は依存追加禁止。そのため `platformdirs` は import せず、
  `bw_session_check.py::_get_cache_base()` と同水準の簡易ロジックで OS 別パスを解決する）
- 記録失敗（disk full 等）は skip + stderr WARN（hook 本体の block 判定に影響しない。Issue #1999）
- 環境変数 ``BYPASS_AUDIT_LOG`` でログファイルパスを上書き可（テスト用）
- append の atomic 性は OS の O_APPEND に委ねる（1 行 <= PIPE_BUF なので競合しない）
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_BYPASS_LOG_ENV = "BYPASS_AUDIT_LOG"

# shared/paths.py の APP_NAME と統一（Issue #2950）
# Issue #3683: 上流固有文字列を排除するため中立 app 名 tidd を使う
# （shared/paths.py の APP_NAME = "tidd" と同一）。
_APP_NAME = "tidd"


def _default_cache_base() -> Path:
    """OS 別のキャッシュベースディレクトリを返す（`shared/paths.cache_dir()` の簡易版）.

    hook は stdlib のみで動く制約があるため `platformdirs` は使えない。
    """
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            return Path(local_app_data) / _APP_NAME
        return Path.home() / "AppData" / "Local" / _APP_NAME
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / _APP_NAME
    else:
        xdg = os.environ.get("XDG_CACHE_HOME", "")
        if xdg:
            return Path(xdg) / _APP_NAME
        return Path.home() / ".cache" / _APP_NAME


def _log_path() -> Path:
    override = os.environ.get(_BYPASS_LOG_ENV)
    if override:
        return Path(override)
    return _default_cache_base() / "bypass-audit.jsonl"


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
            encoding="utf-8",
            errors="replace",
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
        書き込み失敗は skip + stderr WARN（hook 本体の判定に影響しない。Issue #1999）。
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
    except (OSError, TypeError, ValueError) as exc:
        # disk full / permission denied / JSON エラー等でも hook の判定は変えない。
        # silent skip だと監査証跡の欠落に気づけないため WARN を stderr に出す（Issue #1999）。
        print(
            f"bypass-audit: WARN: 監査ログ書き込みに失敗しました ({out}): {exc}",
            file=sys.stderr,
        )
        return
