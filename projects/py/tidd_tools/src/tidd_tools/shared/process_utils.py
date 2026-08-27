"""クロスプラットフォーム対応のプロセス生存確認（Issue #3880）.

POSIX では `os.kill(pid, 0)`（シグナルを送らずプロセス存在確認のみ行う慣用パターン）が
使えるが、Windows の `os.kill` はシグナル 0 をサポートせず `SystemError` を送出する
（stdlib 制約: Windows で `os.kill` が受け付けるのは `CTRL_C_EVENT`・`CTRL_BREAK_EVENT`・
`signal.SIGTERM` のみ）。stdlib のみで完結させるため、Windows では `ctypes` 経由で
`OpenProcess`（Win32 API）のハンドル取得可否によりプロセス存在を判定する。
"""

from __future__ import annotations

import os
import sys

#: PROCESS_QUERY_LIMITED_INFORMATION（Win32 API）: プロセス存在確認のみに必要な最小権限
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def is_pid_alive(pid: int) -> bool:
    """`pid` のプロセスが生存していれば `True`、存在しなければ `False` を返す.

    シグナルを送信せず存在確認のみ行う（POSIX の `os.kill(pid, 0)` 慣用パターンと
    同等の意味論）。権限不足でプロセスの詳細を取得できない場合も「プロセス自体は
    存在する」とみなし `True` を返す。
    """
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        return True

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # プロセスは存在するが権限不足でシグナル送信できない = 生存している
        return True
    return True
