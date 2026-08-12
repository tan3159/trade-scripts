#!/usr/bin/env python3
"""PreToolUse hook: `tidd issue-next-timing mark` 指示の書き込みを機械強制ブロックする.

Issue #3559: `issue-next-timing mark` サブコマンドは廃止した。計測境界の記録は
hook / ツール自己記録（record-timing-boundaries / pre-flight / ai-review /
check-pr-conflicts / cleanup-merged-branch）に一本化されており、md へ mark 実行
指示を 1 行足すと二重記録（`record_event_safe` は非冪等）を再生産する。

本 hook は将来の Issue で「md に mark 指示を 1 行足す」という近道が物理的に
選べなくなるよう、`.claude/`・`.codex/`・`templates/`・`docs/` 配下の
`*.md` / `*.toml` への書き込み内容に `issue-next-timing mark`（`mark-quality-check-done`
を除く）が含まれる場合に exit 2 でブロックする。

- `mark-quality-check-done`（require-quality-check.py の証跡・#3158）は廃止対象外
- `docs/decisions/` は過去記録のため対象外
- 対象外パス・対象外ツール・`is_hook_enabled` false のときは no-op（exit 0）

stdlib のみ使用（`tidd_tools` は import しない）。
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
    is_file_edit_tool,
    is_hook_enabled,
    read_hook_input,
)

#: 対象ディレクトリ（リポジトリルート直下の top-level ディレクトリ名）。
#: `.claude/`・`.codex/`・`templates/`・`docs/` 配下の md / toml を対象とする。
_TARGET_DIRS: frozenset[str] = frozenset({".claude", ".codex", "templates", "docs"})

#: 対象拡張子（md / toml 限定・#3559）。
_TARGET_SUFFIXES: frozenset[str] = frozenset({".md", ".toml"})

#: `docs/decisions/` 配下は過去記録のため対象外。
_EXCLUDED_DOCS_DECISIONS: tuple[str, str] = ("docs", "decisions")

#: `issue-next-timing mark`（`mark-quality-check-done` を除く）を検出する。
#: `mark-quality-check-done` は廃止対象外のため、`mark` 直後に `-quality-check-done`
#: が続く場合はブロックしない（negative lookahead）。
_BLOCKED_MARK_RE = re.compile(r"issue-next-timing\s+mark(?!-quality-check-done)")


def _is_target_file(file_path: str) -> bool:
    """対象ファイル（`.claude/`・`.codex/`・`templates/`・`docs/` 配下の md/toml）か判定する.

    相対パス・絶対パス（`/home/<user>/<repo>/.claude/...`）の両方に対応する。
    パスの構成要素（`Path.parts`）のいずれかに対象ディレクトリが含まれ、
    かつ拡張子が md / toml であれば対象とみなす。`docs/decisions/` 配下は除外。
    """
    if Path(file_path).suffix.lower() not in _TARGET_SUFFIXES:
        return False
    parts = [part for part in Path(file_path).parts if part not in ("", ".", "..")]
    if not parts:
        return False
    # docs/decisions/ 配下は過去記録のため対象外
    for i, part in enumerate(parts):
        if (
            part == _EXCLUDED_DOCS_DECISIONS[0]
            and i + 1 < len(parts)
            and parts[i + 1] == _EXCLUDED_DOCS_DECISIONS[1]
        ):
            return False
    return any(part in _TARGET_DIRS for part in parts)


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)
    if not is_file_edit_tool(tool_name):
        return 0

    file_path = get_file_path(payload)
    if not file_path:
        return 0
    if not _is_target_file(file_path):
        return 0

    content = get_new_content(payload)
    if content is None:
        return 0
    if not _BLOCKED_MARK_RE.search(content):
        return 0

    sys.stderr.write(
        f"Blocked: `tidd issue-next-timing mark` の実行指示は書き込めません（#3559）。\n"
        f"対象: {file_path}\n"
        "\n"
        "理由: `issue-next-timing mark` サブコマンドは廃止されました。\n"
        "     計測境界は hook / ツール自己記録で追加すること（詳細: "
        "`docs/reference/timing-log-schema.md`）。\n"
        "     mark を打つと二重記録（record_event_safe は非冪等）になり、\n"
        "     merge_summary のペアリングが壊れて「計測欠落」になります。\n"
        "\n"
        "詳細: docs/reference/hooks.md#ban-timing-mark-instructionpy\n"
    )
    return 2


def main() -> int:
    # Issue #1633: hook 機能別 on/off（opt-in・#2166）
    if not is_hook_enabled("ban-timing-mark-instruction"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
