"""hook の stdin JSON 読み取りユーティリティ（stdlib のみ）.

Claude Code は PreToolUse / PostToolUse / Stop hook の stdin に JSON を渡す。
JSON 解析エラーや stdin 空のときは空 dict を返して hook 側で安全にスキップできるようにする。

Issue #1292: `read_hook_input()` に optional 引数 ``hook_name`` を追加。
指定時に ``_lib/schemas/{hook_name}.json`` で payload を検証し、
不一致なら stderr にエラーを出力して exit 2 で終了する。

Issue #1633: `get_hooks_config_path()` / `is_hook_enabled()` を追加。
~/.config/tidd_tools/hooks-config.json（OS ネイティブパス）で hook を個別 on/off できる。
stdlib のみ（platformdirs 不使用）。OS 判定 + 環境変数で同等パスを自前解決する。

Issue #2166: `is_hook_enabled()` の default を False に反転（opt-in 設計）。
安全系 3 hook（require-issue / block-dangerous-git / ban-claude-p）のみキー未設定時 True。
他の hook はキー未設定時 False（no-op）。consumer リポジトリが段階的に自動化を採用できるようにする。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def read_hook_input(hook_name: str | None = None) -> dict[str, Any]:
    """stdin から hook 入力 JSON を読み取って dict として返す.

    Args:
        hook_name: 指定時に ``_lib/schemas/{hook_name}.json`` で schema 検証する
            （Issue #1292）。未指定なら検証を skip する（既存 hook との後方互換）。

    Returns:
        payload dict。schema 検証失敗時は sys.exit(2) で終了する。

    - stdin が空・非 JSON の場合は空 dict を返す（hook 側でスキップ判定する）
    - 例外を投げないことで hook の信頼性を担保する
    """
    try:
        data = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not data.strip():
        return {}
    try:
        result = json.loads(data)
    except json.JSONDecodeError:
        return {}
    if not isinstance(result, dict):
        return {}

    # Issue #1292: hook_name 指定時は schema 検証を実施
    if hook_name:
        # `_lib/` を sys.path に追加して validate_payload を確実に import できるようにする。
        # 既存 hook は `sys.path.insert(0, hooks_dir)` してから `from _lib.hook_io import ...`
        # で呼ぶため、_lib 直下は自動で通らない場合がある。
        from pathlib import Path as _Path

        _lib_dir = _Path(__file__).resolve().parent
        if str(_lib_dir) not in sys.path:
            sys.path.insert(0, str(_lib_dir))
        try:
            from validate_payload import (  # type: ignore[import-not-found]
                PayloadValidationError,
                validate_payload,
            )
        except ImportError:
            # validate_payload が存在しない場合は skip（前方互換）
            return result
        try:
            validate_payload(result, hook_name)
        except PayloadValidationError as exc:
            sys.stderr.write(f"{exc}\n")
            sys.exit(2)
    return result


# ── Issue #1633: hook 機能別 on/off ──────────────────────────────────────────

#: キー未設定時に default True（有効）として扱う安全系 hook 名セット（Issue #2166）。
#: consumer リポジトリで copier copy 直後でも最低限の安全ガードが働くよう、
#: この 3 hook のみ default ON に維持する。他の hook は default OFF（opt-in）。
_SAFETY_HOOKS: frozenset[str] = frozenset(
    {
        "require-issue",
        "block-dangerous-git",
        "ban-claude-p",
    }
)


def get_hooks_config_path() -> str:
    """OS ネイティブ config ディレクトリの hooks-config.json パスを返す（stdlib のみ）.

    ADR 013 と同じ方針で OS を判定し、platformdirs 相当のパスを自前計算する。

    - Windows (sys.platform == "win32"):
        ``%APPDATA%\\tidd_tools\\hooks-config.json``
        (APPDATA 未設定なら HOME\\.config\\tidd_tools\\hooks-config.json にフォールバック)
    - その他 (Linux / macOS):
        ``${XDG_CONFIG_HOME}/tidd_tools/hooks-config.json``
        (XDG_CONFIG_HOME 未設定なら ${HOME}/.config/tidd_tools/hooks-config.json)

    Returns:
        設定ファイルのフルパス文字列（ファイルが存在するかは問わない）。
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or ""
        if appdata:
            return os.path.join(appdata, "tidd_tools", "hooks-config.json")
        # APPDATA 未設定フォールバック
        home = os.path.expanduser("~")
        return os.path.join(home, ".config", "tidd_tools", "hooks-config.json")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or ""
        if xdg:
            return os.path.join(xdg, "tidd_tools", "hooks-config.json")
        home = os.environ.get("HOME") or os.path.expanduser("~")
        return os.path.join(home, ".config", "tidd_tools", "hooks-config.json")


#: 文字列値を許可する hook 名 → 許可値セットのレジストリ（Issue #1994）
_STRING_VALUE_HOOKS: dict[str, frozenset[str]] = {
    "yaru-auto-tick": frozenset({"dry-run", "enabled"}),
}


def get_hook_config(hook_name: str, default: bool | str | None = True) -> bool | str | None:
    """hooks-config.json から hook の設定値を返す（Issue #1994・文字列値対応）.

    ``is_hook_enabled()`` の拡張版。bool だけでなく ``_STRING_VALUE_HOOKS`` に
    登録された許可文字列値（例: ``"yaru-auto-tick": "dry-run"``）も返せる。

    - 設定ファイルなし・キーなし → default
    - 不正 JSON → stderr に WARN を出して default
    - bool 値 → そのまま返す
    - 許可文字列値 → そのまま返す
    - 未知の文字列値・非対応型 → stderr に WARN を出して default（安全側）
    """
    config_path = get_hooks_config_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return default

    try:
        config = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write("WARN: hooks-config.json のパースに失敗しました（不正な JSON）。default を使用します。\n")
        return default

    if not isinstance(config, dict) or hook_name not in config:
        return default

    value = config[hook_name]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        allowed = _STRING_VALUE_HOOKS.get(hook_name)
        if allowed is not None and value in allowed:
            return value
        sys.stderr.write(
            f"WARN: hooks-config.json の '{hook_name}' に未知の値 '{value}' が設定されています。default を使用します。\n"
        )
        return default
    sys.stderr.write(
        f"WARN: hooks-config.json の '{hook_name}' の値型が不正です（{type(value).__name__}）。default を使用します。\n"
    )
    return default


def is_hook_enabled(hook_name: str) -> bool:
    """hooks-config.json を読んで hook が有効かどうかを返す（Issue #1633 / #2166）.

    **Issue #2166 変更: opt-in 設計に反転。**
    - 安全系 3 hook（_SAFETY_HOOKS）: 設定ファイルなし・キーなし → True（default ON）
    - 非安全系 hook: 設定ファイルなし・キーなし → False（default OFF、no-op）

    不正 JSON の場合は stderr に WARN を出し、安全系は True、非安全系は False を返す。
    安全系 hook が明示的に無効化されている場合は追加で WARN を出す。

    ``TIDD_HOOKS_ALL_ENABLED=1`` 環境変数が設定されている場合はすべての hook を有効とする
    （テスト・CI 環境で全 hook の動作を検証するために使用する）。

    Args:
        hook_name: hook の名前（拡張子なし、例: "require-issue"）。

    Returns:
        True なら hook を通常通り実行する。False なら hook を no-op で終了する。
    """
    # テスト・CI 環境: 全 hook を強制有効化（TIDD_HOOKS_ALL_ENABLED=1）
    if os.environ.get("TIDD_HOOKS_ALL_ENABLED") == "1":
        return True

    is_safety = hook_name in _SAFETY_HOOKS
    config_path = get_hooks_config_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = f.read()
    except (OSError, FileNotFoundError):
        # 設定ファイルが存在しない → 安全系は True、非安全系は False
        return is_safety

    try:
        config = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write("WARN: hooks-config.json のパースに失敗しました（不正な JSON）。hook を有効として扱います。\n")
        return is_safety

    if not isinstance(config, dict):
        return is_safety

    value = config.get(hook_name)
    if value is None:
        # キーが存在しない → 安全系は True、非安全系は False
        return is_safety

    enabled = bool(value)
    if not enabled:
        sys.stderr.write(f"hook '{hook_name}' is disabled (hooks-config.json).\n")
        if is_safety:
            sys.stderr.write(f"WARN: 安全系 hook '{hook_name}' が無効化されています。意図的な設定であることを確認してください。\n")
    return enabled


# ── Issue #1606: schema guard ─────────────────────────────────────────────────

#: 現行の Claude Code tool_response 実 schema キー一覧。
#: 未知キーのみで既知キーから値が取れない場合に WARN を出すための基準セット。
_KNOWN_TOOL_RESPONSE_KEYS: set[str] = {
    "stdout",
    "stderr",
    "interrupted",
    "isImage",
    "noOutputExpected",
    "output",  # 旧 schema（後方互換）
}


def get_tool_name(payload: dict[str, Any]) -> str:
    return str(payload.get("tool_name", ""))


def get_command(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return str(tool_input.get("command") or "")


def get_file_path(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return str(tool_input.get("file_path") or tool_input.get("path") or "")


def get_tool_output(payload: dict[str, Any]) -> str:
    """PostToolUse payload の ``tool_response`` から stdout 相当を取り出す (Issue #1596).

    Claude Code の実 Bash tool schema は ``tool_response`` に
    ``{"stdout", "stderr", "interrupted", "isImage", "noOutputExpected"}`` を返す。
    旧実装は ``output`` キーのみを読んでいたため、実 payload では常に空文字列となり、
    ``label-pr.py`` などの PostToolUse hook が 100% skip する silent skip 経路が
    できていた（2026-07-03 以降 merged 100 PR で size ラベル付与率 0/100 = 0%）。

    優先順位:
      1. ``tool_response.stdout``（実 Claude Code Bash tool schema）
      2. ``tool_response.output``（旧テスト・後方互換）

    どちらも欠落 or 空文字列なら空文字列を返す。呼び出し側は空文字列を skip 条件として
    扱ってよい。

    **schema guard（Issue #1606）:**
    ``tool_response.keys() - _KNOWN_TOOL_RESPONSE_KEYS`` が非空かつ既知キーから値を
    取れない場合、stderr に ``WARN: unknown tool_response schema: keys=[...]`` を出力する。
    ``SCHEMA_GUARD_STRICT=1`` 環境変数が設定されている場合は exit 2 でブロックする。
    """
    tool_response = payload.get("tool_response") or {}
    if not isinstance(tool_response, dict):
        return ""
    # 実 schema (stdout) を優先し、旧 schema (output) は fallback として残す。
    stdout = tool_response.get("stdout")
    if isinstance(stdout, str) and stdout:
        return stdout
    legacy = tool_response.get("output")
    if isinstance(legacy, str) and legacy:
        return legacy

    # 値が取れなかった場合のみ schema guard を発動する（Issue #1606）。
    unknown_keys = set(tool_response.keys()) - _KNOWN_TOOL_RESPONSE_KEYS
    if unknown_keys:
        sorted_keys = sorted(unknown_keys)
        warn_msg = f"WARN: unknown tool_response schema: keys={sorted_keys}\n"
        sys.stderr.write(warn_msg)
        if os.environ.get("SCHEMA_GUARD_STRICT") == "1":
            sys.exit(2)

    return str(tool_response.get("output") or "")
