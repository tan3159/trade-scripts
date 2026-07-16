#!/usr/bin/env python3
"""PreToolUse hook: PR タイトルに Issue ID (#N) が含まれることを強制する.

Issue #1840: PR タイトルに Issue ID が含まれていない PR が混在しており、
PR 一覧から対応 Issue をすぐに特定できない問題を解消する。

**ブロック条件:**
- `gh pr create` コマンドの `--title` に `#数字` 形式の Issue ID が含まれていない
- `mcp__github__create_pull_request` の `title` に `#数字` 形式の Issue ID が含まれていない

**バイパス:**
- PR ボディに `<!-- allow-no-issue-id: <理由> -->` マーカーが含まれている場合は通過する
  （依存更新・merge commit など Issue を持たない PR に使用）

stdlib のみ使用。
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (  # noqa: E402
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)

_GH_PR_CREATE_RE = re.compile(r"(^|&&|;|\|)\s*gh pr create(\s|$)")
_ISSUE_ID_RE = re.compile(r"#\d+")
_ALLOW_NO_ISSUE_ID_RE = re.compile(r"<!--\s*allow-no-issue-id:")
_MCP_CREATE_PR_TOOL = "mcp__github__create_pull_request"


def _extract_title(command: str) -> str:
    """gh pr create コマンドから --title / -t の値を抽出する."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--title", "-t") and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--title="):
            return tok[len("--title=") :]
        i += 1
    return ""


def _extract_body(command: str) -> str:
    """gh pr create コマンドから --body / --body-file の値を抽出する."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--body", "-b") and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--body="):
            return tok[len("--body=") :]
        if tok in ("--body-file", "-F") and i + 1 < len(tokens):
            try:
                return Path(tokens[i + 1]).read_text(encoding="utf-8")
            except OSError:
                return ""
        if tok.startswith("--body-file="):
            try:
                return Path(tok[len("--body-file=") :]).read_text(encoding="utf-8")
            except OSError:
                return ""
        i += 1
    return ""


def _check_title_and_body(title: str, body: str, title_hint: str) -> int:
    """タイトルと本文を検証して exit code を返す."""
    if not title:
        if _ALLOW_NO_ISSUE_ID_RE.search(body):
            sys.stderr.write(
                "require-issue-id-in-pr-title: allow-no-issue-id マーカーを検出しました。bypass します。\n"
            )
            return 0
        sys.stderr.write(
            "Blocked: PR タイトルに Issue ID（例: #123）を含めてください。\n"
            "\n"
            f"  {title_hint}\n"
            "  正しい形式:     feat(scope): #1840 説明文\n"
            "\n"
            "Issue ID なしで作成したい場合は PR ボディに以下を追加してください:\n"
            "  <!-- allow-no-issue-id: <理由> -->\n"
            "\n"
            "詳細: docs/reference/hooks.md#require-issue-id-in-pr-titlepy\n"
        )
        return 2

    if _ISSUE_ID_RE.search(title):
        return 0

    if _ALLOW_NO_ISSUE_ID_RE.search(body):
        sys.stderr.write(
            "require-issue-id-in-pr-title: allow-no-issue-id マーカーを検出しました。bypass します。\n"
        )
        return 0

    sys.stderr.write(
        "Blocked: PR タイトルに Issue ID（例: #123）を含めてください。\n"
        "\n"
        f"  現在のタイトル: {title!r}\n"
        "  正しい形式:     feat(scope): #1840 説明文\n"
        "\n"
        "Issue ID なしで作成したい場合は PR ボディに以下を追加してください:\n"
        "  <!-- allow-no-issue-id: <理由> -->\n"
        "\n"
        "詳細: docs/reference/hooks.md#require-issue-id-in-pr-titlepy\n"
    )
    return 2


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)

    # MCP create_pull_request の場合
    if tool_name == _MCP_CREATE_PR_TOOL:
        tool_input = payload.get("tool_input", {})
        title = str(tool_input.get("title", ""))
        body = str(tool_input.get("body", ""))
        return _check_title_and_body(
            title, body, "--title が指定されていません（MCP create_pull_request）。"
        )

    # Bash の場合
    if tool_name != "Bash":
        return 0
    command = get_command(payload)
    if not _GH_PR_CREATE_RE.search(command):
        return 0

    title = _extract_title(command)
    body = _extract_body(command)
    return _check_title_and_body(
        title,
        body,
        "--title が指定されていません（--fill 等の自動タイトル生成はサポートされません）。",
    )


def main() -> int:
    if not is_hook_enabled("require-issue-id-in-pr-title"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
