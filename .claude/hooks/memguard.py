#!/usr/bin/env python3
"""PreToolUse hook: WSL2 のメモリ枯渇によるセッション切断を事前検知してブロックする（Issue #2780）.

/proc/meminfo の MemAvailable と SwapFree を読んで閾値判定する。
閾値未満のときは exit 2 でツール実行をブロックし、stderr に発火分岐・実効閾値・
/compact 優先の推奨アクションを出力する（Issue #4156）。

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

# SwapFree==0 判定のマージン倍率（Issue #4031）
# swap が恒常的にほぼ使い切られた状態（SwapFree≈0MB）が定常状態の環境（WSL2 等）では、
# SwapFree==0 のみで無条件ブロックすると MemAvailable が健全でも false positive になる。
# MemAvailable が「閾値 × マージン」未満のときに限り SwapFree==0 をブロック条件として扱う。
_SWAP_EXHAUSTED_MARGIN = 3

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


def _classify_block(meminfo: dict[str, int], limit_mb: int) -> tuple[bool, int, str]:
    """メモリ枯渇でブロックすべき状態かどうかを、発火分岐の情報つきで判定する（Issue #4156）。

    戻り値は `(blocked, effective_threshold_mb, reason)`。

    次のいずれかを満たすときブロックする。

    1. MemAvailable < limit → reason="mem_available"、実効閾値は limit そのもの
    2. SwapFree == 0 かつ SwapTotal > 0（swap が有効なのに使い切っている）かつ
       MemAvailable < limit * _SWAP_EXHAUSTED_MARGIN（Issue #4031）
       → reason="swap_exhausted"、実効閾値は limit * _SWAP_EXHAUSTED_MARGIN

    2 は MemAvailable が limit 以上でもブロックしうるが（例:
    MemAvailable=900MB・limit=800MB・SwapFree=0MB → block）、MemAvailable が
    limit の _SWAP_EXHAUSTED_MARGIN 倍以上（健全）ならブロックしない。
    swap が恒常的にほぼ使い切られた状態が定常状態の環境（WSL2 等）で
    SwapFree==0 のみを危険信号として扱うと false positive になるため（#4031）。
    swap 無効環境は常に SwapFree=0 になるため SwapTotal=0 を除外条件に置いている。

    ブロックしない場合も `effective_threshold_mb` は limit を返す（未使用値）。
    """
    limit_kb = limit_mb * 1024
    # 両キーとも欠落時は limit_kb（＝ブロックしない値）にフォールバックする。
    # 0 を既定にすると MemAvailable 非対応カーネルで全ツール実行が止まるため。
    mem_available_kb = meminfo.get("MemAvailable", limit_kb)
    swap_free_kb = meminfo.get("SwapFree", limit_kb)

    # MemAvailable が閾値未満なら即ブロック（分岐1）
    if mem_available_kb < limit_kb:
        return True, limit_mb, "mem_available"

    # SwapFree が 0（完全枯渇）かつ MemAvailable がマージン未満のときはブロックする（分岐2）。
    # ただし swap 無効環境（SwapTotal=0）は常に SwapFree=0 になるため対象外にする。
    if swap_free_kb == 0 and meminfo.get("SwapTotal", 0) > 0:
        effective_limit_mb = limit_mb * _SWAP_EXHAUSTED_MARGIN
        if mem_available_kb < effective_limit_mb * 1024:
            return True, effective_limit_mb, "swap_exhausted"

    return False, limit_mb, "mem_available"


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

    blocked, effective_threshold_mb, reason = _classify_block(meminfo, limit_mb)
    if blocked:
        mem_available_mb = meminfo.get("MemAvailable", 0) // 1024
        swap_free_mb = meminfo.get("SwapFree", 0) // 1024
        if reason == "swap_exhausted":
            reason_line = (
                f"SwapFree が枯渇（0MB）しているため発火しました"
                f"（実効閾値={effective_threshold_mb}MB = 基準閾値{limit_mb}MB "
                f"× マージン{_SWAP_EXHAUSTED_MARGIN}・#4031）。\n"
            )
        else:
            reason_line = f"MemAvailable が実効閾値={effective_threshold_mb}MB を下回ったため発火しました。\n"
        sys.stderr.write(
            f"MEMGUARD: 空きメモリが不足しています（"
            f"MemAvailable={mem_available_mb}MB, SwapFree={swap_free_mb}MB, "
            f"実効閾値={effective_threshold_mb}MB）。\n"
            f"{reason_line}"
            "セッション切断を防ぐために重い処理を中断してください。\n"
            "推奨アクション:\n"
            "  1. /compact でコンテキストを圧縮する（このセッション内で完結する第一手）\n"
            "  2. git commit で変更を退避する（※本 hook が Bash も含め全ツールをブロックする"
            "ため、このメッセージが出ている間は実行できません。空き容量が戻ってから実行する）\n"
            "  3. 必要であればセッションを再起動する（リモートセッションでは選択できない場合がある）\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
