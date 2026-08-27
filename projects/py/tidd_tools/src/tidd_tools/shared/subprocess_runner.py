"""安全な subprocess ラッパー.

- `shell=False` を必須化（Windows と POSIX でクォート挙動が一致しない `shell=True` を避ける）
- リスト引数のみ受け付ける
- タイムアウト時は `SubprocessTimeoutError` を送出してプロセスをクリーンに終了する
- 環境変数は呼び出し側が明示する設計（暗黙的な汚染を避ける）
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from tidd_tools.shared.errors import SubprocessTimeoutError

logger = logging.getLogger(__name__)


def run(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
    capture: bool = True,
    encoding: str | None = "utf-8",
    errors: str | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run の安全なラッパー.

    Args:
        args: 実行するコマンドの引数リスト（`shell=True` 相当の文字列展開はしない）
        cwd: 作業ディレクトリ
        env: 子プロセスの環境変数。`None` のときは呼び出し時点の `os.environ`
            **スナップショット**を `dict` 化してから子に渡す（`subprocess.run(env=None)`
            の「OS に委ねる」挙動とは異なり、呼び出し後に親が `os.environ` を
            書き換えても子プロセスには反映されない点に注意）。
        timeout: タイムアウト秒。超過時に `SubprocessTimeoutError` を送出
        check: True なら非ゼロ終了で `subprocess.CalledProcessError` を送出
        capture: True なら stdout / stderr を文字列キャプチャ
        encoding: 文字列デコードに使う encoding。既定は `"utf-8"`（Windows ネイティブの
            既定ロケール（cp932 等）に依存させず、`gh`/`git` の UTF-8 出力を確実に
            デコードするため・Issue #3872）。呼び出し先がロケール依存の出力を返す
            場合（`cmd.exe` 等）は明示的に `encoding=None` を渡してロケール依存
            デコードにフォールバックできる。
        errors: 文字列デコードの `errors` ハンドラ（`"replace"` 等）。`None`（既定）は
            strict デコード（不正なバイト列は `UnicodeDecodeError` を送出）。
            外部コマンドの出力が指定 `encoding` と異なり得る場合（Issue #2982）に指定する。
        input: 子プロセスの stdin に渡す文字列（Issue #3040）。`ruff format -` の
            ような stdin 経由フィルタコマンドに対応するために追加。

    Returns:
        `subprocess.CompletedProcess[str]`

    Raises:
        SubprocessTimeoutError: timeout 超過時
        subprocess.CalledProcessError: check=True で非ゼロ終了時
    """
    if not isinstance(args, list):
        raise TypeError("args must be list[str]; pass list to avoid shell=True ambiguity")
    logger.debug("subprocess: %s (cwd=%s, timeout=%s)", args, cwd, timeout)
    effective_env: dict[str, str] = dict(env) if env is not None else dict(os.environ)
    try:
        return subprocess.run(
            args,
            cwd=str(cwd) if isinstance(cwd, Path) else cwd,
            env=effective_env,
            input=input,
            capture_output=capture,
            text=capture,
            encoding=encoding,
            errors=errors,
            check=check,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SubprocessTimeoutError(args, timeout if timeout is not None else 0.0) from exc
