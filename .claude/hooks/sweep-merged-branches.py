#!/usr/bin/env python3
"""PostToolUse hook: PR マージ後の不要ブランチを自動掃除する (#3398).

Bash 経由の `gh pr merge` または `mcp__github__merge_pull_request` の成功を
捕捉し、`tidd sweep-merged-branches` を呼び出してローカル・リモートの全ブランチ
（main 除く・アクティブな worktree 使用中は除外）をスイープする。

安全条件（PR MERGED + HEAD 一致 + 関連 Issue 非 OPEN）を満たすブランチのみを
自動削除する。安全条件を満たさないブランチは削除せずに stderr へ非ブロッキング
でレポートする（sweep 自体の判定は tidd_tools 側に集約）。

config.json の "sweep-merged-branches" キーで on/off できる（デフォルト false・
opt-in）。hook は常に exit 0（セッションをブロックしない）。stdlib のみ使用。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.gh_command import is_gh_pr_merge as _is_gh_pr_merge
from _lib.hook_io import (
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
    resolve_target_cwd,
)
from _lib.hook_io import (
    is_bash_success as _is_bash_success,
)

_HOOK_KEY = "sweep-merged-branches"
_MCP_MERGE_TOOL = "mcp__github__merge_pull_request"
_SWEEP_TIMEOUT_SECONDS = 120


def _mcp_success(payload: dict[str, Any]) -> bool:
    """mcp__github__merge_pull_request の tool_response が成功扱いか判定する."""
    tool_response = payload.get("tool_response") or {}
    if isinstance(tool_response, dict):
        if tool_response.get("interrupted") is True:
            return False
        if tool_response.get("isError") is True:
            return False
    return True


def _sweep_command(repo_root: Path) -> list[str] | None:
    """`tidd sweep-merged-branches` を実行するコマンドを解決する.

    グローバル `tidd` が PATH 上にあればそれを優先し、なければ
    `uv run --project <repo>/projects/py/tidd_tools tidd` にフォールバックする。
    どちらも使えない場合は None。
    """
    if shutil.which("tidd"):
        return ["tidd", "sweep-merged-branches"]
    tidd_project = repo_root / "projects" / "py" / "tidd_tools"
    if tidd_project.is_dir():
        return [
            "uv",
            "run",
            "--project",
            str(tidd_project),
            "tidd",
            "sweep-merged-branches",
        ]
    return None


def _run_sweep(payload: dict[str, Any], command: str | None) -> int:
    """スイープを subprocess で実行し、結果を stderr へ転記する（常に exit 0）."""
    cwd = resolve_target_cwd(payload, command)
    cmd = _sweep_command(Path(cwd))
    if cmd is None:
        sys.stderr.write(
            "sweep-merged-branches: WARN: tidd コマンドが見つからないため"
            " sweep を実行できません（非ブロッキング）\n"
        )
        return 0
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_SWEEP_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(
            f"sweep-merged-branches: WARN: sweep 実行に失敗しました: {exc}\n"
        )
        return 0

    if result.stdout.strip():
        sys.stderr.write(result.stdout)
    if result.stderr.strip():
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        sys.stderr.write(
            f"sweep-merged-branches: WARN: sweep が exit {result.returncode}"
            "（非ブロッキング・影響なし）\n"
        )
    return 0


def main() -> int:
    payload = read_hook_input(hook_name="PostToolUse")
    if not payload:
        return 0

    if not is_hook_enabled(_HOOK_KEY):
        return 0

    tool_name = get_tool_name(payload)
    command = get_command(payload)

    if tool_name == "Bash":
        if not _is_gh_pr_merge(command):
            return 0
        if not _is_bash_success(payload):
            return 0
    elif tool_name == _MCP_MERGE_TOOL:
        if not _mcp_success(payload):
            return 0
    else:
        return 0

    return _run_sweep(payload, command)


if __name__ == "__main__":
    sys.exit(main())
