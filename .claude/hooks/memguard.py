#!/usr/bin/env python3
"""PreToolUse hook: WSL2 のメモリ枯渇によるセッション切断を事前検知してブロックする（Issue #2780）.

/proc/meminfo の MemAvailable と SwapFree を読んで閾値判定する。
閾値未満のときは exit 2 でツール実行をブロックし、stderr にコミット・/compact の指示を出力する。

exit code:
- 0: config.json で無効 / 空きメモリ十分 / /proc/meminfo 読めない / stdin 破損 → 素通り
- 2: MemAvailable または SwapFree が閾値未満 → ブロック

環境変数:
- MEMGUARD_LIMIT_MB: 閾値(MB)。デフォルト 800。
- MEMGUARD_MEMINFO_PATH: テスト用 /proc/meminfo パスの上書き（デフォルト /proc/meminfo）。

stdlib のみ使用（.claude/hooks/*.py の共通制約）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib.hook_io import is_hook_enabled, read_hook_input

# デフォルト閾値: 800MB
_DEFAULT_LIMIT_MB = 800

# /proc/meminfo のパス（テスト用に環境変数で上書き可能）
_MEMINFO_PATH = os.environ.get("MEMGUARD_MEMINFO_PATH", "/proc/meminfo")


def _read_meminfo() -> dict[str, int] | None:
    """/proc/meminfo を読んで kB 単位の値を dict で返す。読めなければ None。"""
    try:
        content = Path(_MEMINFO_PATH).read_text(encoding="utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return None
    result: dict[str, int] = {}
    for line in content.splitlines():
        # 例: "MemAvailable:   3145728 kB"
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(":")
            try:
                result[key] = int(parts[1])
            except ValueError:
                pass
    return result if result else None


def _get_limit_mb() -> int:
    """MEMGUARD_LIMIT_MB 環境変数から閾値 MB を取得する。不正値はデフォルトを返す。"""
    raw = os.environ.get("MEMGUARD_LIMIT_MB", "")
    if raw.strip().isdigit():
        return int(raw.strip())
    return _DEFAULT_LIMIT_MB


def _is_blocked(meminfo: dict[str, int], limit_mb: int) -> bool:
    """メモリ枯渇でブロックすべき状態かどうかを判定する。

    次のいずれかを満たすときブロックする。

    1. MemAvailable < limit
    2. SwapFree == 0 かつ SwapTotal > 0（swap が有効なのに使い切っている）

    2 は MemAvailable が limit 以上でもブロックする（Scenario 3:
    MemAvailable=900MB・limit=800MB・SwapFree=0MB → block）。swap 無効環境は
    常に SwapFree=0 になるため SwapTotal=0 を除外条件に置いている。
    """
    limit_kb = limit_mb * 1024
    # 両キーとも欠落時は limit_kb（＝ブロックしない値）にフォールバックする。
    # 0 を既定にすると MemAvailable 非対応カーネルで全ツール実行が止まるため。
    mem_available_kb = meminfo.get("MemAvailable", limit_kb)
    swap_free_kb = meminfo.get("SwapFree", limit_kb)

    # MemAvailable が閾値未満なら即ブロック
    if mem_available_kb < limit_kb:
        return True

    # SwapFree が 0（完全枯渇）のときはブロック（Scenario 3）
    # デフォルト閾値 800MB は MemAvailable 900MB には届かないが
    # SwapFree=0 なら swap も使えない危険な状態としてブロックする。
    # ただし swap 無効環境（SwapTotal=0）は常に SwapFree=0 になるため対象外にする。
    return swap_free_kb == 0 and meminfo.get("SwapTotal", 0) > 0


def main() -> int:
    # config.json の 'memguard' が false / 未設定なら no-op（default OFF・opt-in）
    if not is_hook_enabled("memguard"):
        return 0

    # Issue #2957: stdin 読み取りを hook_io.read_hook_input へ集約。
    # stdin 破損（読み取り失敗・空・非 JSON・非 dict）時は空 dict が返るため素通り（Scenario 6）。
    payload = read_hook_input(hook_name="PreToolUse")
    if not payload:
        return 0

    # /proc/meminfo が読めない環境は素通り（Scenario 5）
    meminfo = _read_meminfo()
    if meminfo is None:
        return 0

    limit_mb = _get_limit_mb()

    if _is_blocked(meminfo, limit_mb):
        mem_available_mb = meminfo.get("MemAvailable", 0) // 1024
        swap_free_mb = meminfo.get("SwapFree", 0) // 1024
        sys.stderr.write(
            f"MEMGUARD: 空きメモリが不足しています（"
            f"MemAvailable={mem_available_mb}MB, SwapFree={swap_free_mb}MB, 閾値={limit_mb}MB）。\n"
            "セッション切断を防ぐために重い処理を中断してください。\n"
            "推奨アクション:\n"
            "  1. 未コミットの変更をコミットする（git commit）\n"
            "  2. /compact でコンテキストを圧縮する\n"
            "  3. 必要であればセッションを再起動する\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
