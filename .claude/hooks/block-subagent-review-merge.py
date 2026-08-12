#!/usr/bin/env python3
"""PreToolUse hook: subagent からの `tidd ai-review` / `gh pr merge` をブロックする (#2542).

issue-implementer / issue-fixer 等の subagent は PR 作成（または push）で責務が
終端する契約だが、2026-07-25 に issue-implementer が独自判断で `tidd ai-review` を
実行し parser critical PR（`ai_review/` 変更）を異バックエンド合議（#1290）なしで
自動マージした（PR #2539）。agent 定義への禁止明記だけでは prompt 逸脱を防げないため、
実行元が subagent であることを機械判定してブロックする。

判定シグナル: PreToolUse payload の `agent_type` フィールドは subagent（Agent tool）
実行時のみ付与される（main session からの実行には存在しない・#2542 で実証）。

ブロック対象（subagent 実行時のみ）:
  - `tidd ai-review ...` / `python -m tidd_tools ai-review ...`
  - `gh pr merge ...`

**quote-aware 判定（Issue #3510）:** ai-review の検出は shlex でクォートを解釈した
トークン列に対して行い、`ai-review` がコマンド起動トークン（`tidd ai-review` /
`python -m tidd_tools ai-review`）として現れる場合のみブロックする。クォート内の
引数文字列（commit message・ファイルパス等）に `tidd ai-review` という文言が含まれる
だけでは誤検知しない（実例: #3421 の commit message が誤ブロックされた）。

**許可リスト（Issue #3436）:** `/issue-next-all` が `/issue-next` を subagent として
実行する多階層委譲では、issue-implementer の呼び出し元（issue-next エージェント）自身も
`agent_type` が付与された subagent になる。issue-next（-all）エージェントはレビュー・
マージ実行の責務を持つため（`.claude/skills/issue-next/subagent-delegation.md`
「Codex: wait_agent タイムアウト時の注意」参照）、`agent_type` が `issue-next` /
`issue-next-all`（Codex の snake_case `issue_next` / `issue_next_all` を含む）の場合は
以下のブロック判定をスキップする。issue-implementer / issue-fixer は引き続きブロックする。

stdlib のみ使用。
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import get_command, is_hook_enabled, read_hook_input

DETAIL = "詳細: docs/reference/hooks.md#block-subagent-review-mergepy\n"

# `gh pr merge`（グローバルフラグ挟み込みを許容）
_PR_MERGE_RE = re.compile(r"\bgh(\s+-\S+)*\s+pr\s+merge\b")

# issue-next（-all）自身は ai-review / gh pr merge の実行責務を持つ（#3436）。
# Codex は task_name を snake_case（issue_next / issue_next_all）で渡すため、
# 判定前に `_normalize_agent_type()` でハイフン区切りへ正規化する。
_ALLOWED_AGENT_TYPES = {"issue-next", "issue-next-all"}


def _normalize_agent_type(agent_type: str) -> str:
    return agent_type.replace("_", "-")


def _is_ai_review_invocation(command: str) -> bool:
    """`tidd ai-review` / `python -m tidd_tools ai-review` の**実行コマンド**を判定する.

    `shlex.split()` でクォートを解釈してトークン化し、`ai-review` が未クォートの単独
    トークンとして現れ、かつ直前が `tidd`（サブコマンド位置）または `-m tidd_tools`
    （モジュール起動・`uv run` 経由含む）の場合のみブロック対象とする。

    クォート内の引数文字列（commit message やファイルパス）に `tidd ai-review` という
    文言が含まれるだけでは単一トークンに畳まれるため誤検知しない（#3510・#3403）。
    クォート不整合でパース不能な文字列はコマンド実行と確定できないため非ブロック。
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    for i, token in enumerate(tokens):
        if token != "ai-review":
            continue
        if i >= 1 and tokens[i - 1] == "tidd":
            return True
        if i >= 2 and tokens[i - 2] == "-m" and tokens[i - 1] == "tidd_tools":
            return True
    return False


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    agent_type = payload.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type:
        return 0  # main session からの実行は対象外

    if _normalize_agent_type(agent_type) in _ALLOWED_AGENT_TYPES:
        return 0  # 許可リスト一致（#3436）

    command = get_command(payload)
    if not command:
        return 0

    if _is_ai_review_invocation(command):
        sys.stderr.write(
            f"BLOCK: 契約違反: subagent（{agent_type}）から tidd ai-review を実行できません。\n"
            "subagent の責務は gh pr create（issue-implementer）/ push（issue-fixer）で終端します。\n"
            "ai-review の実行は親セッションの責務です。特に ai_review/ を変更する\n"
            "parser critical PR は異バックエンド合議（#1290・parser-critical-pr.md）が必須であり、\n"
            "subagent はこの判定を行えません。最終応答（PR: #<N> / branch: / worktree:）を返して\n"
            "終了してください。\n"
        )
        sys.stderr.write(DETAIL)
        return 2

    if _PR_MERGE_RE.search(command):
        sys.stderr.write(
            f"BLOCK: 契約違反: subagent（{agent_type}）から gh pr merge を実行できません。\n"
            "マージ判断は親セッション（ai-review の exit code とマージ gate）の責務です。\n"
            "最終応答を返して終了してください。\n"
        )
        sys.stderr.write(DETAIL)
        return 2

    return 0


def main() -> int:
    if not is_hook_enabled("block-subagent-review-merge"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
