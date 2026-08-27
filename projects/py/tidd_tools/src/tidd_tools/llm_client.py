"""OpenAI 互換 chat/completions API を呼び出す汎用 HTTP client (Issue #3116).

``ai-review`` の custom backend（``AI_REVIEW_BACKEND=custom``）から利用される。
社内 LLM ゲートウェイ等、任意の OpenAI 互換エンドポイントに対応するため、
接続先（URL・モデル名）はここには持たず呼び出し元から渡される。

公開 API:
- :func:`call_chat_completion` — ``POST {base_url}/chat/completions`` を呼び出し
  ``choices[0].message.content`` を文字列で返す
- :class:`LLMClientError` — 呼び出し失敗時の例外（``status_code`` 属性で HTTP
  ステータスコードを保持。接続失敗・タイムアウト・応答不備時は ``None``）
"""

from __future__ import annotations

import sys
from typing import Any

from tidd_tools.retry import retry_with_backoff

# custom backend の HTTP リクエストの最大リトライ回数（5xx・タイムアウト・接続失敗・
# 応答不備のみ対象。401/403/429 は即座に失敗として扱う ── リトライしても解決しないため）。
_MAX_RETRIES = 2


class LLMClientError(Exception):
    """LLM API 呼び出し失敗時の例外.

    Attributes:
        status_code: HTTP ステータスコード。接続失敗・タイムアウト・応答不備
            （``choices`` 欠落等）の場合は ``None``。
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _RetryableError(Exception):
    """内部リトライ対象の一時的エラー（5xx・タイムアウト・接続失敗・応答不備）."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _extract_content(data: object) -> str:
    """chat/completions レスポンス JSON から ``choices[0].message.content`` を抽出する.

    構造が期待と異なる場合は空文字列を返す（呼び出し元で応答不備として扱う）。
    """
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def call_chat_completion(
    *,
    base_url: str,
    model: str,
    api_key: str,
    prompt: str,
    timeout: float = 300,
    max_retries: int = _MAX_RETRIES,
) -> str:
    """OpenAI 互換 ``/chat/completions`` API を呼び出し応答テキストを返す.

    Args:
        base_url: API のベース URL（例: ``https://api.example.com/v1``）。末尾の
            ``/`` の有無は問わない。
        model: モデル名。
        api_key: ``Authorization: Bearer <api_key>`` ヘッダに使う値。
        prompt: ``messages[0].content`` に渡すプロンプト全文。
        timeout: リクエストタイムアウト秒数。
        max_retries: 5xx・タイムアウト・接続失敗・応答不備時の最大リトライ回数。

    Returns:
        ``choices[0].message.content`` の文字列。

    Raises:
        LLMClientError: HTTP 4xx/5xx・タイムアウト・接続失敗・``choices`` 欠落や
            空 content 等、応答が不正な場合。``status_code`` 属性に HTTP
            ステータスコード（接続失敗・タイムアウト・応答不備時は ``None``）を持つ。
    """
    # Issue #3892: httpx は起動コストが高い（rich/click/pygments を CLI extra 経由で
    # 巻き込む）ため、実際に custom backend を呼び出す時点まで import を遅延する。
    import httpx

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}

    def _request() -> str:
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise _RetryableError(f"LLM API がタイムアウトしました（{timeout}秒）: {exc}") from exc
        except httpx.TransportError as exc:
            raise _RetryableError(f"LLM API への接続に失敗しました: {exc}") from exc

        if response.status_code in (401, 403):
            raise LLMClientError(
                f"LLM API が認証エラーを返しました（HTTP {response.status_code}）",
                status_code=response.status_code,
            )
        if response.status_code == 429:
            raise LLMClientError(
                "LLM API がレート制限を返しました（HTTP 429）",
                status_code=429,
            )
        if response.status_code >= 500:
            raise _RetryableError(
                f"LLM API がサーバーエラーを返しました（HTTP {response.status_code}）",
                status_code=response.status_code,
            )
        if response.status_code != 200:
            raise LLMClientError(
                f"LLM API がエラーを返しました（HTTP {response.status_code}）",
                status_code=response.status_code,
            )

        try:
            data: Any = response.json()
        except ValueError as exc:
            raise _RetryableError(f"LLM API 応答の JSON パースに失敗しました: {exc}") from exc

        content = _extract_content(data)
        if not content:
            raise _RetryableError("LLM API 応答に choices[0].message.content が含まれていません")
        return content

    def _on_retry(attempt: int, wait: float, _result: Any, exc: BaseException | None) -> None:
        print(
            f"==> custom backend 呼び出しをリトライします（{attempt + 1}/{max_retries + 1} 回目失敗: {exc}）。"
            f" {wait} 秒待機します。",
            file=sys.stderr,
        )

    try:
        result: str = retry_with_backoff(
            _request,
            max_retries=max_retries,
            is_success=None,
            on_retry=_on_retry,
            retry_exceptions=(_RetryableError,),
        )
    except _RetryableError as exc:
        raise LLMClientError(str(exc), status_code=exc.status_code) from exc
    return result
