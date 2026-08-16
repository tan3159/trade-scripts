#!/usr/bin/env python3
"""SessionStart hook: worktree 進入時に .venv を自動初期化する.

Issue #2596: worktree ディレクトリ内でセッション開始時に .venv が存在しない場合、
自動的に Python プロジェクトを検出して `uv venv` と `uv sync --extra dev`
を実行する。

判定ロジック:
- `.git` が file であれば worktree と判定（main リポジトリでは .git はディレクトリ）
- `projects/py/` 配下の `pyproject.toml` で対象プロジェクトを検出する
- 対象プロジェクトの `.venv/pyvenv.cfg` の有無で初期化済みを判定

失敗時の挙動（#2596 Scenario 3):
- `uv sync` がネットワークエラー等で失敗してもセッション起動をブロックしない（exit 0）
- stderr にエラーと手動実行の案内を出力する

機能キー: `session-start-venv-init`（config.json で on/off・default OFF）

テスト用エスケープハッチ:
- `SESSION_START_VENV_SKIP_SYNC=1` 環境変数でコマンド実行をスキップ（テスト用）

stdlib のみ使用。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import is_hook_enabled, read_hook_input

_HOOK_NAME = "session-start-venv-init"


def is_worktree(cwd: str) -> bool:
    """.git が file であれば worktree と判定して True を返す.

    main リポジトリでは .git はディレクトリ。worktree では .git は
    `gitdir: <path>` を含むファイルとなる。
    """
    git_path = Path(cwd) / ".git"
    return git_path.is_file()


def has_venv(cwd: str) -> bool:
    """.venv/pyvenv.cfg が存在すれば True を返す（空ディレクトリ対策）."""
    return (Path(cwd) / ".venv" / "pyvenv.cfg").is_file()


def find_python_project(cwd: str) -> Path | None:
    """`projects/py/` 配下から同期対象の Python プロジェクトを探す."""
    python_root = Path(cwd) / "projects" / "py"
    candidates = []
    if (python_root / "pyproject.toml").is_file():
        candidates.append(python_root)
    candidates.extend(
        child
        for child in sorted(python_root.iterdir())
        if child.is_dir() and (child / "pyproject.toml").is_file()
    ) if python_root.is_dir() else None
    return candidates[0] if candidates else None


def _run_command(cmd: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    """サブプロセスでコマンドを実行する."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        cwd=cwd,
        check=False,
    )


def _run_venv_init(cwd: str, project: Path | None = None) -> int:
    """venv の初期化を実行する.

    Returns:
        exit code（常に 0。セッション起動をブロックしない）
    """
    skip_sync = os.environ.get("SESSION_START_VENV_SKIP_SYNC") == "1"

    if skip_sync:
        sys.stderr.write(
            f"{_HOOK_NAME}: SESSION_START_VENV_SKIP_SYNC=1 が設定されています。"
            "uv コマンドの実行をスキップします（テスト用）。\n"
        )
        return 0

    legacy_direct_call = project is None
    project = project or Path(cwd)
    project_arg = (
        "projects/py/tidd_tools"
        if legacy_direct_call
        else str(project.relative_to(cwd))
    )
    sync_command = f"uv sync --project {project_arg} --extra dev"
    sys.stderr.write(
        f"{_HOOK_NAME}: {project_arg} で .venv が見つかりません。uv venv + uv sync を実行します...\n"
    )

    # uv venv --python 3.11 --clear
    try:
        venv_result = _run_command(
            ["uv", "venv", "--python", "3.11", "--clear"],
            cwd=cwd if legacy_direct_call else str(project),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(
            f"{_HOOK_NAME}: uv venv の実行に失敗しました: {e}\n"
            f"手動で実行してください: uv venv --python 3.11 --clear && {sync_command}\n"
        )
        return 0

    if venv_result.returncode != 0:
        sys.stderr.write(
            f"{_HOOK_NAME}: uv venv が失敗しました（exit {venv_result.returncode}）。\n"
            f"{venv_result.stderr}\n"
            f"手動で実行してください: uv venv --python 3.11 --clear && {sync_command}\n"
        )
        return 0

    # uv sync --project projects/py/tidd_tools --extra dev
    try:
        sync_result = _run_command(
            ["uv", "sync", "--project", project_arg, "--extra", "dev"],
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(
            f"{_HOOK_NAME}: uv sync の実行に失敗しました: {e}\n"
            f"手動で実行してください: {sync_command}\n"
        )
        return 0

    if sync_result.returncode != 0:
        sys.stderr.write(
            f"{_HOOK_NAME}: uv sync が失敗しました（exit {sync_result.returncode}）。\n"
            f"{sync_result.stderr}\n"
            f"手動で実行してください: {sync_command}\n"
        )
        return 0

    sys.stderr.write(f"{_HOOK_NAME}: .venv の初期化が完了しました。\n")
    return 0


def main_with_cwd(cwd: str) -> int:
    """指定された cwd で hook ロジックを実行する（テスト用に分離）."""
    if not is_hook_enabled(_HOOK_NAME):
        return 0

    if not is_worktree(cwd):
        return 0

    project = find_python_project(cwd)
    if project is None:
        sys.stderr.write(
            f"{_HOOK_NAME}: Python プロジェクトが見つかりません（projects/py/ 配下）。\n"
        )
        return 0

    if has_venv(str(project)):
        return 0

    return _run_venv_init(cwd, project)


def main() -> int:
    """SessionStart hook のエントリーポイント."""
    payload = read_hook_input(hook_name="SessionStart")

    # cwd は JSON の cwd フィールドまたはプロセス CWD から取得
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(cwd, str):
        cwd = os.getcwd()

    return main_with_cwd(cwd)


if __name__ == "__main__":
    sys.exit(main())
