#!/usr/bin/env python3
"""PreToolUse hook: `gh pr create` 前に mypy (strict) を gate する (Issue #1892).

**背景:** mypy strict は `.circleci/config.yml` の CI ステップでのみ実行されており、
`gh pr create` 前にチェックするローカル hook が存在しない。これにより LLM の自律
ループが「コードを書く → PR 作成 → CI 数分待ち → mypy fail 検知 → fix → push →
CI 再待ち」という往復コストを毎 PR で踏んでいる。ruff format と同様に
`require-ruff-format.py`（#1752）と同構造でローカル gate に前出しする。

本 hook は `gh pr create` の Bash 呼び出しを検知して `projects/py/` 配下から
検査対象プロジェクト（`src/` を持つディレクトリ）を動的に検出し、そのディレクトリを
cwd に `uv run mypy src/` を実行し、型エラーがあれば exit 2 で block する。
（repo root から `mypy src/` は解決できないため、CI ステップと同じくプロジェクト
ディレクトリを cwd にして実行する。）

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
import re
import subprocess
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
_PROJECTS_PY_SUBDIR = "projects/py"
_PREFERRED_PROJECT_NAME = "tidd_tools"
_DEFAULT_TIMEOUT_SEC = 120


def _find_target_dir(repo_root: Path) -> Path | None:
    """`projects/py/` 配下から検査対象プロジェクトを検出する（Issue #2278）.

    consumer では `projects/py/tidd_tools` が存在しないため、`src/` を持つ
    プロジェクトディレクトリを動的に探す。`tidd_tools` が存在する場合は
    handbook 自身の従来挙動を保つため優先的に選ぶ。
    """
    projects_py = repo_root / _PROJECTS_PY_SUBDIR
    if not projects_py.is_dir():
        return None
    preferred = projects_py / _PREFERRED_PROJECT_NAME
    if (preferred / "src").is_dir():
        return preferred
    candidates = sorted(
        p for p in projects_py.iterdir() if p.is_dir() and (p / "src").is_dir()
    )
    return candidates[0] if candidates else None


def _git_toplevel() -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip())


def _get_timeout_sec() -> int:
    raw = os.environ.get("REQUIRE_MYPY_TIMEOUT_SEC")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _DEFAULT_TIMEOUT_SEC


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
    if not _GH_PR_CREATE_RE.search(command):
        return 0

    repo_root = _git_toplevel()
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
        result = subprocess.run(
            ["uv", "run", "mypy", "src/"],
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

    # 非 0 exit = 型エラー検出。stderr にレポートを出して block する。
    mypy_output = (result.stdout or "") + (result.stderr or "")
    sys.stderr.write("Blocked: mypy (strict) エラーがあります。\n")
    sys.stderr.write("\n")
    sys.stderr.write("mypy 出力:\n")
    for line in mypy_output.splitlines():
        if line.strip():
            sys.stderr.write(f"  {line}\n")
    sys.stderr.write("\n")
    sys.stderr.write("解決手順:\n")
    sys.stderr.write(f"  cd {target_dir}\n")
    sys.stderr.write("  uv run mypy src/\n")
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
