#!/usr/bin/env python3
"""PreToolUse hook: git commit 前に claude -p / --print の使用を検出してブロックする.

旧 ban-claude-p.sh を 1:1 で踏襲する（Phase 4 / #1057 で Python 化）。Issue #565・#957。

検出対象: リポジトリ全体のステージング済みファイル（index 内容を検査する）
除外対象:
  - *.md（Markdown ドキュメント。説明用途）
  - tests/ 配下（テストフィクスチャ）
  - docs/ 配下（ドキュメント）
  - node_modules/ 配下
  - .venv/ 配下
  - __pycache__/ 配下
  - .claude/hooks/ban-claude-p.{sh,py}（hook 本体）

stdlib のみ使用。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import get_command, is_hook_enabled, read_hook_input  # noqa: E402

# 旧 sh: grep -qE '(^|&&|;|\|)\s*git commit(\s|$)'
_GIT_COMMIT_RE = re.compile(r"(^|&&|;|\|)\s*git commit(\s|$)")

# 旧 sh の grep パターン:
#   'claude[[:space:]]+(-p[[:space:]"\'`]|-p$|--print[[:space:]"\'`]|--print$)'
# 行単位で適用するため re.MULTILINE で $ を行末扱いにする。
_CLAUDE_P_RE = re.compile(
    r"claude\s+(-p[\s\"'`]|-p$|--print[\s\"'`]|--print$)", re.MULTILINE
)

_EXCLUDE_DIR_FRAGMENTS = (
    "/tests/",
    "/docs/",
    "/node_modules/",
    "/.venv/",
    "/__pycache__/",
)
_SELF_PATHS = (
    ".claude/hooks/ban-claude-p.sh",
    ".claude/hooks/ban-claude-p.py",
)


def _git(*args: str, cwd: str | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            cwd=cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout


def _is_excluded(path: str) -> bool:
    if path.endswith(".md"):
        return True
    if path in _SELF_PATHS:
        return True
    wrapped = "/" + path + "/"
    for fragment in _EXCLUDE_DIR_FRAGMENTS:
        if fragment in wrapped:
            return True
    return False


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")  # Issue #1364
    command = get_command(payload)
    if not command:
        return 0
    if not _GIT_COMMIT_RE.search(command):
        return 0

    rc, root_out = _git("rev-parse", "--show-toplevel")
    if rc != 0:
        return 0
    git_root = root_out.strip()
    if not git_root:
        return 0

    rc, staged_out = _git(
        "-C", git_root, "diff", "--cached", "--name-only", "--diff-filter=AM"
    )
    if rc != 0:
        return 0
    staged_files = [line for line in staged_out.splitlines() if line]
    if not staged_files:
        return 0

    found = False
    for file in staged_files:
        if _is_excluded(file):
            continue
        rc, blob = _git("-C", git_root, "show", f":{file}")
        if rc != 0:
            continue
        if _CLAUDE_P_RE.search(blob):
            sys.stderr.write(
                f"Blocked: {file} に claude -p / --print の使用が含まれています。\n"
            )
            found = True

    if found:
        sys.stderr.write("\n")
        sys.stderr.write(
            "claude -p / --print はコストのかかる方法で AI を呼び出します。\n"
            "代わりに Claude Code の Agent tool（タスクを自律で実行する機能）を使ってください。\n"
        )
        sys.stderr.write("詳細: Issue #565 / docs/reference/hooks.md#ban-claude-ppy\n")
        return 2

    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("ban-claude-p"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
