#!/usr/bin/env python3
"""PreToolUse hook: git commit 前に claude -p / --print の使用を検出してブロックする.

旧 ban-claude-p.sh を 1:1 で踏襲する（Phase 4 / #1057 で Python 化）。Issue #565・#957。
Issue #2443: worktree 盲目対策 — `_lib.hook_io.resolve_target_cwd` で対象リポジトリの
CWD を解決してから git を実行する（コマンド解析 → payload `cwd` → プロセス CWD）。

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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.git_helpers import git_toplevel
from _lib.git_helpers import run_git as _run_git
from _lib.hook_io import (
    get_command,
    is_hook_enabled,
    read_hook_input,
    resolve_target_cwd,
)

# 旧 sh: grep -qE '(^|&&|;|\|)\s*git commit(\s|$)'
# Issue #2443（PR #2455 レビュー指摘）: worktree コミットで多用される
# `git -C <path> commit` も入口判定に一致させる（パス表現は
# _lib.hook_io._GIT_DASH_C_RE と同じ: "…" / '…' / 非空白列）。
_GIT_COMMIT_RE = re.compile(
    r"(^|&&|;|\|)\s*git\s+(?:-C\s+(?:\"[^\"]+\"|'[^']+'|\S+)\s+)?commit(\s|$)"
)

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
    "templates/workflow/.claude/hooks/ban-claude-p.py",  # Issue #2486: copier 配布先も除外
)


def _git(*args: str, cwd: str | None = None) -> tuple[int, str]:
    """Issue #2958: subprocess 実行部は `_lib.git_helpers.run_git()` に委譲する."""
    result = _run_git(*args, cwd=cwd, timeout=20)
    if result is None:
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

    # Issue #2443: hook プロセスはセッション CWD（メイン checkout）で動くため、
    # worktree コミットの staged 内容を検査できるよう対象リポジトリの CWD を解決する。
    target_cwd = resolve_target_cwd(payload, command)
    git_root = git_toplevel(cwd=target_cwd)
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
