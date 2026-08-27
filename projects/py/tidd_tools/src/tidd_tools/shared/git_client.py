"""git CLI ラッパー（クロスプラットフォーム対応）.

Phase 2-A (#1053) で導入。後続 Phase の check-pr-conflicts / issue-next-state / loop-error-log /
ai-review.sh 移植先が利用する。

設計方針:
- `shell=False` 強制（`subprocess_runner.run` を経由）
- 失敗時は `GitCommandError` 例外を送出
- パスは `pathlib.Path` で扱い、`str()` への変換は呼び出し境界のみ
"""

from __future__ import annotations

import logging
from pathlib import Path

from tidd_tools.shared.errors import GitCommandError
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)


def _run_git(args: list[str], *, cwd: Path | str | None = None, check: bool = True) -> str:
    """git コマンドを実行して stdout 文字列を返す."""
    result = run_subprocess(["git", *args], cwd=cwd)
    if result.returncode != 0:
        if check:
            raise GitCommandError(args, result.returncode, result.stderr or "")
        return ""
    return result.stdout.rstrip("\n")


def rev_parse_show_toplevel(cwd: Path | str | None = None) -> Path:
    """`git rev-parse --show-toplevel` の結果を `Path` で返す."""
    out = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    return Path(out).resolve()


def current_branch(cwd: Path | str | None = None) -> str:
    """現在のブランチ名を返す（detached HEAD のときは空文字）."""
    return _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, check=False)


def diff_name_only(base_ref: str = "main", *, cwd: Path | str | None = None) -> list[str]:
    """`git diff --name-only <base_ref>...HEAD` を返す."""
    out = _run_git(["diff", "--name-only", f"{base_ref}...HEAD"], cwd=cwd, check=False)
    return [line for line in out.splitlines() if line.strip()]


def head_sha(cwd: Path | str | None = None) -> str:
    """`git rev-parse HEAD` を返す."""
    return _run_git(["rev-parse", "HEAD"], cwd=cwd)


def log_oneline(n: int = 10, *, cwd: Path | str | None = None) -> list[str]:
    """直近 n 件のコミット要約を返す."""
    out = _run_git(["log", f"-n{n}", "--oneline"], cwd=cwd, check=False)
    return out.splitlines()
