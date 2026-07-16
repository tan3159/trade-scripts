#!/usr/bin/env python3
"""PreToolUse hook: `Path(__file__)[.resolve()].parents[<数字>]` の新規追加をブロックする (Issue #1423 / #2077).

Issue #1379 で `parents[N]` → `_find_repo_root` fixture への移行が実施されたが、
Issue #1419（mutmut-weekly が動かない件）の再発で残存 3 ファイルが判明した。
新規追加を機械強制ブロックして「モグラたたき」を止める。
Issue #2077: `.resolve()` を挟まない bare `Path(__file__).parents[N]` が検出網をすり抜けていたため、
`.resolve()` の有無を問わず検出するよう拡張した。

対象:
  - Edit / Write の `projects/py/tidd_tools/tests/**/*.py`
  - 新内容（tool_input `content` / `new_string` / disk fallback）に
    `Path(__file__).parents[<数字>]`（`.resolve()` の有無を問わない）パターンを検出したら exit 2

対象外:
  - Non-Edit/Write tools
  - .py 以外のファイル
  - tests/ 配下ではない .py（src/tidd_tools/ 等）
  - docstring / コメント内の `parents[N]` プレースホルダ（数字ではないため regex マッチしない）

代替:
  - `from tests.conftest import _find_repo_root`
  - `repo_root` fixture（session-scoped, conftest.py 定義）

stdlib のみ使用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import get_file_path, get_tool_name, is_hook_enabled, read_hook_input  # noqa: E402

# `Path(__file__)[.resolve()].parents[<数字>]` を検出（.resolve() 任意・プレースホルダ `[N]` はマッチしない）
_PARENTS_N_RE = re.compile(r"Path\(__file__\)(?:\.resolve\(\))?\.parents\[\d+\]")

# 対象パス: projects/py/tidd_tools/tests/ 配下の .py（regressions/ 含む）
_TARGET_PATH_RE = re.compile(r"(^|/)projects/py/tidd_tools/tests/.+\.py$")


def _get_new_content(payload: dict) -> str | None:
    """tool_input から Edit/Write の新内容を取り出す."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    for key in ("content", "new_string", "new_str", "replacement"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return None


def _read_from_disk(file_path: str) -> str | None:
    p = Path(file_path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)
    if tool_name not in {"Edit", "Write"}:
        return 0

    file_path = get_file_path(payload)
    if not file_path:
        return 0

    # .py 以外は対象外
    if Path(file_path).suffix.lower() != ".py":
        return 0

    # tests/ 配下でなければ対象外
    if not _TARGET_PATH_RE.search(file_path):
        return 0

    content = _get_new_content(payload)
    if content is None:
        content = _read_from_disk(file_path)
    if content is None:
        return 0

    match = _PARENTS_N_RE.search(content)
    if not match:
        return 0

    sys.stderr.write(
        "`Path(__file__).parents[N]`（.resolve() の有無を問わない）パターンの新規追加は禁止されています "
        "（Issue #1379 / #1423 / #2077）。\n"
        f"検出: {match.group(0)}\n"
        f"対象: {file_path}\n"
        "\n"
        "テストを特定の方法（ミューテーションテスト）で実行するとパスの解決に失敗してテストが壊れます。\n"
        "\n"
        "代替コード（テストファイル内で自動的に利用できます）:\n"
        "  from tests.conftest import _find_repo_root\n"
        "  repo_root = _find_repo_root(Path(__file__))\n"
        "\n"
        "または `repo_root` session-scoped fixture（conftest.py 定義）を引数で受け取ってください。\n"
        "詳細: `docs/reference/hooks.md#ban-parents-npy`\n"
    )
    return 2


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("ban-parents-n"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
