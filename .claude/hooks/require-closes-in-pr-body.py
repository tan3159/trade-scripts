#!/usr/bin/env python3
"""PreToolUse hook: PR 本文に closes #N が含まれることを強制する.

Issue #3420: `gh pr create` で PR 本文に `closes #N` / `fixes #N` / `resolves #N`
が含まれない場合、Issue やること全消化 gate (#1756) や `auto-tick-issue-items.py`
が機能せず、マージしても Issue が閉じない孤立 PR が発生する問題を解消する。

**ブロック条件:**
- `gh pr create` コマンドの `--body` / `--body-file` / heredoc に
  `closes/fixes/resolves #N`（大文字小文字不問）が含まれていない

**バイパス:**
- PR ボディに `<!-- allow-no-closes: <理由> -->` マーカーが含まれる場合は通過する
  （実験的な PR・外部 Issue 紐付けが不要な場合に使用）

**対象ツール:**
- `Bash` 内の `gh pr create` コマンド
- `mcp__github__create_pull_request`

stdlib のみ使用。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.gh_command import CLOSES_RE as _CLOSES_RE
from _lib.gh_command import extract_pr_body as _extract_body
from _lib.gh_command import is_gh_pr_create as _is_gh_pr_create
from _lib.hook_io import (
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)
from _lib.override_markers import has_override_marker

_ALLOW_NO_CLOSES_MARKER = "allow-no-closes"
_MCP_CREATE_PR_TOOL = "mcp__github__create_pull_request"


def _check_body(body: str) -> int:
    """PR 本文を検証して exit code を返す."""
    if has_override_marker(body, _ALLOW_NO_CLOSES_MARKER):
        return 0

    if _CLOSES_RE.search(body):
        return 0

    sys.stderr.write(
        "Blocked: PR 本文に closes #N を含めてください。\n"
        "\n"
        "  PR 本文に closes/fixes/resolves + Issue 番号を追加してください。\n"
        "  例: closes #3420\n"
        "  例: fixes #100\n"
        "\n"
        "closes なしで作成したい場合は PR ボディに以下を追加してください:\n"
        "  <!-- allow-no-closes: <理由> -->\n"
        "\n"
        "詳細: docs/reference/hooks.md#require-closes-in-pr-bodypy\n"
    )
    return 2


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)

    # MCP create_pull_request の場合
    if tool_name == _MCP_CREATE_PR_TOOL:
        tool_input = payload.get("tool_input", {})
        body = str(tool_input.get("body", ""))
        return _check_body(body)

    # Bash の場合
    if tool_name != "Bash":
        return 0
    command = get_command(payload)
    if not _is_gh_pr_create(command):
        return 0

    body = _extract_body(command)
    return _check_body(body)


def main() -> int:
    if not is_hook_enabled("require-closes-in-pr-body"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
