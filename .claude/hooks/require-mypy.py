#!/usr/bin/env python3
"""PreToolUse hook: `gh pr create` 前に mypy (strict) を gate する (Issue #1892).

**背景:** mypy strict は `.circleci/config.yml` の CI ステップでのみ実行されており、
`gh pr create` 前にチェックするローカル hook が存在しない。これにより LLM の自律
ループが「コードを書く → PR 作成 → CI 数分待ち → mypy fail 検知 → fix → push →
CI 再待ち」という往復コストを毎 PR で踏んでいる。ruff format と同様に
`require-ruff-format.py`（#1752）と同構造でローカル gate に前出しする。

本 hook は `gh pr create` の Bash 呼び出しを検知して `projects/py/` 配下から
検査対象プロジェクト（`src/` を持つディレクトリ）を動的に検出し、そのディレクトリを
cwd に `uv run --extra dev mypy src tests` を実行し、型エラーがあれば exit 2 で block する。
（repo root から `mypy src tests` は解決できないため、CI ステップと同じくプロジェクト
ディレクトリを cwd にして実行する。`--extra dev` は consumer の dev 依存 mypy を
解決するために必要・Issue #3564。）

**対象プロジェクトの検出（Issue #2278）:** `projects/py/tidd_tools/src/` が存在すれば
handbook 自身の従来挙動を保つため優先的に選ぶ。存在しない consumer レイアウトでは
`projects/py/` 配下で `src/` を持つ最初のディレクトリ（名前順）を対象にする。

**skip する条件（exit 0 + stderr に WARN）:**

- `uv` が PATH に存在しない
- `projects/py/` 配下に `src/` を持つプロジェクトが 1 つも存在しない
- mypy が `REQUIRE_MYPY_TIMEOUT_SEC`（default 120s）で timeout

stdlib のみ使用。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.gh_command import is_gh_pr_create as _is_gh_pr_create
from _lib.git_helpers import git_toplevel as _git_toplevel_shared
from _lib.hook_io import (
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
    resolve_target_cwd,
)
from _lib.target_dir import find_target_dir as _find_target_dir_shared
from _lib.target_dir import get_timeout_sec as _get_timeout_sec_shared

_PROJECTS_PY_SUBDIR = "projects/py"
_PREFERRED_PROJECT_NAME = "tidd_tools"
_DEFAULT_TIMEOUT_SEC = 120
_TIMEOUT_ENV_VAR = "REQUIRE_MYPY_TIMEOUT_SEC"


def _find_target_dir(repo_root: Path) -> Path | None:
    """`projects/py/` 配下から検査対象プロジェクトを検出する（Issue #2278 / #2958）.

    consumer では `projects/py/tidd_tools` が存在しないため、`src/` を持つ
    プロジェクトディレクトリを動的に探す。`tidd_tools` が存在する場合は
    handbook 自身の従来挙動を保つため優先的に選ぶ。

    Issue #2958: 検出ロジック本体は `_lib/target_dir.py` の `find_target_dir()`
    に委譲する（mypy は `mypy src tests` を実行するため `require_src=True`）。
    """
    return _find_target_dir_shared(repo_root, require_src=True)


def _git_toplevel(cwd: str | None = None) -> Path | None:
    """Issue #2958: toplevel 解決は `_lib/git_helpers.py` の `git_toplevel()` に委譲する."""
    root = _git_toplevel_shared(cwd=cwd, timeout=5)
    return Path(root) if root else None


def _get_timeout_sec() -> int:
    """Issue #2958: 解決本体は `_lib/target_dir.py` の `get_timeout_sec()` に委譲する."""
    return _get_timeout_sec_shared(_TIMEOUT_ENV_VAR, _DEFAULT_TIMEOUT_SEC)


def _disabled_via_env() -> bool:
    """`CLAUDE_HOOK_DISABLE=require-mypy`（カンマ区切り可）による無効化（Issue #1892 受け入れ基準）."""
    raw = os.environ.get("CLAUDE_HOOK_DISABLE", "")
    return "require-mypy" in {s.strip() for s in raw.split(",")}


def _main() -> int:
    if _disabled_via_env():
        return 0
    payload = read_hook_input(hook_name="PreToolUse")
    if get_tool_name(payload) != "Bash":
        return 0
    command = get_command(payload)
    if not _is_gh_pr_create(command):
        return 0

    # Issue #2454: worktree で作成された PR を検査できるよう対象 CWD を解決する
    target_cwd = resolve_target_cwd(payload, command)
    repo_root = _git_toplevel(target_cwd)
    if repo_root is None:
        # git 外なら判定不能 → 通す
        return 0

    target_dir = _find_target_dir(repo_root)
    if target_dir is None:
        sys.stderr.write(
            f"WARN: require-mypy: {_PROJECTS_PY_SUBDIR}/ 配下に src を持つプロジェクトが見つかりません。skip します。\n"
        )
        return 0

    try:
        # Issue #3564: consumer では mypy が `[project.optional-dependencies] dev` に
        # のみ存在するため、`--extra dev` を付けないと fresh worktree（.venv 無し）で
        # `Failed to spawn: mypy` になり PR 作成を恒久ブロックする。
        result = subprocess.run(
            ["uv", "run", "--extra", "dev", "mypy", "src", "tests"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=_get_timeout_sec(),
        )
    except FileNotFoundError:
        sys.stderr.write(
            "WARN: require-mypy: uv が見つからないため skip します。"
            " docs/reference/hooks.md#require-mypypy 参照\n"
        )
        return 0
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"WARN: require-mypy: timeout ({_get_timeout_sec()}s) により skip します。"
            " docs/reference/hooks.md#require-mypypy 参照\n"
        )
        return 0

    if result.returncode == 0:
        return 0

    # 非 0 exit のうちツール未解決（Failed to spawn）は型エラーではなく
    # dev extra 未同期の環境問題として判別する（Issue #3564）。
    # 修正前は「ツールが未インストール」なのに型エラーと誤判定して表示し、
    # 解決手順も --extra dev 無しで同じ失敗を繰り返す無限ループに陥っていた。
    mypy_output = (result.stdout or "") + (result.stderr or "")
    if "Failed to spawn" in mypy_output:
        sys.stderr.write(
            "Blocked: mypy を解決できません（dev extra が同期されていません）。\n"
        )
        sys.stderr.write("\n")
        sys.stderr.write("原因: `uv run` は extra を自動同期しないため、mypy が\n")
        sys.stderr.write(
            "      `[project.optional-dependencies] dev` にのみ存在する consumer では\n"
        )
        sys.stderr.write("      ツール未解決（Failed to spawn）になります。\n")
        sys.stderr.write("\n")
        sys.stderr.write("解決手順:\n")
        sys.stderr.write(f"  cd {target_dir}\n")
        sys.stderr.write("  uv sync --extra dev\n")
        sys.stderr.write("  uv run --extra dev mypy src tests\n")
        sys.stderr.write(
            "  それでも失敗する場合は pyproject.toml の dev に mypy を追加してください\n"
        )
        sys.stderr.write("\n")
        sys.stderr.write("詳細: docs/reference/hooks.md#require-mypypy\n")
        return 2

    # 非 0 exit = 型エラー検出。stderr にレポートを出して block する。
    sys.stderr.write("Blocked: mypy (strict) エラーがあります。\n")
    sys.stderr.write("\n")
    sys.stderr.write("mypy 出力:\n")
    for line in mypy_output.splitlines():
        if line.strip():
            sys.stderr.write(f"  {line}\n")
    sys.stderr.write("\n")
    sys.stderr.write("解決手順:\n")
    sys.stderr.write(f"  cd {target_dir}\n")
    sys.stderr.write("  uv run --extra dev mypy src tests\n")
    sys.stderr.write("  型エラーを修正して commit → push 後に gh pr create を再実行\n")
    sys.stderr.write("\n")
    sys.stderr.write("詳細: docs/reference/hooks.md#require-mypypy\n")
    return 2


def main() -> int:
    if not is_hook_enabled("require-mypy"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
