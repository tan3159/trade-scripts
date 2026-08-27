"""非実装 subagent routing の role registry データ定義とアクセサ (Issue #4191).

Issue #4185（Epic）「起動元に応じて非実装 subagent の委譲先とモデルを選択する」の
分割サブIssue。issue-writer・duplicate-detector・ai-confirm-verifier・
post-merge-verifier・yaru-verifier・issue-next オーケストレーションの各 role について、
Claude Code / Codex それぞれの native mechanism（`Agent tool`・`spawn_agent`・
`Skill tool`）・agent_type（`.claude/agents/*.md` / `.codex/agents/*.toml` の識別子）・
model を保持する。

resolver 本体（起動元の解決・未登録起動元/対象外 role の異常系・exit code 契約）は
Issue #4192 で実装する。実装担当 issue-implementer / issue-fixer の起動元別委譲設定は
Issue #4181（`resolve_issue_next_agent.py`）の責務であり本 registry の対象外。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RoleLauncherConfig:
    """role・起動元の組み合わせごとの起動設定."""

    native_mechanism: str
    """起動元ネイティブの subagent 起動手段（例: "Agent tool"・"spawn_agent"・"Skill tool"）."""

    agent_type: str | None
    """`.claude/agents/*.md` / `.codex/agents/*.toml` の識別子。起動元の既定ロールを使う場合は None."""

    model: str | None
    """明示指定するモデル名。起動元の既定モデルを継承する場合は None."""


# role・起動元をキーとする registry データ。
# 外側キー: `.claude/agents/*.md` の agent 名（"issue-next" はオーケストレーション自体を表す）。
# 内側キー: "claude_code" / "codex"。
_ROLE_REGISTRY: dict[str, dict[str, RoleLauncherConfig]] = {
    "issue-writer": {
        "claude_code": RoleLauncherConfig(native_mechanism="Agent tool", agent_type="issue-writer", model="sonnet"),
        "codex": RoleLauncherConfig(native_mechanism="spawn_agent", agent_type="issue_writer", model=None),
    },
    "duplicate-detector": {
        "claude_code": RoleLauncherConfig(
            native_mechanism="Agent tool", agent_type="duplicate-detector", model="sonnet"
        ),
        "codex": RoleLauncherConfig(native_mechanism="spawn_agent", agent_type="duplicate_detector", model=None),
    },
    "ai-confirm-verifier": {
        "claude_code": RoleLauncherConfig(
            native_mechanism="Agent tool", agent_type="ai-confirm-verifier", model="sonnet"
        ),
        "codex": RoleLauncherConfig(native_mechanism="spawn_agent", agent_type="ai_confirm_verifier", model=None),
    },
    "post-merge-verifier": {
        "claude_code": RoleLauncherConfig(
            native_mechanism="Agent tool", agent_type="post-merge-verifier", model="sonnet"
        ),
        "codex": RoleLauncherConfig(native_mechanism="spawn_agent", agent_type="post_merge_verifier", model=None),
    },
    "yaru-verifier": {
        "claude_code": RoleLauncherConfig(native_mechanism="Agent tool", agent_type="yaru-verifier", model="sonnet"),
        "codex": RoleLauncherConfig(native_mechanism="spawn_agent", agent_type="yaru_verifier", model=None),
    },
    "issue-next": {
        "claude_code": RoleLauncherConfig(native_mechanism="Skill tool", agent_type="issue-next", model=None),
        "codex": RoleLauncherConfig(native_mechanism="spawn_agent", agent_type=None, model=None),
    },
}


def get_role_config(role: str, launcher: str) -> dict[str, str | None] | None:
    """role・起動元の起動設定を取得する。未登録の組み合わせは None を返す（Issue #4191）."""
    launchers = _ROLE_REGISTRY.get(role)
    if launchers is None:
        return None
    config = launchers.get(launcher)
    if config is None:
        return None
    return asdict(config)
