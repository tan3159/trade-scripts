#!/usr/bin/env python3
"""PreToolUse hook: exit 3 の証跡なしでの Claude フォールバックレビュー起動をブロックする (#3629).

**背景:** `tidd ai-review` が exit 3（全バックエンド利用不可）を返したときだけ
`ai-fallback-reviewer` subagent を起動すべきだが、手動・誤発火による起動を防ぐ
機械強制がない。exit 3 の際に `~/.cache/tidd/ai-reviewer/pr-<N>/backend-unavailable`
証跡フラグ（tidd_tools.ai_review.state_dir が作成）を書き、本 hook はその有無で
フォールバック subagent の起動を許可 / ブロックする。

**判定対象:** Claude Code の Agent tool（`tool_input.subagent_type == "ai-fallback-reviewer"`）
と Codex の spawn_agent（`tool_input.task_name == "ai_fallback_reviewer"`）。kebab-case /
snake_case を同一視する（#3203）。

**判定:** prompt / message から `PR番号: <N>` 形式で PR 番号を抽出し、
`tidd/ai-reviewer/pr-<N>/backend-unavailable` の有無で判定する。

- フラグあり → exit 0（起動許可）
- フラグなし → exit 2 + stderr に "Blocked" を出力（起動ブロック）
- PR 番号を抽出できない → fail-closed で exit 2（exit 3 の証跡を検証できないため）
- escape hatch: `SKIP_FALLBACK_REVIEW_GATE=1` で素通し（exit 0）

opt-in 設計（#2166）: config.json の `block-unauthorized-fallback-review` キーで
有効化する（is_hook_enabled）。

stdlib のみ使用。
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import get_ai_reviewer_state_dir, is_hook_enabled, read_hook_input
from _lib.tidd_uvx import build_uvx_tidd_cmd

DETAIL = "詳細: docs/reference/hooks.md#block-unauthorized-fallback-reviewpy\n"

_TARGET_AGENT = "ai_fallback_reviewer"
_BACKEND_UNAVAILABLE_FLAG = "backend-unavailable"
_PR_NUM_RE = re.compile(r"PR番号:\s*(\d+)")


def _ai_review_guidance_command(pr_num: str) -> str:
    """`tidd ai-review <PR> 1` の案内コマンド文字列を組み立てる（Issue #3405）.

    handbook ローカルパス（`projects/py/tidd_tools`）を直書きせず、`_lib/tidd_uvx.py`
    経由で uvx ゼロインストール実行方式のコマンドを組み立てる（consumer でも実行可能）。
    """
    uvx_cmd = build_uvx_tidd_cmd("ai-review", pr_num, "1")
    if uvx_cmd is None:
        # uvx が PATH に無い環境向けフォールバック（PATH 導入済み tidd 前提）
        return f"tidd ai-review {pr_num} 1"
    return shlex.join(["uvx", *uvx_cmd[1:]])


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")

    # tool_name が Agent tool（Claude Code）または spawn_agent（Codex）でなければ対象外
    tool_name = str(payload.get("tool_name", ""))
    if tool_name not in {"Agent", "spawn_agent"}:
        return 0

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    # ai-fallback-reviewer のみ対象（kebab-case / snake-case を同一視・#3203）
    agent_name = str(
        tool_input.get("subagent_type") or tool_input.get("task_name") or ""
    ).replace("-", "_")
    if agent_name != _TARGET_AGENT:
        return 0

    # escape hatch
    if os.environ.get("SKIP_FALLBACK_REVIEW_GATE") == "1":
        return 0

    # prompt / message から PR 番号を抽出（取れなければ fail-closed でブロック）
    prompt = str(tool_input.get("prompt") or tool_input.get("message") or "")
    match = _PR_NUM_RE.search(prompt)
    if not match:
        sys.stderr.write(
            "Blocked: ai-fallback-reviewer の prompt / message から `PR番号: <N>` を抽出できません。\n"
            "exit 3 の証跡（backend-unavailable フラグ）を検証できないため起動をブロックします。\n"
            "prompt に `PR番号: <PR番号>` を明示してください。\n"
        )
        sys.stderr.write(DETAIL)
        return 2

    pr_num = match.group(1)
    flag_path = get_ai_reviewer_state_dir(pr_num) / _BACKEND_UNAVAILABLE_FLAG
    if flag_path.is_file():
        return 0

    sys.stderr.write(
        f"Blocked: PR #{pr_num} の exit 3 証跡（backend-unavailable フラグ）が存在しません。\n"
        "ai-fallback-reviewer は `tidd ai-review` が exit 3（全バックエンド利用不可）を返した"
        "ときだけ起動できます。\n"
        f"まず `{_ai_review_guidance_command(pr_num)}` を実行して exit 3 を確認してください。\n"
    )
    sys.stderr.write(DETAIL)
    return 2


def main() -> int:
    if not is_hook_enabled("block-unauthorized-fallback-review"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
