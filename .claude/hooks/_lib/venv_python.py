"""hook 共通の venv 実行ファイル解決ヘルパー（Issue #3896）.

複数の hook が `.venv` 配下の実行ファイルパスを `.venv/bin/*`（POSIX レイアウト）で
決め打ちしており、Windows ネイティブの venv（`.venv/Scripts/*.exe`）では解決に失敗して
恒久的に skip/ブロックされていた（`require-red-first.py` の TDD RED-GREEN 順序ゲート・
`auto-ruff-format.py` の自動整形・`require-ruff-format.py` の自動整形フォールバック）。

`session-start-cache.py` が既に POSIX/Windows 両レイアウトを探索する実装を持っていた
（Issue #2211/#3087）ため、そのパターンを本モジュールへ切り出して各 hook から共有する。

stdlib のみ使用（hooks ディレクトリは外部依存を持たない）。
"""

from __future__ import annotations

from pathlib import Path


def find_venv_executable(root: Path, name: str) -> Path | None:
    """`root/.venv` 配下から実行ファイル ``name`` を OS レイアウト別に探索する.

    Args:
        root: venv を含むディレクトリ（通常はリポジトリルート）。
        name: 実行ファイル名（拡張子なし。例: ``"ruff"``, ``"python"``）。

    Returns:
        見つかった実行ファイルの絶対パス。POSIX レイアウト（``.venv/bin/<name>``）を
        優先し、なければ Windows レイアウト（``.venv/Scripts/<name>.exe``）を試す。
        どちらも存在しなければ ``None``。
    """
    for candidate in (
        root / ".venv" / "bin" / name,
        root / ".venv" / "Scripts" / f"{name}.exe",
    ):
        if candidate.is_file():
            return candidate
    return None


def find_venv_python(root: Path) -> Path | None:
    """`root/.venv` 配下の Python インタープリタを OS レイアウト別に探索する.

    POSIX venv には ``python3`` と ``python`` の両方が存在しうるため ``python3`` を
    優先して探索する。Windows venv（``.venv/Scripts``）には通常 ``python.exe`` のみ
    存在するため ``python3`` の探索は素通りして ``python`` にフォールバックする。

    Args:
        root: venv を含むディレクトリ（通常はリポジトリルート）。

    Returns:
        見つかった Python 実行ファイルの絶対パス。見つからなければ ``None``。
    """
    for name in ("python3", "python"):
        found = find_venv_executable(root, name)
        if found is not None:
            return found
    return None
