#!/usr/bin/env python3
"""PostToolUse hook: `tidd merge-summary report` 実行後に marker へ session_id を記録する.

**Issue #2752: merge-summary ゲートのセッション紐付け（根治）。**

背景: `.claude/hooks/on-stop.py` の merge-summary 転記チェックは
`cache/merge-summary-emitted/<N>.marker` の存在だけを見ており、marker に発行元セッションの
概念がない。複数ターミナルで同一ディレクトリを開いている場合、一方のターミナルで実行した
`tidd merge-summary report` が作った marker が、無関係な別ターミナルの会話を誤ってブロック
する事故が実際に発生した（#2733/#2735/#2742）。

本 hook は `tidd merge-summary report` を実行した Bash 呼び出しの PostToolUse payload から
`session_id` を取り出し、対応する marker ファイルに書き込む。marker の生成自体は
CLI（`merge_summary.py`）側の責務のまま変更しない。CLI サブプロセスは Claude Code の
session_id を知り得ない一方、この hook はその Bash 呼び出し専用の payload を受け取るため、
競合状態を持ち込まずに識別子を後付けできる。

on-stop.py の `_check_merge_summary_in_transcript()` はこの識別子を Stop hook payload の
session_id と照合し、不一致（別セッション発行）の marker は照合対象から除外する。
session_id が記録されていない旧 marker は後方互換として常に照合される。

hook 失敗原則（`docs/reference/hooks.md` §失敗原則 参照）:
  - 対象コマンドでない・session_id が取れない・marker が存在しない等はすべて no-op（exit 0）
  - 記録の成否を stderr にログする（silent success だが可視化のためログは出す）

stdlib のみ使用。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)
from _lib.issue_ref import ISSUE_KEY_RE as _ISSUE_KEY_RE

# `tidd merge-summary report ...` / `python -m tidd_tools merge-summary report ...` の両方を捕捉
_MERGE_SUMMARY_REPORT_RE = re.compile(r"merge-summary\s+report\b")
_BARE_NUMBER_RE = re.compile(r"^\d+$")
_MARKER_SUBDIR = "merge-summary-emitted"


def _extract_issue_num(command: str) -> str | None:
    """`tidd merge-summary report` コマンド文字列から Issue 番号を抜き出す.

    `merge_summary.py` の `_extract_issue_num()` と同じ規則
    （`issue-<N>` 形式優先、なければ裸の数値トークン）。
    """
    match = _ISSUE_KEY_RE.search(command)
    if match:
        return match.group(1)
    for token in command.replace('"', " ").replace("'", " ").split():
        if _BARE_NUMBER_RE.match(token):
            return token
    return None


def _resolve_marker_dir(payload: dict) -> Path:
    """merge-summary-emitted ディレクトリを解決する（on-stop.py と同規則）.

    優先順: ISSUE_NEXT_STATE_ROOT 環境変数 > payload の cwd > プロセス CWD。
    """
    root_override = os.environ.get("ISSUE_NEXT_STATE_ROOT", "")
    if root_override:
        return Path(root_override) / "cache" / _MARKER_SUBDIR
    payload_cwd = payload.get("cwd", "")
    if isinstance(payload_cwd, str) and payload_cwd:
        return Path(payload_cwd) / "cache" / _MARKER_SUBDIR
    return Path.cwd() / "cache" / _MARKER_SUBDIR


def _main() -> int:
    payload = read_hook_input(hook_name="PostToolUse")

    if get_tool_name(payload) != "Bash":
        return 0

    command = get_command(payload)
    if not command or not _MERGE_SUMMARY_REPORT_RE.search(command):
        return 0

    session_id = payload.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        sys.stderr.write(
            "stamp-merge-summary-session: skip: payload に session_id がありません\n"
        )
        return 0

    issue_num = _extract_issue_num(command)
    if not issue_num:
        sys.stderr.write(
            "stamp-merge-summary-session: skip: コマンドから Issue 番号を抽出できません\n"
        )
        return 0

    marker_dir = _resolve_marker_dir(payload)
    marker_path = marker_dir / f"{issue_num}.marker"
    if not marker_path.is_file():
        sys.stderr.write(
            f"stamp-merge-summary-session: skip: marker が見つかりません: {marker_path}\n"
        )
        return 0

    try:
        marker_path.write_text(session_id, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(
            f"stamp-merge-summary-session: marker への書き込みに失敗しました: {exc}\n"
        )
        return 0

    sys.stderr.write(
        f"stamp-merge-summary-session: marker に session_id を記録しました "
        f"(issue #{issue_num})\n"
    )
    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("stamp-merge-summary-session"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
