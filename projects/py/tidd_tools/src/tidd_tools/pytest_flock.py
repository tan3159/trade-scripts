"""`fcntl.flock` ベースの pytest フルスイート排他コンテキストマネージャ（Issue #2787）.

### 動機

`-n auto`（= CPU コア数ぶんのワーカー）が実行本数だけ掛け算されるとメモリを使い切る:
- 1 本 × 12 コア → 12 ワーカー（安定）
- 4 本 × 12 コア → 48 ワーカー（WSL2 メモリ枯渇）

### 解決策

`~/.cache/tidd/pytest-fullsuite.lock` を `fcntl.flock(LOCK_EX)` で
取得してから pytest を起動し、終了時に解放する。

### 設計上の保証

- プロセスが kill されても OS がロックを自動解放（stale lock 掃除不要）
- ロックファイル作成失敗時は pytest を起動せず異常終了（fail-closed）
- 待ち時間上限超過時は pytest を起動せず異常終了（fail-closed）
- Windows 実機ネイティブ（`sys.platform == "win32"`）では `msvcrt.locking()` で
  同等の排他ロックを取得する（Issue #4207）。`fcntl` も `msvcrt` も使えない
  環境（サンドボックス等）ではロックを利用できず異常終了する
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path
from types import ModuleType, TracebackType
from typing import IO

from tidd_tools.shared.paths import cache_dir as _cache_dir

#: デフォルトのロックファイルパス
DEFAULT_LOCK_PATH: Path = _cache_dir() / "pytest-fullsuite.lock"

#: デフォルトのロック待ち時間上限（秒）
DEFAULT_TIMEOUT: float = 600.0

#: ロック取得を再試行する間隔（秒）
_POLL_INTERVAL: float = 0.5


class PytestFlockContext:
    """pytest フルスイートの同時実行を fcntl.flock で直列化するコンテキストマネージャ.

    使い方::

        from tidd_tools.pytest_flock import PytestFlockContext

        with PytestFlockContext():
            subprocess.run(["uv", "run", "pytest", "-n", "auto"])

    `lock_path` を省略すると `~/.cache/tidd/pytest-fullsuite.lock` を使う。
    `timeout` 秒待ってもロックが取れない場合は `SystemExit(2)` で処理を停止する。
    `stderr` を省略すると `sys.stderr` に待機メッセージを出力する。
    """

    def __init__(
        self,
        *,
        lock_path: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        stderr: IO[str] | None = None,
    ) -> None:
        self._lock_path = lock_path if lock_path is not None else DEFAULT_LOCK_PATH
        self._timeout = timeout
        self._stderr = stderr if stderr is not None else sys.stderr
        self._fd: int | None = None
        self._lock_acquired: bool = False

    def __enter__(self) -> PytestFlockContext:
        self._acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._release()

    def _acquire(self) -> None:
        """ロックファイルを作成して排他ロックを取得する."""
        if sys.platform == "win32":
            self._acquire_windows()
            return

        self._acquire_posix()

    def _acquire_windows(self) -> None:
        """Windows 実機ネイティブ向けロック取得（`msvcrt.locking()` ベース・Issue #4207）.

        `fcntl.flock` に相当する非ブロッキング排他ロックプリミティブが `msvcrt` には
        存在しないため、1 バイト領域を `msvcrt.locking(fd, LK_NBLCK, 1)` で排他ロックする
        （`fcntl.flock(fd, LOCK_EX | LOCK_NB)` と等価な意味論）。ロック中は取得失敗の
        理由を問わず `OSError` を送出するため（fcntl の `BlockingIOError` に相当する
        専用の例外型がない）、取得失敗はすべて「待機して再試行」として扱う
        （Issue #4207 Gherkin Scenario 2: 別プロセス保持中は待機またはロック取得失敗）。

        `if sys.platform == "win32":` で本体を包むのは、mypy --strict
        （`platform = "linux"` 固定・Issue #4201）に `msvcrt` 参照を到達不能な
        コードとして型チェック対象から外させるため（Issue #3877 と同じ手法）。
        """
        if sys.platform == "win32":
            try:
                import msvcrt
            except ImportError:
                # 実 Windows では常に存在するはずだが、念のため fail-closed にする。
                self._warn("pytest ロックを利用できません（msvcrt 非対応環境）")
                raise SystemExit(2) from None

            try:
                self._lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o644)
                # msvcrt.locking はロック対象バイト領域が実ファイル内に存在する必要が
                # あるため、1 バイト書き込んでからロック対象位置（先頭）へ戻す。
                os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
            except OSError as exc:
                self._warn(f"pytest ロックを利用できません（ファイル作成失敗: {exc}）")
                raise SystemExit(2) from exc

            self._fd = fd

            # まず非ブロッキングで試みる（即取得できる場合はメッセージなし）
            if self._try_lock_windows(msvcrt, fd):
                self._lock_acquired = True
                self._info("pytest ロック取得")
                return

            # ロックが取れなかった → 待機ループに入る
            self._info("pytest ロック待機中（別の pytest フルスイートが実行中）")
            deadline = time.monotonic() + self._timeout
            while time.monotonic() < deadline:
                time.sleep(_POLL_INTERVAL)
                if self._try_lock_windows(msvcrt, fd):
                    self._lock_acquired = True
                    self._info("pytest ロック取得")
                    return
                elapsed = time.monotonic() - (deadline - self._timeout)
                self._info(f"pytest ロック待機中（{elapsed:.0f}s 経過）")

            # タイムアウト → ロックを諦めず pytest を起動しない
            self._warn(f"pytest ロック待機を打ち切り（{self._timeout:.0f}s 超過・pytest 未起動）")
            self._close_after_acquire_failure()
            raise SystemExit(2)

    @staticmethod
    def _try_lock_windows(msvcrt: ModuleType, fd: int) -> bool:
        """`msvcrt.locking()` による非ブロッキング取得を 1 回試みる.

        取得できれば ``True``、ロック中（またはその他の失敗）なら ``False`` を返す。
        """
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True
        return False  # pragma: no cover - 呼び出し元で保証済み

    def _acquire_posix(self) -> None:
        """POSIX（`fcntl.flock`）向けロック取得."""
        try:
            import fcntl
        except ImportError:
            # win32 以外でも fcntl が使えない環境（サンドボックス等）では
            # pytest を起動しない（Issue #3880）。
            self._warn("pytest ロックを利用できません（fcntl 非対応環境）")
            raise SystemExit(2) from None

        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            self._warn(f"pytest ロックを利用できません（ファイル作成失敗: {exc}）")
            raise SystemExit(2) from exc

        self._fd = fd

        # まず非ブロッキングで試みる（即取得できる場合はメッセージなし）
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_acquired = True
            self._info("pytest ロック取得")
            return
        except BlockingIOError:
            pass
        except OSError as exc:
            self._close_after_acquire_failure()
            self._warn(f"pytest ロックを利用できません（ロック取得失敗: {exc}）")
            raise SystemExit(2) from exc

        # ロックが取れなかった → 待機ループに入る
        self._info("pytest ロック待機中（別の pytest フルスイートが実行中）")
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_acquired = True
                self._info("pytest ロック取得")
                return
            except BlockingIOError:
                elapsed = time.monotonic() - (deadline - self._timeout)
                self._info(f"pytest ロック待機中（{elapsed:.0f}s 経過）")
            except OSError as exc:
                self._close_after_acquire_failure()
                self._warn(f"pytest ロックを利用できません（ロック取得失敗: {exc}）")
                raise SystemExit(2) from exc

        # タイムアウト → ロックを諦めず pytest を起動しない
        self._warn(f"pytest ロック待機を打ち切り（{self._timeout:.0f}s 超過・pytest 未起動）")
        self._close_after_acquire_failure()
        raise SystemExit(2)

    def _close_after_acquire_failure(self) -> None:
        """ロック取得前に終了するとき、保持していない fd だけを閉じる."""
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None
        self._lock_acquired = False

    def _release(self) -> None:
        """ロックを解放して fd を閉じる."""
        if self._fd is None:
            return
        if sys.platform == "win32":
            # Issue #4207: Windows 実機ネイティブでは _acquire_windows がロック取得成功時
            # に fd を設定するため、ここに到達しうる。msvcrt.locking() で明示的に解放する。
            try:
                import msvcrt
            except ImportError:
                pass
            else:
                try:
                    if self._lock_acquired:
                        os.lseek(self._fd, 0, os.SEEK_SET)
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        else:
            # fcntl 非対応環境（サンドボックス等）では _acquire_posix が fd を設定する前に
            # return するため、ここには到達しないはずだが、念のため実行時も分岐する。
            try:
                import fcntl
            except ImportError:
                pass
            else:
                try:
                    if self._lock_acquired:
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None
        self._lock_acquired = False

    def _info(self, message: str) -> None:
        print(message, file=self._stderr)

    def _warn(self, message: str) -> None:
        print(message, file=self._stderr)
