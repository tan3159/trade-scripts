#!/usr/bin/env python3
"""PreToolUse hook: issue-implementer / issue-fixer 呼び出し時のプロンプト契約を検証する (#2724).

issue-implementer の入力契約は「Issue番号: N のみ」、
issue-fixer の入力契約は「PR番号: N のみ」である（各 agent 定義 L10・L14）。
しかし呼び出し元（親セッション）が誤って曖昧なプロンプトで Agent tool を
起動した場合、フレッシュな subagent が無関係な Issue を拾って実装を完走させてしまう。

本 hook は Agent tool 呼び出しの PreToolUse 時に契約を検証し、違反の場合は
exit 2 でブロックする。Claude Code の Agent tool（tool_name="Agent"）と
Codex の spawn_agent（tool_name="spawn_agent"）の両スキーマに対応する（#3203）:

  - Claude Code: tool_input.subagent_type / tool_input.prompt
  - Codex: tool_input.task_name / tool_input.message

Codex の task_name は snake_case が標準だが、Claude Code の agent 名
（kebab-case）との互換のため `-` を `_` に正規化して同一視する。

検証対象:
  - subagent_type == "issue-implementer": prompt が ^Issue番号:\\s*\\d+\\s*$ に一致すること
  - subagent_type == "issue-fixer": prompt が ^PR番号:\\s*\\d+\\s*$ に一致すること

その他の subagent_type は対象外（Agent tool 以外の tool_name も対象外）。

stdlib のみ使用。

実物確認（Claude Code 2026-07-27 に transcript から確認した実際の payload 構造）:
  {
    "tool_name": "Agent",
    "tool_input": {
      "subagent_type": "issue-implementer",
      "prompt": "Issue番号: 2724"
    }
  }

Codex の spawn_agent は以下で届く（#3203）:
  {
    "tool_name": "spawn_agent",
    "tool_input": {
      "task_name": "issue_implementer",
      "message": "Issue番号: 2724",
      "fork_turns": "all"
    }
  }
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import is_hook_enabled, read_hook_input

DETAIL = "詳細: docs/reference/hooks.md#require-subagent-prompt-contractpy\n"

# 契約フォーマット正規表現
_ISSUE_NUM_RE = re.compile(r"^Issue番号:\s*\d+\s*$")
_PR_NUM_RE = re.compile(r"^PR番号:\s*\d+\s*$")

# 検証対象の subagent_type / task_name -> (期待フォーマット正規表現, エラー時のキーワード)
# キーは snake_case に正規化して比較する（kebab-case の Claude agent 名と同一視・#3203）。
_CONTRACT_MAP: dict[str, tuple[re.Pattern[str], str]] = {
    "issue_implementer": (_ISSUE_NUM_RE, "Issue番号"),
    "issue_fixer": (_PR_NUM_RE, "PR番号"),
}


def _normalize_subagent_name(name: str) -> str:
    """Claude Code の agent 名（kebab-case）と Codex の task_name（snake_case）を同一視する."""
    return name.replace("-", "_")


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")

    # tool_name が Agent tool（Claude Code）または spawn_agent（Codex）でなければ対象外
    tool_name = str(payload.get("tool_name", ""))
    if tool_name not in {"Agent", "spawn_agent"}:
        return 0

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    subagent_type = _normalize_subagent_name(
        str(tool_input.get("subagent_type") or tool_input.get("task_name") or "")
    )
    if subagent_type not in _CONTRACT_MAP:
        return 0  # 対象外の subagent_type

    pattern, keyword = _CONTRACT_MAP[subagent_type]
    prompt = str(tool_input.get("prompt") or tool_input.get("message") or "")

    if pattern.match(prompt):
        return 0  # 契約通り

    sys.stderr.write(
        f"BLOCK: 契約違反: {subagent_type} への prompt が契約フォーマットに一致しません。\n"
        f"契約: '{keyword}: <番号>' のみを渡してください（例: '{keyword}: 2724'）。\n"
        f"受け取った prompt: {prompt!r}\n"
        "Issue番号/PR番号のみを渡す契約に違反しています。\n"
        "既存の agent を継続する場合は Agent tool ではなく SendMessage を使ってください。\n"
        f"新規 subagent として起動する場合は '{keyword}: <番号>' のみを prompt に渡してください。\n"
    )
    sys.stderr.write(DETAIL)
    return 2


def main() -> int:
    if not is_hook_enabled("require-subagent-prompt-contract"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
