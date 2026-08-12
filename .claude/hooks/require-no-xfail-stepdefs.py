#!/usr/bin/env python3
"""PreToolUse hook: gh pr create 時に step_defs の xfail 残存をブロックする.

Issue #3425: `gh pr create` 実行時に、origin/main との差分に含まれる
`tests/step_defs/` 配下の `test_*.py` に xfail marker
（`pytest.mark.xfail` / `@xfail`）が残存している場合にブロックする。

`tidd test-plan`（Issue #2000）は PR 全体の step_defs を対象に xfail を
検知するが、本 hook は PR 作成の早期段階で同一問題を検出する補完であり、
`tidd test-plan` の xfail チェックを置き換えるものではない。

**ブロック条件:**
- Bash ツールで `gh pr create` を実行する
- `origin/main...HEAD` の差分に含まれる `tests/step_defs/` 配下の
  `test_*.py` に `pytest.mark.xfail` / `@xfail` が含まれる

**ブロック時:** exit 2 + stderr に残存ファイル一覧と
「実装完了時に xfail を外してください（#2000）」を出力する。

**fail-open:** git コマンド失敗（diff 取得不能・リポジトリ外など）はブロックしない。

stdlib のみ使用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.gh_command import is_gh_pr_create as _is_gh_pr_create
from _lib.git_helpers import run_git_in_repo as _run_git_in_repo
from _lib.hook_io import (
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
    resolve_target_cwd,
)

# xfail marker の検出（step_defs の `test_*.py` のみを対象にする）:
# - `pytest.mark.xfail`（decorator / pytestmark 代入の両方に含まれる）
# - `@xfail`（shorthand decorator）
_XFAIL_RE = re.compile(r"pytest\.mark\.xfail|@xfail")

# 差分内の対象ファイル（`tests/step_defs/` 配下の `test_*.py`）
_STEP_DEFS_DIR_MARKER = "tests/step_defs/"


def _is_step_defs_test_file(path: str) -> bool:
    """origin/main との差分に含まれる step_defs 配下のテストファイルか判定する."""
    name = path.rsplit("/", 1)[-1]
    return (
        _STEP_DEFS_DIR_MARKER in path
        and name.startswith("test_")
        and name.endswith(".py")
    )


def _find_offending_files(repo: str) -> list[str]:
    """origin/main...HEAD の差分から xfail が残存する step_defs ファイルを列挙する."""
    rc, stdout, _stderr = _run_git_in_repo(
        repo, "diff", "--name-only", "origin/main...HEAD", timeout=20
    )
    if rc != 0:
        # fail-open: git コマンド失敗時はブロックしない（#3425）
        return []

    offending: list[str] = []
    for rel_path in stdout.splitlines():
        rel_path = rel_path.strip()
        if not _is_step_defs_test_file(rel_path):
            continue
        full_path = Path(repo) / rel_path
        try:
            content = full_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _XFAIL_RE.search(content):
            offending.append(rel_path)
    return offending


def _block_message(offending: list[str]) -> str:
    lines = [
        "Blocked: tests/step_defs/ 配下の test_*.py に xfail marker が残存しています。",
        "実装完了時に xfail を外してください（#2000）",
        "",
        "該当ファイル:",
    ]
    lines.extend(f"  {path}" for path in offending)
    lines.append("")
    lines.append("詳細: docs/reference/hooks.md#require-no-xfail-stepdefspy")
    return "\n".join(lines) + "\n"


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)

    # Bash 以外のツールは通過
    if tool_name != "Bash":
        return 0

    command = get_command(payload)
    # `gh pr create` 以外のコマンドは通過
    if not _is_gh_pr_create(command):
        return 0

    repo = resolve_target_cwd(payload, command)
    offending = _find_offending_files(repo)
    if not offending:
        return 0

    sys.stderr.write(_block_message(offending))
    return 2


def main() -> int:
    if not is_hook_enabled("require-no-xfail-stepdefs"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
