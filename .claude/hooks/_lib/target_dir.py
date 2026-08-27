"""hook 共通の対象プロジェクトディレクトリ・timeout 解決ヘルパー（Issue #2958）.

**背景:** `require-mypy.py` と `require-ruff-format.py` は `projects/py/` 配下から
検査対象プロジェクトを検出するロジック（`_find_target_dir`）と、環境変数経由の
timeout 秒数を解決するロジック（`_get_timeout_sec`）を丸ごとコピペで重複定義して
おり、`_find_target_dir` は Issue #2278 で mypy 側にのみ `src/` 存在チェックが
追加されたことで既に乖離していた。

`src/` チェックの有無は事故ではなく意図的な差分:
  - require-mypy.py: `uv run mypy src tests` を実行するため、`src/` を持たない
    プロジェクトはそもそも検査対象になり得ない（存在確認が必須）。
  - require-ruff-format.py: `ruff format <target_dir 全体>` を実行するため、
    `src/` の有無に関わらずプロジェクトディレクトリ自体が対象になり得る。

そのため本モジュールでは `require_src` 引数で挙動を明示的に指定させる（暗黙の
統一で正当な差分を消さない）。

**`require_tests`（Issue #4012）:** vendor 配布された `tidd_tools`（`tests/` 非同梱・
Issue #3979）が優先プロジェクトとして選ばれ続けると、`tests/` を持つ他の実プロジェクト
（consumer が実際に開発対象とするプロジェクト）が mypy 検査対象から漏れる。
`require_tests=True` を指定すると、優先プロジェクトが `tests/` を欠く場合に
**選定自体を** `tests/` を持つ他候補へフォールバックする（`mypy_targets` を
`src` のみへ縮小する方式（#3998）とは異なり、検査対象プロジェクト自体を切り替える。
consumer #915 の実装を upstream の正式仕様として統合したもの）。

stdlib のみ使用。
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECTS_PY_SUBDIR = "projects/py"
PREFERRED_PROJECT_NAME = "tidd_tools"


def find_target_dir(
    repo_root: Path,
    *,
    require_src: bool,
    require_tests: bool = False,
    projects_py_subdir: str = PROJECTS_PY_SUBDIR,
    preferred_name: str = PREFERRED_PROJECT_NAME,
) -> Path | None:
    """`projects/py/` 配下から検査対象プロジェクトディレクトリを検出する（Issue #2278 / #2958 / #4012）.

    consumer レイアウトでは `projects/py/tidd_tools` が存在しないため、条件を
    満たすプロジェクトディレクトリを動的に探す。`tidd_tools` が存在する場合は
    handbook 自身の従来挙動を保つため優先的に選ぶ（ただし `require_tests=True` で
    `tidd_tools` が `tests/` を欠く場合は他候補へフォールバックする・Issue #4012）。

    Args:
        repo_root: リポジトリルート。
        require_src: True の場合 `src/` サブディレクトリの存在を候補の条件に含める
            (require-mypy.py 用)。False の場合はディレクトリ自体の存在のみで
            判定する (require-ruff-format.py 用)。
        require_tests: True の場合 `tests/` サブディレクトリの存在も候補の条件に
            含める（既定 False・後方互換）。優先プロジェクトが `tests/` を欠く場合、
            `tests/` を持つ他候補へ選定自体をフォールバックする（Issue #4012）。
        projects_py_subdir: 探索対象のサブディレクトリ（既定 `projects/py`）。
        preferred_name: 優先的に選ぶプロジェクト名（既定 `tidd_tools`）。

    Returns:
        検出したプロジェクトディレクトリの絶対 Path。見つからなければ None。
    """

    def _is_valid(p: Path) -> bool:
        if not p.is_dir():
            return False
        if require_src and not (p / "src").is_dir():
            return False
        return not (require_tests and not (p / "tests").is_dir())

    projects_py = repo_root / projects_py_subdir
    if not projects_py.is_dir():
        return None
    preferred = projects_py / preferred_name
    if _is_valid(preferred):
        return preferred
    candidates = sorted(p for p in projects_py.iterdir() if _is_valid(p))
    return candidates[0] if candidates else None


def get_timeout_sec(env_var: str, default: int) -> int:
    """環境変数から timeout 秒数を解決する。未設定・不正値なら default を返す（最小 1 秒）."""
    raw = os.environ.get(env_var)
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return default
