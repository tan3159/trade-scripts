#!/usr/bin/env python3
"""PreToolUse hook: `gh pr create` 前に ruff format --check を gate する (Issue #1752).

**背景:** ruff 未整形コードが PR gate を通過して main にマージされ、翌 nightly の
`Run ruff format --check` step で初めて検知される事象が繰り返し発生している
(#1739 / #1741 / #1747)。auto-ruff-format PostToolUse hook は subprocess 失敗時に
silent 失敗（exit 0）する設計のため、network 断・外部エディタ経由の変更・過去 commit の
未整形コードを catch できない。

本 hook は `gh pr create` の Bash 呼び出しを検知して
`uv run --project projects/py/tidd_tools ruff format --check projects/py/tidd_tools`
を実行し、未整形ファイルがあれば `.venv/bin/ruff format` で自動整形し
`git add` + `git commit` 後に exit 0 で PR 作成を継続する（Issue #1934 Phase B）。

**skip する条件（exit 0 + stderr に WARN）:**

- `uv` が PATH に存在しない
- 対象ディレクトリ `projects/py/tidd_tools/` が存在しない
- ruff format --check が `REQUIRE_RUFF_FORMAT_TIMEOUT_SEC`（default 60s）で timeout

**exit 2 でブロックする条件:**

- `.venv/bin/ruff` が存在しない（自動整形不可）
- `ruff format` の実行が失敗した
- `ruff format` 後も `ruff format --check` が通らない（整形が不完全）

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
_TARGET_SUBDIR = "projects/py/tidd_tools"
_DEFAULT_TIMEOUT_SEC = 60
_ISSUE_NUM_RE = re.compile(r"issue-(\d+)")


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
    raw = os.environ.get("REQUIRE_RUFF_FORMAT_TIMEOUT_SEC")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _DEFAULT_TIMEOUT_SEC


def _get_current_branch(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _build_commit_message(branch: str | None) -> str:
    """ブランチ名から Issue 番号を抽出して commit メッセージを生成する."""
    if branch:
        m = _ISSUE_NUM_RE.search(branch)
        if m:
            return f"style: #{m.group(1)} ruff format 自動適用"
    return "style: ruff format 自動適用"


def _apply_ruff_format_and_commit(
    repo_root: Path,
    venv_ruff: Path,
    target_subdir: str,
) -> int:
    """ruff format 自動整形 → git add → git commit を実行する.

    成功: 0、失敗: 2 を返す。
    """
    # ruff format <target_subdir> を実行
    try:
        fmt_result = subprocess.run(
            [str(venv_ruff), "format", target_subdir],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=_get_timeout_sec(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(
            f"Blocked: require-ruff-format: ruff format の実行に失敗しました: {e}\n"
            " docs/reference/hooks.md#require-ruff-formatpy 参照\n"
        )
        return 2

    if fmt_result.returncode != 0:
        sys.stderr.write(
            "Blocked: require-ruff-format: ruff format が失敗しました。\n"
            f"{fmt_result.stderr}\n"
            " docs/reference/hooks.md#require-ruff-formatpy 参照\n"
        )
        return 2

    # 整形後に --check を再実行して完全性を確認
    try:
        check_result = subprocess.run(
            [str(venv_ruff), "format", "--check", target_subdir],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=_get_timeout_sec(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(
            f"Blocked: require-ruff-format: ruff format --check (整形後確認) に失敗: {e}\n"
            " docs/reference/hooks.md#require-ruff-formatpy 参照\n"
        )
        return 2

    if check_result.returncode != 0:
        sys.stderr.write(
            "Blocked: require-ruff-format: ruff format 後も未整形ファイルが残っています。\n"
            f"{check_result.stderr}\n"
            " docs/reference/hooks.md#require-ruff-formatpy 参照\n"
        )
        return 2

    # git add
    try:
        add_result = subprocess.run(
            ["git", "add", "-u", "--", target_subdir],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(
            f"Blocked: require-ruff-format: git add に失敗しました: {e}\n"
            " docs/reference/hooks.md#require-ruff-formatpy 参照\n"
        )
        return 2

    if add_result.returncode != 0:
        sys.stderr.write(
            f"Blocked: require-ruff-format: git add が失敗しました。\n{add_result.stderr}\n"
        )
        return 2

    # commit メッセージを構築
    branch = _get_current_branch(repo_root)
    commit_msg = _build_commit_message(branch)

    # git commit
    try:
        commit_result = subprocess.run(
            ["git", "commit", "--no-gpg-sign", "-m", commit_msg],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(
            f"Blocked: require-ruff-format: git commit に失敗しました: {e}\n"
            " docs/reference/hooks.md#require-ruff-formatpy 参照\n"
        )
        return 2

    if commit_result.returncode != 0:
        sys.stderr.write(
            f"Blocked: require-ruff-format: git commit が失敗しました。\n{commit_result.stderr}\n"
        )
        return 2

    sys.stderr.write(
        f"require-ruff-format: ruff format 自動適用 + commit 完了。PR 作成を継続します。\n"
        f"  commit: {commit_msg!r}\n"
        f"  詳細: docs/reference/hooks.md#require-ruff-formatpy\n"
    )
    return 0


def _main() -> int:
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

    target_dir = repo_root / _TARGET_SUBDIR
    if not target_dir.is_dir():
        sys.stderr.write(
            f"WARN: require-ruff-format: 対象ディレクトリ {_TARGET_SUBDIR} が見つかりません。skip します。\n"
        )
        return 0

    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                _TARGET_SUBDIR,
                "ruff",
                "format",
                "--check",
                _TARGET_SUBDIR,
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=_get_timeout_sec(),
        )
    except FileNotFoundError:
        sys.stderr.write(
            "WARN: require-ruff-format: uv が見つからないため skip します。"
            " docs/reference/hooks.md#require-ruff-formatpy 参照\n"
        )
        return 0
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"WARN: require-ruff-format: timeout ({_get_timeout_sec()}s) により skip します。"
            " docs/reference/hooks.md#require-ruff-formatpy 参照\n"
        )
        return 0

    if result.returncode == 0:
        return 0

    # 非 0 exit = 未整形ファイル検出。.venv/bin/ruff で自動整形を試みる（Issue #1934）。
    venv_ruff = repo_root / ".venv" / "bin" / "ruff"
    if not venv_ruff.is_file():
        # ruff 不在 → 従来どおり exit 2 でブロック
        ruff_output = (result.stdout or "") + (result.stderr or "")
        sys.stderr.write("Blocked: ruff format 未適用のファイルがあります。\n")
        sys.stderr.write("\n")
        sys.stderr.write("ruff 出力:\n")
        for line in ruff_output.splitlines():
            if line.strip():
                sys.stderr.write(f"  {line}\n")
        sys.stderr.write("\n")
        sys.stderr.write("解決手順:\n")
        sys.stderr.write(f"  cd {repo_root}\n")
        sys.stderr.write(
            "  uv run --project projects/py/tidd_tools ruff format projects/py/tidd_tools\n"
        )
        sys.stderr.write(
            "  git add -u -- projects/py/tidd_tools && git commit -m 'style: apply ruff format'\n"
        )
        sys.stderr.write("  git push\n")
        sys.stderr.write("\n")
        sys.stderr.write("詳細: docs/reference/hooks.md#require-ruff-formatpy\n")
        return 2

    # .venv/bin/ruff が存在する → 自動整形 + commit → exit 0 で継続
    return _apply_ruff_format_and_commit(repo_root, venv_ruff, _TARGET_SUBDIR)


def main() -> int:
    if not is_hook_enabled("require-ruff-format"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
