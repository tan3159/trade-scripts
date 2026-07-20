#!/usr/bin/env python3
"""PreToolUse hook: 教科書非準拠の SKILL.md 編集をブロックする.

Anthropic 公式ベストプラクティス（skill-authoring.md）に準拠しない SKILL.md /
スキルサブファイルへの Edit / Write を exit 2 でブロックする。

チェック項目（`tidd_tools.skill_checker` に共通化済み・Issue #2112）:
  1. SKILL.md body（YAML frontmatter 除く）が 500 行未満
  2. YAML frontmatter に name（≤64 chars, lowercase + digits + hyphen only,
     reserved words 'anthropic'/'claude' を含まない）が存在
  3. YAML frontmatter に description（non-empty, ≤1024 chars）が存在
  4. 参照が 1 階層以内（SKILL.md → ref.md → nested.md は NG）
  5. 100 行超の reference .md に TOC（## Contents / ## 目次 等）が存在

対象パス:
  - `.claude/skills/**/SKILL.md`
  - `.claude/skills/**/*.md`（サブファイルも対象）

判定ロジック:
  優先: `tidd_tools.skill_checker`（projects/py/tidd_tools/src を同梱する本体開発環境）
  フォールバック: `.claude/hooks/_lib/skill_lint.py`（stdlib のみ・copier 配布先用）
  -> consumer リポジトリには projects/py/tidd_tools/src が存在しないため、
     _lib/skill_lint.py が常に機能することを保証する（Issue #2151）。

Issue #1764, #2112, #2151
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _find_tidd_tools_src() -> Path | None:
    """リポジトリルートを git rev-parse で探し tidd_tools src ディレクトリを返す."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        repo_root = Path(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    src = repo_root / "projects" / "py" / "tidd_tools" / "src"
    if src.is_dir():
        return src
    return None


def _add_tidd_tools_to_path() -> bool:
    """tidd_tools src を sys.path に追加して True を返す。失敗時は False。"""
    src = _find_tidd_tools_src()
    if src is None:
        return False
    src_str = str(src)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)
    return True


def _read_input() -> dict:
    """Claude Code が hook に渡す JSON を stdin から読み取る."""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _is_skill_md(file_path: str) -> bool:
    """対象パスが .claude/skills/**/SKILL.md かどうかを判定する."""
    parts = Path(file_path).parts
    try:
        idx = parts.index(".claude")
    except ValueError:
        return False
    if len(parts) < idx + 4:
        return False
    return parts[idx + 1] == "skills" and parts[-1] == "SKILL.md"


def _is_skill_subfile(file_path: str) -> bool:
    """対象パスが .claude/skills/**/*.md（SKILL.md 以外）かどうかを判定する."""
    parts = Path(file_path).parts
    try:
        idx = parts.index(".claude")
    except ValueError:
        return False
    if len(parts) < idx + 4:
        return False
    return (
        parts[idx + 1] == "skills"
        and parts[-1] != "SKILL.md"
        and parts[-1].endswith(".md")
    )


def _get_new_content(payload: dict) -> str | None:
    """tool_input から Write/Edit の新内容を取り出す.

    Write（content キーあり）は従来どおりそのまま返す。
    Edit（new_string キーあり）はディスク上の file_path の現内容を読み、
    old_string → new_string を適用した合成後の全文を返す（Issue #1971）。
    ディスクにファイルがない / old_string が見つからない場合は安全側として
    new_string 単体を返す（挙動を変えない）。
    """
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None

    # Write: content キーを優先
    content = tool_input.get("content")
    if isinstance(content, str):
        return content

    # Edit: new_string を old_string に適用した合成後全文を返す
    new_string = tool_input.get("new_string")
    if not isinstance(new_string, str):
        for key in ("new_str", "replacement"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
        return None

    raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
    old_string = tool_input.get("old_string")
    replace_all = bool(tool_input.get("replace_all", False))

    if isinstance(old_string, str) and raw_path:
        disk_content = _read_from_disk(raw_path)
        if disk_content is not None and old_string in disk_content:
            if replace_all:
                return disk_content.replace(old_string, new_string)
            else:
                return disk_content.replace(old_string, new_string, 1)

    return new_string


def _read_from_disk(file_path: str) -> str | None:
    """ファイルをディスクから読み取る."""
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
    payload = _read_input()
    tool_name = str(payload.get("tool_name", ""))
    if tool_name not in {"Edit", "Write"}:
        return 0

    tool_input = payload.get("tool_input", {}) or {}
    raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
    if not raw_path:
        return 0

    # 対象パスの判定
    is_skill = _is_skill_md(raw_path)
    is_subfile = _is_skill_subfile(raw_path)

    if not is_skill and not is_subfile:
        return 0

    # 新内容を取り出す（tool_input 経由 or ディスク）
    content = _get_new_content(payload)
    if content is None:
        content = _read_from_disk(raw_path)
    if content is None:
        return 0

    # tidd_tools.skill_checker に委譲（Issue #2112: ロジック共通化）
    # copier 配布先では tidd_tools が存在しないため _lib/skill_lint.py にフォールバック
    _add_tidd_tools_to_path()
    checker = None
    try:
        from tidd_tools import skill_checker as checker  # type: ignore[import-not-found]
    except ImportError:
        # フォールバック: _lib/skill_lint.py（stdlib のみ・copier 配布先用・Issue #2151）
        try:
            import skill_lint as checker  # type: ignore[import-not-found]
        except ImportError:
            pass

    if checker is None:
        sys.stderr.write(
            "validate-skill: skill_checker / skill_lint を import できませんでした。\n"
            "チェックをスキップします（hook は機能しません）。\n"
        )
        return 0

    if is_skill:
        errors = checker.check_skill_md(content, raw_path)
    else:
        errors = checker.check_subfile(content)

    if not errors:
        return 0

    sys.stderr.write("validate-skill: SKILL.md の品質チェックに失敗しました。\n")
    sys.stderr.write(f"対象: {raw_path}\n\n")
    for i, err in enumerate(errors, 1):
        sys.stderr.write(f"[{i}] {err}\n\n")
    sys.stderr.write(
        "詳細: docs/reference/skill-authoring-rules.md\n"
        "     docs/reference/hooks.md#validate-skillpy\n"
    )
    return 2


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    _lib_dir = Path(__file__).resolve().parent / "_lib"
    if str(_lib_dir) not in sys.path:
        sys.path.insert(0, str(_lib_dir))
    try:
        from hook_io import is_hook_enabled  # type: ignore[import-not-found]

        if not is_hook_enabled("validate-skill"):
            return 0
    except ImportError:
        pass

    return _main()


if __name__ == "__main__":
    sys.exit(main())
