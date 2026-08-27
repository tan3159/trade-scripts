"""秘匿情報の共通解決レイヤー（Issue #1630）.

``get_secret(name)`` が公開 API。**環境変数のみ**で解決する。

Issue #3182 の決定（docs/decisions/2026-08-05-secrets-backend-env-vs-bw.md）により
Bitwarden CLI 経由の解決（``secrets-backend=bw``・``bw_item``）は廃止された
（#3212）。``~/.config/tidd_tools/config.json`` の旧 ``"secrets-backend"`` キーは
無視される（読み込み時に非推奨 WARN を出す）。旧 ``SECRETS_BACKEND`` 環境変数も
廃止（後方互換なし・Issue #2496）。
"""

from __future__ import annotations

import json
import os
import sys


def _config_path() -> str:
    """config.json のパスを返す（`_warn_deprecated_secrets_backend` 用）."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or ""
        if appdata:
            return os.path.join(appdata, "tidd_tools", "config.json")
        home = os.environ.get("HOME") or os.path.expanduser("~")
        return os.path.join(home, ".config", "tidd_tools", "config.json")
    xdg = os.environ.get("XDG_CONFIG_HOME") or ""
    if xdg:
        return os.path.join(xdg, "tidd_tools", "config.json")
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".config", "tidd_tools", "config.json")


_DEPRECATED_KEY_WARNED = False


def _warn_deprecated_secrets_backend() -> None:
    """config.json に旧 ``secrets-backend`` キーがあれば非推奨 WARN を出す（#3212）.

    env 方式一本化のためキーは無視される。複数回の WARN を避けるため
    モジュール内フラグで 1 回だけ出力する。
    """
    global _DEPRECATED_KEY_WARNED
    if _DEPRECATED_KEY_WARNED:
        return
    try:
        with open(_config_path(), encoding="utf-8") as f:
            raw = f.read()
        config = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(config, dict) and "secrets-backend" in config:
        print(
            "WARN: config.json の 'secrets-backend' キーは廃止されました（#3212）。"
            "シークレットは環境変数でのみ解決されます。",
            file=sys.stderr,
        )
    _DEPRECATED_KEY_WARNED = True


def get_secret(name: str) -> str:
    """秘匿情報を解決して返す.

    解決順序:
    1. 環境変数 ``name`` に実質的な値（非空・非空白）があればそれを返す
    2. ない場合は ``RuntimeError`` を raise する（Bitwarden フォールバックは廃止・#3212）

    Args:
        name: 解決する環境変数名（例: ``"ANTHROPIC_API_KEY"``）

    Returns:
        解決された秘匿情報の文字列。

    Raises:
        RuntimeError: 環境変数が設定されていない場合。
            エラーメッセージに ``name`` と env 設定手順のヒントを含む。
    """
    # Step 1: 環境変数に実質的な値があればそれを返す
    env_value = os.environ.get(name, "")
    if env_value and env_value.strip():
        return env_value

    # 旧 secrets-backend キーの非推奨 WARN（env 方式一本化・#3212）
    _warn_deprecated_secrets_backend()

    # Step 2: 取得できなかったので RuntimeError（env 設定を促す）
    raise RuntimeError(f"環境変数 {name} を設定してください（.envrc / CI secrets / export 等）")
