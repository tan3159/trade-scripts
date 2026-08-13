#!/usr/bin/env python3
"""SessionStart hook: CLAUDE.local.md 等のサイズ超過を stdout に WARN/BLOCK する（Issue #3700）.

copier consumer（例: mn-scripts）では git 管理外の `CLAUDE.local.md` が毎セッション
自動ロードされる。手動追記を続けると肥大化し、セッションごとのトークン消費が累積する。
本 hook は repo root の自動ロード対象ファイルを検査し、config.json で設定した閾値
超過を stdout に通知して「参照時読み docs への移行」を促す（削除はしない）。

`session-start-memory-warn.py` / `session-start-cache.py` と同様に **ブロックしない・
exit 0**。機能キー `session-start-claude-local-md-warn` で on/off できる
（default OFF・opt-in）。

**warn/block 二段階（#3700 設計判断）:**
- `claude-local-md-warn-warn-bytes`（default 2048）超過 → WARN メッセージ
  （移行を「検討」）
- `claude-local-md-warn-block-bytes`（default 4096）超過 → BLOCK メッセージ
  （移行を「推奨」。ラベルが BLOCK でも非ブロッキング・exit 0）

閾値は consumer 側で `~/.config/tidd_tools/config.json`（git 管理外）のキーで上書きできる。

stdlib のみ使用（uv run オーバーヘッドを避ける・detect-rule-bloat.py と同じ方針）。

テスト用環境変数:
  - CLAUDE_LOCAL_MD_WARN_REPO_ROOT: 検査対象 repo root を上書き（git 解決をスキップ）
  - XDG_CONFIG_HOME: config.json の場所を上書き（hook_io.get_hooks_config_path 経由）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import get_hooks_config_path, is_hook_enabled

_HOOK_NAME = "session-start-claude-local-md-warn"

#: 自動ロードされる git 管理外ファイルの候補（repo root 直下）
_LOCAL_MD_FILENAMES = ("CLAUDE.local.md", "AGENTS.local.md")

_DEFAULT_WARN_BYTES = 2048
_DEFAULT_BLOCK_BYTES = 4096

_WARN_BYTES_KEY = "claude-local-md-warn-warn-bytes"
_BLOCK_BYTES_KEY = "claude-local-md-warn-block-bytes"


def _resolve_repo_root(override: str | None) -> Path | None:
    """検査対象 repo root を返す（git root を解決できない場合は None）.

    `CLAUDE_LOCAL_MD_WARN_REPO_ROOT` が設定されている場合は git 解決をスキップする
    （テスト用）。git root を解決できない場合・CWD がリポジトリ外の場合は silent skip。
    """
    if override:
        return Path(override)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip())


def _load_config_dict() -> dict[str, Any]:
    """`~/.config/tidd_tools/config.json`（git 管理外）を dict で返す.

    ファイル不在・不正 JSON・非 dict の場合は空 dict（default 閾値にフォールバック）。
    """
    config_path = get_hooks_config_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _read_threshold(config: dict[str, Any], key: str, default: int) -> int:
    """config.json から整数閾値を読む。非 int（bool 含む）・欠如時は default.

    JSON の `true`/`false` は `isinstance(value, int)` を満たすため `type(value) is int`
    で厳密判定する（bool を閾値として受理しない）。
    """
    value = config.get(key)
    if type(value) is int:
        return value
    return default


def _check_local_md_files(repo_root: Path, warn_bytes: int, block_bytes: int) -> None:
    """repo root の自動ロード対象ファイルを検査し、閾値超過を stdout に通知する."""
    for name in _LOCAL_MD_FILENAMES:
        path = repo_root / name
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > block_bytes:
            sys.stdout.write(
                f"{_HOOK_NAME}: BLOCK: {name} が {size} bytes"
                f"（{_BLOCK_BYTES_KEY} {block_bytes} 超過）。"
                "参照時読み docs（例: docs/wsl-environment.md）への移行を推奨します。\n"
            )
        elif size > warn_bytes:
            sys.stdout.write(
                f"{_HOOK_NAME}: WARN: {name} が {size} bytes"
                f"（{_WARN_BYTES_KEY} {warn_bytes} 超過）。"
                "参照時読み docs への移行を検討してください。\n"
            )


def _run_warn(repo_root: Path) -> int:
    """CLAUDE.local.md 等のサイズ検査を実行する（常に exit 0・非ブロッキング）."""
    config = _load_config_dict()
    warn_bytes = _read_threshold(config, _WARN_BYTES_KEY, _DEFAULT_WARN_BYTES)
    block_bytes = _read_threshold(config, _BLOCK_BYTES_KEY, _DEFAULT_BLOCK_BYTES)
    _check_local_md_files(repo_root, warn_bytes, block_bytes)
    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off（default OFF・opt-in）
    if not is_hook_enabled(_HOOK_NAME):
        return 0

    override = os.environ.get("CLAUDE_LOCAL_MD_WARN_REPO_ROOT")
    repo_root = _resolve_repo_root(override)
    if repo_root is None:
        return 0
    return _run_warn(repo_root)


if __name__ == "__main__":
    sys.exit(main())
