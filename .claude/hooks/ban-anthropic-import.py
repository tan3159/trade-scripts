#!/usr/bin/env python3
"""PreToolUse hook: Anthropic SDK の直接インポートを機械強制ブロックする.

このリポジトリでは Anthropic API を Python から直接呼び出す実装は禁止されています。
`docs/reference/claude-api.md` の方針に反する `import anthropic` / `from anthropic import` /
動的 import / `pyproject.toml` / `requirements*.txt` の依存宣言を検出して exit 2 でブロックする。

Issue #1281（hard rule・override マーカーなし）。

対象:
  - Python (.py) の `import anthropic` / `from anthropic import` /
    `import anthropic as X` / `importlib.import_module("anthropic")`
  - `pyproject.toml` の依存宣言に `anthropic` パッケージが含まれるケース
  - `requirements*.txt` の依存に `anthropic` パッケージが含まれるケース

対象外:
  - Markdown（`.md`）: `docs/research/**` 等の例示コードブロックは許可

stdlib のみ使用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import get_file_path, get_tool_name, is_hook_enabled, read_hook_input  # noqa: E402

# Python import 検出パターン
# - `import anthropic` / `import anthropic as X` / `import os, anthropic` / `import anthropic, os` 等
#   `import <modules>` 行に anthropic モジュールが単独トークンとして出現するかを (?<![\w.]) / (?![\w.]) で判定
_IMPORT_ANTHROPIC_RE = re.compile(
    r"^\s*import\s+[^\n#]*(?<![\w.])anthropic(?!\w)",
    re.MULTILINE,
)
# - `from anthropic import ...` / `from anthropic.foo import ...`
_FROM_ANTHROPIC_RE = re.compile(
    r"^\s*from\s+anthropic(\.[\w.]+)?\s+import\s+", re.MULTILINE
)
# - `importlib.import_module("anthropic")` (単引用符・二重引用符両方)
_DYNAMIC_IMPORT_RE = re.compile(
    r"""importlib\.import_module\s*\(\s*['"]anthropic['"]"""
)

# pyproject.toml の依存宣言検出
# - "anthropic" / 'anthropic' / "anthropic[extras]" / "anthropic>=X.Y" 等
# - lookahead で `"anthropic-plugin"` のような別パッケージを誤検知しないようにする
_TOML_DEP_RE = re.compile(
    r"""['"]anthropic(?:\[[\w,-]+\])?(?:\s*[<>=!~;][^"']*)?['"]""",
)
# requirements*.txt の依存宣言検出（case-insensitive で Anthropic / ANTHROPIC も検出）
# - 行頭に anthropic パッケージ名（オプションで [extras] や version specifier）
_REQ_TXT_DEP_RE = re.compile(
    r"^\s*anthropic(?:\[[\w,-]+\])?(?:\s*[<>=!~;].*)?\s*(?:#.*)?$",
    re.MULTILINE | re.IGNORECASE,
)


def _get_new_content(payload: dict) -> str | None:
    """tool_input から Write/Edit の新内容を取り出す（ない場合は None）.

    Claude Code の Edit / Write tool のキー名は環境差があり得るため、
    `content` / `new_string` / `new_str` / `replacement` を順に見る。
    """
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    for key in ("content", "new_string", "new_str", "replacement"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return None


def _read_from_disk(file_path: str) -> str | None:
    """ファイルをディスクから読み取る（存在しなければ None）."""
    p = Path(file_path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _check_python(content: str) -> str | None:
    """Python コードを検査し、違反があればエラーメッセージを返す."""
    if _IMPORT_ANTHROPIC_RE.search(content):
        return "`import anthropic` を検出しました"
    if _FROM_ANTHROPIC_RE.search(content):
        return "`from anthropic import ...` を検出しました"
    if _DYNAMIC_IMPORT_RE.search(content):
        return '`importlib.import_module("anthropic")` を検出しました'
    return None


def _check_pyproject_toml(content: str) -> str | None:
    if _TOML_DEP_RE.search(content):
        return "pyproject.toml の依存宣言に anthropic が含まれます"
    return None


def _check_requirements_txt(content: str) -> str | None:
    if _REQ_TXT_DEP_RE.search(content):
        return "requirements*.txt の依存宣言に anthropic が含まれます"
    return None


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")  # Issue #1364
    tool_name = get_tool_name(payload)
    if tool_name not in {"Edit", "Write"}:
        return 0

    file_path = get_file_path(payload)
    if not file_path:
        return 0

    name = Path(file_path).name.lower()
    suffix = Path(file_path).suffix.lower()

    # Markdown は例示コードブロックのため対象外
    if suffix == ".md":
        return 0

    # 新内容を取り出す（tool_input 経由 or ディスク）
    content = _get_new_content(payload)
    if content is None:
        content = _read_from_disk(file_path)
    if content is None:
        return 0

    error: str | None = None
    if suffix == ".py":
        error = _check_python(content)
    elif name == "pyproject.toml":
        error = _check_pyproject_toml(content)
    elif name.startswith("requirements") and name.endswith(".txt"):
        error = _check_requirements_txt(content)

    if error is None:
        return 0

    # pyproject.toml / requirements.txt はメッセージが違うので出し分け
    if suffix == ".py":
        sys.stderr.write(
            "このリポジトリでは Anthropic API を Python から直接呼び出す実装は禁止されています。\n"
            f"検出: {error}\n"
            f"対象: {file_path}\n"
            "代替: Claude Code のスキルやサブエージェント機能を使って実装してください。\n"
            "詳細: `docs/reference/claude-api.md` / `docs/reference/hooks.md#ban-anthropic-importpy`\n"
        )
    else:
        sys.stderr.write(
            f"{error}\n"
            f"対象: {file_path}\n"
            "このリポジトリでは Anthropic API パッケージの直接依存宣言は禁止されています。\n"
            "詳細: `docs/reference/claude-api.md` / `docs/reference/hooks.md#ban-anthropic-importpy`\n"
        )
    return 2


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("ban-anthropic-import"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
