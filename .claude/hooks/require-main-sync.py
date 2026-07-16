#!/usr/bin/env python3
"""PreToolUse hook: git checkout -b 前に main が origin/main と同期しているか確認する.

旧 require-main-sync.sh の振る舞いを 1:1 で踏襲する（Phase 4 / #1057 で Python 化）。

確認内容:
  1. 現在のブランチが main か
  2. main が origin/main と同じコミットか

どちらかを満たさない場合は exit 2 でブロックする。
stdlib のみ使用。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import get_command, is_hook_enabled, read_hook_input  # noqa: E402


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=False, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


# 旧 sh の先頭ステートメント抽出ロジック相当:
#   FIRST_STMT=$(echo "$COMMAND" | sed 's/[;&|].*//' | tr -d '"'"'" | tr '\t\n\r' '   ')
_QUOTE_CHARS = ('"', "'")
_TAB_NL = "\t\n\r"


def _first_statement(command: str) -> str:
    head = re.split(r"[;&|]", command, maxsplit=1)[0]
    # クォート除去
    for q in _QUOTE_CHARS:
        head = head.replace(q, "")
    # タブ・改行をスペースに置換
    for ch in _TAB_NL:
        head = head.replace(ch, " ")
    return head


# 旧 sh: grep -qE '^\s*git(\s+-\S+)*\s+checkout\b.*\s-b\b'
_CHECKOUT_B_RE = re.compile(r"^\s*git(\s+-\S+)*\s+checkout\b.*\s-b\b")


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")  # Issue #1364
    command = get_command(payload)
    if not command:
        return 0

    head = _first_statement(command)
    if not _CHECKOUT_B_RE.search(head):
        return 0

    current_branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if current_branch is None:
        return 0

    if current_branch != "main":
        sys.stderr.write(
            "BLOCK: 新規ブランチは main から切ってください。"
            "git checkout main && git pull を実行してください\n"
        )
        sys.stderr.write("詳細: docs/reference/hooks.md#require-main-syncpy\n")
        return 2

    # origin/main をフェッチ（失敗しても継続）
    subprocess.run(
        ["git", "fetch", "origin", "main", "--quiet"],
        capture_output=True,
        check=False,
        timeout=30,
    )

    local_commit = _git("rev-parse", "HEAD")
    remote_commit = _git("rev-parse", "origin/main")
    if local_commit and remote_commit and local_commit != remote_commit:
        sys.stderr.write(
            "BLOCK: main が origin/main と一致しません。"
            "git checkout main && git pull を実行してください\n"
        )
        sys.stderr.write("詳細: docs/reference/hooks.md#require-main-syncpy\n")
        return 2

    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("require-main-sync"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
