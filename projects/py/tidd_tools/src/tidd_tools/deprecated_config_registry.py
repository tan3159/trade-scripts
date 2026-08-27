"""廃止済み設定キーのレジストリ（Issue #2531）.

廃止された env var や設定キーを一箇所に定義する。
`tidd health-check` / `tidd config show` で残存検知に使用する。

将来 #2534 が「ドキュメント走査」にも使う予定のため、
env var 名と移行先をデータとして取り出せる構造にする。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeprecatedEnvVar:
    """廃止済み環境変数のメタデータ."""

    env_var: str
    """廃止された環境変数名（例: "AI_REVIEW_CODEX_ENABLED"）."""

    deprecated_issue: str
    """廃止を決定した Issue 番号（例: "#2495"）."""

    replacement_key: str
    """移行先の config.json キー（例: "ai-review-codex"）."""

    migration_guide_url: str
    """移行手順を記述したドキュメントへのパス."""


# 廃止済み env var レジストリ
# キー: 環境変数名, 値: DeprecatedEnvVar インスタンス
DEPRECATED_ENV_VARS: dict[str, DeprecatedEnvVar] = {
    "AI_REVIEW_AGY_ENABLED": DeprecatedEnvVar(
        env_var="AI_REVIEW_AGY_ENABLED",
        deprecated_issue="#2495",
        replacement_key="ai-review-agy",
        migration_guide_url="docs/setup/ai-review-backend-migration.md",
    ),
    "AI_REVIEW_CODEX_ENABLED": DeprecatedEnvVar(
        env_var="AI_REVIEW_CODEX_ENABLED",
        deprecated_issue="#2495",
        replacement_key="ai-review-codex",
        migration_guide_url="docs/setup/ai-review-backend-migration.md",
    ),
    "SECRETS_BACKEND": DeprecatedEnvVar(
        env_var="SECRETS_BACKEND",
        deprecated_issue="#2496",
        replacement_key="secrets-backend",
        migration_guide_url="docs/setup/secrets-management.md",
    ),
}
