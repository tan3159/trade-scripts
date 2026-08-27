"""tidd_tools CLI 起動時の .venv Python バージョン自動修復（Issue #2178）.

worktree 作成時に `uv venv --python 3.11 --clear` が省略されると
Python 3.14 等の誤ったバージョンで .venv が初期化され、
ruff / mypy が spawn に失敗して CI commit status が未送信のままマージされる問題を防ぐ。

対処:
1. 起動時に sys.version_info が 3.11 系かチェック
2. 違う場合は uv venv + uv sync を実行して os.execv で同一コマンドを再実行
3. 試行回数を環境変数でカウントし、上限 2 回超過時は FATAL メッセージを出して exit 1
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

from tidd_tools.shared.subprocess_runner import run as run_subprocess

REPAIR_ATTEMPT_ENV = "_TIDD_VENV_REPAIR_ATTEMPT"
REPAIR_LIMIT = 2
REQUIRED_MAJOR = 3
REQUIRED_MINOR = 11


def check_and_repair_venv() -> None:
    """Python バージョンを検査し、必要なら自動修復して同一コマンドを再実行する.

    - Python 3.11 系なら即 return（何もしない）
    - それ以外は修復処理を実行して os.execv で再起動
    - 試行上限（2回）超過時は FATAL メッセージを stderr に出して sys.exit(1)
    """
    if sys.version_info[:2] == (REQUIRED_MAJOR, REQUIRED_MINOR):
        return None

    attempt = int(os.environ.get(REPAIR_ATTEMPT_ENV, "0"))

    major, minor = sys.version_info[:2]

    if attempt >= REPAIR_LIMIT:
        print(  # noqa: T201
            f"FATAL: venv 修復に失敗しました（Python {major}.{minor}"
            f" → 3.11 への修復を {REPAIR_LIMIT} 回試みたが解決しなかった）",
            file=sys.stderr,
        )
        sys.exit(1)

    project_root = _find_project_root()
    print(  # noqa: T201
        f"venv を Python 3.11 に修復しました（現在: {major}.{minor}、試行回数: {attempt + 1}/{REPAIR_LIMIT}）",
        file=sys.stderr,
    )

    _run_repair(project_root)

    os.environ[REPAIR_ATTEMPT_ENV] = str(attempt + 1)

    _execv_with_new_venv(str(project_root / ".venv" / "bin" / "python"), list(sys.argv))
    return None


def _find_project_root() -> Path:
    """uv workspace root（優先）または最寄りの pyproject.toml を返す.

    uv workspace 構成では ``.venv`` は workspace root
    （``[tool.uv.workspace]`` を含む pyproject.toml のディレクトリ）に置かれるため、
    workspace member（``projects/py/tidd_tools`` 等）で ``uv venv --clear`` を
    実行すると空の ``.venv`` が member 配下に作られてしまう。
    Issue #2200 の CircleCI nightly 失敗はこれが原因。

    Issue #3848: 探索の起点は ``__file__``（tidd_tools 自身の配置場所）ではなく
    ``Path.cwd()``（呼び出し元のディレクトリ）にする。``uvx --from
    <spec>#subdirectory=projects/py/tidd_tools`` 経由の実行では ``__file__`` が
    uv のエフェメラルキャッシュ配下を指すため、``__file__`` 基準で探索すると
    呼び出し元の consumer リポジトリではなくキャッシュ配下を誤って解決してしまう。

    - 上方向に探索して ``[tool.uv.workspace]`` を含む pyproject.toml があれば
      その親ディレクトリを workspace root として返す。
    - 見つからなければ最寄りの pyproject.toml のディレクトリを返す（後方互換）。
    - それも見つからなければ、実行中の venv（``sys.prefix``）から project root を
      逆算する（Issue #4007）。``uv run --project <dir>`` は CWD を変更しないため、
      vendor 配布された consumer（``projects/py/tidd_tools`` のみに pyproject.toml が
      存在し、consumer リポジトリのルートには存在しない）のルートから実行すると、
      cwd 起点の上方向探索は vendor 先ディレクトリが cwd の子孫であるため永遠に
      見つからない。しかし uv は既にそのプロジェクト用の venv を
      ``sys.prefix``（``<dir>/.venv``）として解決済みであるため、そこから
      project root を逆算できる。
    - どちらも見つからなければ、consumer リポジトリを特定できなかったとみなし
      案内メッセージを表示して非ゼロで終了する（Issue #3848）。
    """
    current = Path.cwd()
    nearest_pyproject: Path | None = None
    for parent in [current, *current.parents]:
        pyproject = parent / "pyproject.toml"
        if not pyproject.exists():
            continue
        if nearest_pyproject is None:
            nearest_pyproject = parent
        try:
            content = pyproject.read_text(encoding="utf-8")
        except OSError:
            continue
        if "[tool.uv.workspace]" in content:
            return parent
    if nearest_pyproject is not None:
        return nearest_pyproject
    active_venv_root = _project_root_from_active_venv()
    if active_venv_root is not None:
        return active_venv_root
    _exit_no_project_root(current)


def _project_root_from_active_venv() -> Path | None:
    """実行中の venv（``sys.prefix``）から project root を逆算する（Issue #4007）.

    ``sys.prefix != sys.base_prefix`` は venv 内で実行中であることを示す。venv が
    ``.venv`` という名前で project root 直下に置かれている（``uv venv`` の既定配置）
    前提で、その親ディレクトリに ``pyproject.toml`` があれば project root とみなす。
    """
    if sys.prefix == sys.base_prefix:
        return None
    prefix = Path(sys.prefix)
    if prefix.name != ".venv":
        return None
    candidate = prefix.parent
    if (candidate / "pyproject.toml").is_file():
        return candidate
    return None


def _exit_no_project_root(start: Path) -> NoReturn:
    """consumer リポジトリを特定できない場合の案内メッセージを出して終了する（Issue #3848）."""
    print(  # noqa: T201
        f"FATAL: {start} から上方向に pyproject.toml が見つからず、"
        "consumer リポジトリのルートを特定できませんでした。"
        "consumer リポジトリのルート（またはその配下）から実行するか、"
        "consumer リポジトリを明示的に指定して再実行してください。",
        file=sys.stderr,
    )
    sys.exit(1)


def _run_repair(project_root: Path) -> None:
    """uv venv --python 3.11 --clear と uv sync --extra dev を実行する.

    `capture=False` で標準出力・標準エラーを継承する（修復進捗をユーザーにそのまま見せるため）。
    """
    run_subprocess(
        ["uv", "venv", "--python", "3.11", "--clear"],
        check=True,
        cwd=str(project_root),
        capture=False,
    )
    run_subprocess(
        ["uv", "sync", "--project", str(project_root), "--extra", "dev"],
        check=True,
        cwd=str(project_root),
        capture=False,
    )


def _execv_with_new_venv(python_path: str, argv: list[str]) -> None:
    """新しい .venv の Python で同一コマンドを os.execv により再実行する."""
    os.execv(python_path, [python_path] + argv)
