"""AI エージェント（Claude Code / Codex）セッション判定関数（Issue #1304・#3202）.

hook / tidd_tools サブコマンド等から共通利用可能な、AI エージェントセッション内で
実行されているかを判定するヘルパー。session 外での LLM 呼び出しを skip 警告付きで
避けるために使う。

- stdlib のみで実装（`.claude/hooks/` 全体の原則）
- 環境変数 `CLAUDECODE` または `TIDD_AGENT` が truthy（`1` / `true` / `yes` / `on`、
  `TIDD_AGENT` は `codex` も可）ならセッション内と判定
- 呼び出し側は `is_claude_code_session()` を使う

判定ロジックの根拠:
- Claude Code CLI は起動時に環境変数 `CLAUDECODE=1` を子プロセスへ引き継ぐ
- Codex は `.codex/hooks.json` の command 経由で `TIDD_AGENT=codex` を hook
  プロセスへ渡す（正本: `.rulesync/hooks.jsonc`・Issue #3202）
- subprocess から更に起動された子孫でも同変数が継承される（`os.environ` 経由）
- CI / cron / 素の bash 起動では未設定なので session 外と判定される

補足:
- 「セッション内=Agent tool が利用可能」ではない点に注意
  （subprocess から Agent tool は呼べない・#1304 SKILL.md 側で Agent tool 起動する設計）
- セッション内でも Agent tool を呼ぶかどうかは呼び出し側の責務
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}
# Issue #3202: Codex は CLAUDECODE を立てないため、TIDD_AGENT=codex を追加で受理する
_TIDD_AGENT_TRUTHY = _TRUTHY | {"codex"}


def is_claude_code_session(env: dict[str, str] | None = None) -> bool:
    """AI エージェント（Claude Code / Codex）セッション内で実行されているかを返す.

    Parameters
    ----------
    env:
        判定に使う環境変数辞書。省略時は ``os.environ`` を使う。
        テストで dependency injection するときに指定する。

    Returns
    -------
    bool
        ``CLAUDECODE`` または ``TIDD_AGENT`` のいずれかが truthy
        （``1``/``true``/``yes``/``on``、``TIDD_AGENT`` は ``codex`` も可）なら True。
    """
    source = env if env is not None else os.environ
    claudecode = source.get("CLAUDECODE", "").strip().lower()
    tidd_agent = source.get("TIDD_AGENT", "").strip().lower()
    return claudecode in _TRUTHY or tidd_agent in _TIDD_AGENT_TRUTHY
