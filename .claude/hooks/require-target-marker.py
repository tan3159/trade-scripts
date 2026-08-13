#!/usr/bin/env python3
"""PreToolUse hook: 新規 pytest ファイルの `@pytest.mark.target_<basename>` 欠落をブロックする.

`.claude/rules/testing-framework.md`（#785）は新規 pytest ファイルへの
`@pytest.mark.target_<basename>` marker 付与を必須と定めているが、機械強制が無く
お願いベースだった。marker の無い新規テストファイルは `tidd test-plan` の marker
選択実行（#2972）から静かに除外され、一度も実行されないまま GREEN 扱いになる検知漏れが
起きる（Issue #3423）。

対象:
  - `Write` ツールが扱う `projects/py/<project>/tests/**/test_*.py`
  - **まだファイルが存在しない**（= 新規作成）パスのみ。既存ファイルへの Write は対象外
  - `conftest.py` は `test_*.py` パターンに含まれないため自動的に対象外

動作:
  - `tool_input.content` に `@pytest.mark.target_` が含まれなければ exit 2 でブロック
  - marker 名が対象 basename と一致するかまでは検証しない（意味理解が必要なため hook では扱わない）

stdlib のみ使用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (
    get_file_path,
    get_new_content,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)

_TARGET_TEST_PATH_RE = re.compile(r"^projects/py/[^/]+/tests/(?:.*/)?test_[^/]+\.py$")
# Issue #3423: testing-framework.md の記載例は decorator 形式（`@pytest.mark.target_...`）
# だが、実コードベースではモジュールレベル代入 `pytestmark = pytest.mark.target_...`
# （decorator 形式より約半数を占める慣行）も広く使われている（Issue #3423 実装調査）。
# `@` 有無どちらのスタイルにも "pytest.mark.target_" 部分文字列は共通して含まれるため、
# `@` を要求せずこのトークンで判定し、正当な新規テストファイルの誤ブロックを避ける。
_TARGET_MARKER_TOKEN = "pytest.mark.target_"


def _normalize_path(raw_path: str) -> str:
    """絶対パスなら `projects/py/...` 以下の相対パスへ変換を試みる（判定できなければそのまま）."""
    p = Path(raw_path)
    if not p.is_absolute():
        return raw_path
    parts = p.parts
    for i, part in enumerate(parts):
        if part == "projects" and i + 1 < len(parts) and parts[i + 1] == "py":
            return "/".join(parts[i:])
    return raw_path


def _resolve_disk_path(raw_path: str) -> Path:
    p = Path(raw_path)
    if p.is_absolute():
        return p
    return Path.cwd() / p


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)
    if tool_name != "Write":
        return 0

    raw_path = get_file_path(payload)
    if not raw_path:
        return 0

    normalized = _normalize_path(raw_path)
    if not _TARGET_TEST_PATH_RE.match(normalized):
        return 0

    # 既に存在するファイルへの Write（上書き）は対象外
    if _resolve_disk_path(raw_path).is_file():
        return 0

    content = get_new_content(payload)
    if content is not None and _TARGET_MARKER_TOKEN in content:
        return 0

    sys.stderr.write(
        "新規 pytest ファイルには @pytest.mark.target_<basename> が必須です"
        "（basename はテスト対象ファイル名・ハイフンはアンダースコアに変換）。\n"
        f"対象: {raw_path}\n"
        "\n"
        "付与例:\n"
        "  import pytest\n"
        "\n"
        "  pytestmark = pytest.mark.target_foo\n"
        "\n"
        "詳細: .claude/rules/testing-framework.md（#785）・docs/reference/hooks.md#require-target-markerpy\n"
    )
    return 2


def main() -> int:
    if not is_hook_enabled("require-target-marker"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
