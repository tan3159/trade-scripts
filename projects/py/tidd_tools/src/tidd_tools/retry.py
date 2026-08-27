"""指数バックオフ付きリトライ共通ユーティリティ (#2567).

stdlib のみを使用する。tenacity 等の依存は追加しない。

公開 API:
- :func:`retry_with_backoff` — 述語ベース / 例外ベースの両方に対応する汎用リトライ
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any


def retry_with_backoff(
    operation: Callable[[], Any],
    *,
    max_retries: int,
    is_success: Callable[[Any], bool] | None,
    on_retry: Callable[[int, float, Any, BaseException | None], None],
    on_exhausted: Callable[[Any, BaseException | None], Any] | None = None,
    retry_exceptions: Sequence[type[BaseException]] | None = None,
    sleep_func: Callable[[float], None] | None = None,
    initial_wait: float = 1,
) -> Any:
    """指数バックオフ付きで operation をリトライする.

    Args:
        operation: リトライ対象の呼び出し可能オブジェクト。引数なしで呼ぶ。
        max_retries: 最大リトライ回数。計 max_retries+1 回試行する。
        is_success: 述語ベース判定。None のとき例外ベース（retry_exceptions）モードで動作する。
        on_retry: リトライ直前に呼ばれるコールバック。
            引数: (attempt: int, wait: float, result: Any, exc: BaseException | None)
        on_exhausted: 最大到達時に呼ばれるコールバック。
            引数: (last_result: Any, last_exc: BaseException | None)
            返り値がそのまま返される。None の場合は最後の結果を返すか例外を raise する。
        retry_exceptions: 例外ベースモード時にリトライ対象とする例外クラスのシーケンス。
        sleep_func: テスト注入用の sleep 実装。None のとき time.sleep を使う。
        initial_wait: 初回 sleep 秒数。以後 2 倍ずつ増加する。

    Returns:
        成功時は operation の返り値。
        on_exhausted が指定されている場合はその返り値。
        例外ベースかつ on_exhausted=None のとき最後の例外を raise する。
        述語ベースかつ on_exhausted=None のとき最後の結果を返す。
    """
    sleep_impl = sleep_func if sleep_func is not None else time.sleep
    exc_types: tuple[type[BaseException], ...] = tuple(retry_exceptions) if retry_exceptions else ()

    wait = initial_wait
    last_result: Any = None
    last_exc: BaseException | None = None

    for attempt in range(max_retries + 1):
        try:
            result = operation()
            last_result = result
            last_exc = None
        except BaseException as exc:
            if exc_types and isinstance(exc, exc_types):
                last_exc = exc
                last_result = None
                if attempt < max_retries:
                    on_retry(attempt, wait, None, exc)
                    sleep_impl(wait)
                    wait *= 2
                continue
            raise

        # 述語ベース: is_success が None のときはここに到達しない（例外ベース専用）
        if is_success is None or is_success(result):
            return result

        # 述語ベース: 失敗
        if attempt < max_retries:
            on_retry(attempt, wait, result, None)
            sleep_impl(wait)
            wait *= 2

    # 最大到達
    if on_exhausted is not None:
        return on_exhausted(last_result, last_exc)

    # 例外ベースで on_exhausted=None: 最後の例外を raise
    if last_exc is not None:
        raise last_exc

    # 述語ベースで on_exhausted=None: 最後の結果を返す
    return last_result
