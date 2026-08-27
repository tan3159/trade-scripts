"""pytest ワーカー数をメモリ量に応じて算出するユーティリティ（Issue #2784）.

### 動機

pytest-xdist の `-n auto` は CPU コア数だけを見てワーカー数を決めるため、
CPU に対してメモリが極端に少ない環境（例: CPU 12 コア / RAM 5 GB）では
OOM が発生してセッションごと WSL2 が落ちる。

### 算出規則

```
PYTEST_XDIST_AUTO_NUM_WORKERS が設定されていれば その値（1 以上の整数）を使う。
未設定なら min(CPU コア数, max(2, floor(MemAvailable / 512MB)))。
MemAvailable が取得できない場合は CPU コア数から算出したフォールバック値
min(CPU コア数, max(2, floor(CPU コア数 / 2))) を使う（Issue #3891）。
```

### Windows でのメモリ取得

Linux は `/proc/meminfo` の `MemAvailable` を読む。Windows は `/proc/meminfo` が
存在しないため `ctypes.windll.kernel32.GlobalMemoryStatusEx` で取得する
（Issue #3891）。取得に失敗した場合は上記のフォールバック値を使う。

### 使い方

```python
from tidd_tools.pytest_workers import calc_pytest_workers_from_system

workers = calc_pytest_workers_from_system()
cmd = ["pytest", "-n", str(workers)]
```
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import IO

#: ワーカー 1 本あたりのメモリ見積もり（バイト）
_BYTES_PER_WORKER = 512 * 1024 * 1024  # 512 MB

#: 最小ワーカー数
_MIN_WORKERS = 2

#: MemAvailable を読み取るファイルパス（Linux）
_MEMINFO_PATH = "/proc/meminfo"


class _MEMORYSTATUSEX(ctypes.Structure):
    """Windows API `GlobalMemoryStatusEx` が使う `MEMORYSTATUSEX` 構造体."""

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def _read_mem_available_bytes_windows() -> int | None:
    """Windows で `GlobalMemoryStatusEx` から利用可能メモリ量（バイト）を読み取る.

    API 呼び出し不能（`ctypes.windll` 非存在）・失敗（戻り値 0）の場合は None を返す。
    """
    try:
        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
        if not ok:
            return None
        return int(stat.ullAvailPhys)
    except (AttributeError, OSError, ValueError):
        return None


def _read_mem_available_bytes() -> int | None:
    """利用可能メモリ量（バイト）を読み取る.

    Linux は `/proc/meminfo` の `MemAvailable`、Windows は
    `GlobalMemoryStatusEx` から取得する。読み取り失敗
    （非対応 OS・権限不足・フォーマット異常）の場合は None を返す。
    """
    if sys.platform == "win32":
        return _read_mem_available_bytes_windows()

    try:
        with open(_MEMINFO_PATH, encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    # 例: "MemAvailable:   2097152 kB"
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024  # kB → bytes
    except (OSError, ValueError):
        pass
    return None


def calc_pytest_workers(
    *,
    cpu_count: int,
    mem_available_bytes: int | None,
    env: dict[str, str],
) -> int:
    """pytest ワーカー数を算出して返す（純粋関数）.

    Args:
        cpu_count: 利用可能な CPU コア数。
        mem_available_bytes: 利用可能なメモリ量（バイト）。
            None の場合（OSError 縮退・Windows でのメモリ取得失敗）は
            CPU コア数から算出したフォールバック値を返す（Issue #3891）。
        env: 環境変数の辞書。``PYTEST_XDIST_AUTO_NUM_WORKERS`` が含まれていれば
            その値を優先する（1 以上の整数でない場合は無視して算出値を使う）。

    Returns:
        採用するワーカー数（1 以上の整数）。
    """
    # 環境変数による上書き
    env_val = env.get("PYTEST_XDIST_AUTO_NUM_WORKERS")
    if env_val is not None:
        try:
            n = int(env_val)
            if n >= 1:
                return n
        except ValueError:
            pass
        # 無効値は無視して算出値にフォールバック

    # MemAvailable が取得できない場合は CPU コア数を考慮したフォールバック値にする
    # （_MIN_WORKERS 固定ではなく min(CPU コア数, max(2, floor(CPU コア数 / 2)))・Issue #3891）
    if mem_available_bytes is None:
        return min(cpu_count, max(_MIN_WORKERS, cpu_count // 2))

    # 算出: min(CPU コア数, max(2, floor(MemAvailable / 512MB)))
    mem_workers = max(_MIN_WORKERS, mem_available_bytes // _BYTES_PER_WORKER)
    return min(cpu_count, mem_workers)


def calc_pytest_workers_from_system(
    *,
    stderr: IO[str] | None = None,
) -> int:
    """実システムの CPU コア数・MemAvailable から pytest ワーカー数を算出して返す.

    算出したワーカー数を ``stderr`` に 1 行出力する。
    ``stderr`` を省略すると ``sys.stderr`` に出力する。

    環境変数 ``PYTEST_XDIST_AUTO_NUM_WORKERS`` が設定されていればその値を優先する。

    Returns:
        採用するワーカー数（1 以上の整数）。
    """
    out = stderr if stderr is not None else sys.stderr

    # 環境変数チェック（システム情報の取得前に短絡）
    env = dict(os.environ)
    env_val = env.get("PYTEST_XDIST_AUTO_NUM_WORKERS")
    if env_val is not None:
        try:
            n = int(env_val)
            if n >= 1:
                print(
                    f"pytest ワーカー数: {n} (PYTEST_XDIST_AUTO_NUM_WORKERS={env_val})",
                    file=out,
                )
                return n
        except ValueError:
            pass  # 無効値は無視して算出値にフォールバック

    # CPU コア数を取得
    try:
        cpu_count = os.cpu_count() or 1
    except NotImplementedError:
        cpu_count = 1

    # MemAvailable を取得
    mem_available = _read_mem_available_bytes()

    workers = calc_pytest_workers(
        cpu_count=cpu_count,
        mem_available_bytes=mem_available,
        env=env,
    )

    if mem_available is not None:
        mem_gb = mem_available / (1024**3)
        print(
            f"pytest ワーカー数: {workers} (cpu={cpu_count}, mem_available={mem_gb:.1f}GB, per_worker=512MB)",
            file=out,
        )
    else:
        print(
            f"pytest ワーカー数: {workers} (cpu={cpu_count}, mem_available=不明→CPU数ベースのフォールバック)",
            file=out,
        )

    return workers
