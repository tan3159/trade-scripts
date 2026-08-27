"""GitHub App installation token 取得 + 既存トークンフォールバック.

旧 ai-review.sh:
- ``get_installation_token`` — JWT を作って ``/app/installations/{id}/access_tokens`` を叩く（指数バックオフ 3 回）
- ``get_effective_review_token`` — GITHUB_TOKEN → GH_TOKEN → app_token → ``gh auth token`` の優先順位

Python 移植では :mod:`tidd_tools.ai_review.jwt` で生成した JWT を使い、urllib + リトライで叩く。
シークレットは **環境変数のみ**で解決する（Bitwarden フォールバックは廃止・Issue #3182/#3212）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from tidd_tools.ai_review.errors import InvalidJwtKeyError
from tidd_tools.ai_review.jwt import generate_app_jwt
from tidd_tools.retry import retry_with_backoff
from tidd_tools.shared.secrets import get_secret  # Issue #1712: credential は get_secret() 経由に統一
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)

GITHUB_INSTALLATION_TOKEN_URL = "https://api.github.com/app/installations/{id}/access_tokens"
DEFAULT_INSTALLATION_RETRY = 3


def load_mise_env() -> bool:
    """mise の解決済み ``[env]`` を未設定のプロセス環境へ反映する（Issue #4092）.

    Claude Code の Bash のような非対話シェルでは mise の activate hook が
    実行されないため、``mise env --json`` を明示的に呼び出す。既に export
    されている値は上書きせず、mise 未導入・未信頼・不正な出力の場合は
    既存の環境変数フォールバックを使えるように何もしない。
    """
    if shutil.which("mise") is None:
        return False
    try:
        result = run_subprocess(["mise", "env", "--json"], capture=True)
        if result.returncode != 0:
            return False
        resolved = json.loads(result.stdout)
    except (OSError, TypeError, ValueError):
        return False
    if not isinstance(resolved, dict):
        return False
    loaded = False
    for name, value in resolved.items():
        if isinstance(name, str) and isinstance(value, str) and not os.environ.get(name):
            os.environ[name] = value
            loaded = True
    return loaded


def load_ai_review_secrets(
    *,
    home: Path | None = None,
    repo_root: Path | None = None,
    is_private: bool | None = None,
) -> dict[str, str]:
    """ai-review 用 secrets を **環境変数のみ**から読み込む（Issue #3212）.

    旧 ai-review.sh は ``~/.ai-reviewer.sh`` の source（Bitwarden 呼び出し）で
    APP_ID / INSTALLATION_ID / PRIVATE_KEY_CONTENT / GH_TOKEN 等を環境変数に
    設定していたが、Bitwarden フォールバックは廃止された（#3182/#3212）。
    以降は環境変数（.envrc / CI secrets / export 等）のみを使用する。

    Args:
        home / repo_root / is_private: 旧 Bitwarden 経路の引数（互換性のため残すが未使用）。

    Returns:
        ``os.environ`` に存在する ai-review 用 secrets 変数の dict。
    """
    _ = (home, repo_root, is_private)  # 旧 bw 経路撤去に伴い未使用（#3212）
    required = ("APP_ID", "INSTALLATION_ID", "GH_TOKEN")
    key_vars = ("PRIVATE_KEY_CONTENT", "PRIVATE_KEY_PATH")
    return {var: os.environ[var] for var in (*required, *key_vars) if os.environ.get(var)}


def _load_private_key() -> str | None:
    """``PRIVATE_KEY_CONTENT`` または ``PRIVATE_KEY_PATH`` から PEM を読み込む."""
    content = os.environ.get("PRIVATE_KEY_CONTENT")
    if content:
        return content
    path = os.environ.get("PRIVATE_KEY_PATH") or str(Path.home() / ".ssh" / "ai-reviewer-private-key.pem")
    pem_path = Path(path)
    if not pem_path.is_file():
        return None
    try:
        return pem_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("PEM 読み込み失敗: %s (%s)", pem_path, exc)
        return None


def get_installation_token(
    *,
    app_id: str | None = None,
    installation_id: str | None = None,
    private_key: str | None = None,
    max_retries: int = DEFAULT_INSTALLATION_RETRY,
) -> str:
    """GitHub App installation token を取得する.

    旧 bash:
    - ``APP_ID`` / ``INSTALLATION_ID`` / 秘密鍵（``PRIVATE_KEY_CONTENT`` または ``PRIVATE_KEY_PATH``）
    - いずれかが欠けたら WARN を stderr に出して空文字を返す（呼び出し元はフォールバックする）
    - 指数バックオフで 3 回リトライ（``1s -> 2s -> 4s``）

    Returns:
        取得できた場合はトークン文字列、失敗時は空文字
    """
    # Issue #1712: os.environ 直接読みを get_secret() 経由に統一（env のみ・#3212）
    # RuntimeError を受け取ったら WARN を stderr に出して再 raise する。
    if not app_id:
        try:
            app_id = get_secret("APP_ID")
        except RuntimeError as exc:
            print(f"WARN: APP_ID を取得できませんでした（{exc}）。APP_TOKEN なしで実行します。", file=sys.stderr)
            raise
    if not installation_id:
        try:
            installation_id = get_secret("INSTALLATION_ID")
        except RuntimeError as exc:
            print(
                f"WARN: INSTALLATION_ID を取得できませんでした（{exc}）。APP_TOKEN なしで実行します。", file=sys.stderr
            )
            raise
    private_key = private_key if private_key is not None else _load_private_key()

    if not app_id or not installation_id or not private_key:
        print("WARN: GitHub App の設定が不完全です。APP_TOKEN なしで実行します。", file=sys.stderr)
        return ""

    try:
        jwt_token = generate_app_jwt(app_id, private_key)
    except InvalidJwtKeyError as exc:
        print(
            f"WARN: GitHub App JWT 生成に失敗しました（{exc}）。APP_TOKEN なしで実行します。",
            file=sys.stderr,
        )
        return ""

    url = GITHUB_INSTALLATION_TOKEN_URL.format(id=installation_id)

    def _fetch_token() -> str:
        req = urllib.request.Request(  # noqa: S310 — URL は固定 GitHub API
            url,
            method="POST",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "tidd-tools-ai-review",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
            token = payload.get("token", "")
            if token:
                return str(token)
            raise RuntimeError(f"no token field in response: {payload!r}")

    def _on_retry(attempt: int, wait: float, result: object, exc: BaseException | None) -> None:
        print(
            f"WARN: installation token の取得に失敗しました。{wait}s 後にリトライします"
            f"（{attempt + 1}/{max_retries}）。",
            file=sys.stderr,
        )

    def _on_exhausted(last_result: object, last_exc: BaseException | None) -> str:
        print(
            f"WARN: installation token の取得に失敗しました（最大リトライ回数 {max_retries} 回到達）。"
            "APP_TOKEN なしで実行します。",
            file=sys.stderr,
        )
        if last_exc is not None:
            logger.debug("installation token last exception: %r", last_exc)
        return ""

    token_result: str = retry_with_backoff(
        _fetch_token,
        max_retries=max_retries,
        is_success=None,
        on_retry=_on_retry,
        on_exhausted=_on_exhausted,
        retry_exceptions=(urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError, RuntimeError),
    )
    return token_result


def get_non_interactive_fallback_token() -> str:
    """bot 専用投稿経路向けの限定フォールバックトークンを返す（Issue #3181）.

    ``get_installation_token()``（GitHub App token）取得失敗時に
    ``post_comment`` / ``post_primary_review_comment`` / ``continue_with_verdict`` が使う。
    ``get_effective_review_token()`` の優先順位から「引数 ``app_token``」と
    「``gh auth token`` フォールバック」を除いた版で、``$GITHUB_TOKEN`` → ``$GH_TOKEN`` の
    CI・自動化用に明示設定される非対話トークンのみを見る。

    決定（docs/decisions/2026-08-05-issue-3181-token-fallback-scope.md）:
    bot ↔ 個人の権限境界を跨ぐフォールバック（``gh auth token`` は人間の対話的セッション）は
    行わない。Bitwarden 経由の取得も行わない（App token 取得失敗の原因が Bitwarden 障害
    であるケースをこのフォールバックでも再度踏まないようにするため）。

    優先順位:
    1. ``$GITHUB_TOKEN``
    2. ``$GH_TOKEN``

    Returns:
        取得できた場合はトークン文字列、どちらも未設定の場合は空文字。
    """
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if github_token.strip():
        return github_token
    gh_token = os.environ.get("GH_TOKEN", "")
    if gh_token.strip():
        return gh_token
    return ""


def get_effective_review_token(app_token: str = "") -> str:
    """gh CLI に渡す GH_TOKEN として有効なトークンを決める.

    優先順位（旧 sh ``get_effective_review_token``）:
    1. ``$GITHUB_TOKEN``
    2. ``$GH_TOKEN``
    3. 引数 ``app_token``（``get_installation_token`` 戻り値）
    4. ``gh auth token`` フォールバック
    """
    # Issue #1712: os.environ 直接読みを get_secret() 経由に統一（env のみ・#3212）
    try:
        env_github = get_secret("GITHUB_TOKEN")
        if env_github:
            return env_github
    except RuntimeError:
        pass
    try:
        env_gh = get_secret("GH_TOKEN")
        if env_gh:
            return env_gh
    except RuntimeError:
        pass
    if app_token:
        return app_token

    try:
        result = run_subprocess(["gh", "auth", "token"], capture=True)
    except FileNotFoundError:
        return ""
    if result.returncode == 0:
        out = result.stdout.strip()
        if out:
            return out
    return ""
