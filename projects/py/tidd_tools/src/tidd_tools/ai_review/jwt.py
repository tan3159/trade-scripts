"""GitHub App JWT 生成（旧 ai-review.sh ``get_installation_token`` 前半）.

旧 bash 実装では ``openssl dgst -sha256 -sign`` + ``base64`` を組み合わせて RS256 JWT を作っていた。
本モジュールでは PyJWT に置き換えてプラットフォーム差（``openssl`` の不在・``base64 -w0`` の挙動差）を排除する。

主要 API:
- :func:`generate_app_jwt` — APP ID と秘密鍵から RS256 JWT を 1 つ生成する
"""

from __future__ import annotations

import time

from tidd_tools.ai_review.errors import InvalidJwtKeyError

JWT_EXPIRATION_SECONDS = 600  # 旧 sh: now + 600（10 分）


def generate_app_jwt(
    app_id: str,
    private_key: str,
    *,
    now: int | None = None,
    expiration_seconds: int = JWT_EXPIRATION_SECONDS,
) -> str:
    """GitHub App 用の RS256 JWT を生成する.

    Args:
        app_id: GitHub App ID（``iss`` クレーム）
        private_key: PEM フォーマットの RSA 秘密鍵（PRIVATE_KEY_CONTENT または読み込んだ PEM ファイル）
        now: epoch 秒。指定時はテスト用にタイムスタンプを固定できる
        expiration_seconds: ``exp - iat`` の秒数（GitHub の上限は 10 分）

    Returns:
        圧縮済み JWT 文字列（``header.payload.signature``）

    Raises:
        InvalidJwtKeyError: PEM が壊れている / RSA 鍵として読めない場合
    """
    if not app_id:
        raise InvalidJwtKeyError("APP_ID is empty")
    if not private_key:
        raise InvalidJwtKeyError("private key is empty")

    # Issue #3892: PyJWT は起動コストが無視できないため、実際に JWT を生成する
    # 時点まで import を遅延する（cli.py の register() 経由で毎回読み込まれるのを防ぐ）。
    import jwt as pyjwt

    issued_at = int(now if now is not None else time.time())
    payload = {
        "iat": issued_at,
        "exp": issued_at + expiration_seconds,
        "iss": str(app_id),
    }
    try:
        return pyjwt.encode(payload, private_key, algorithm="RS256")
    # PyJWT の InvalidKeyError / 値エラー / 文字列バイト変換失敗をまとめて捕捉する
    except (pyjwt.exceptions.InvalidKeyError, ValueError, TypeError) as exc:
        raise InvalidJwtKeyError(str(exc)) from exc
