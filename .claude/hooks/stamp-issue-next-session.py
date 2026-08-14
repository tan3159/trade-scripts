#!/usr/bin/env python3
"""PostToolUse hook: `tidd issue-next-state init` 実行後に state ファイルへ session_id を記録する.

**Issue #3779: require-issue-next-completion.py の別セッション誤ブロック対策.**

背景: `.claude/hooks/require-issue-next-completion.py`（Stop hook）は issue-next state
ファイル（`cache/issue-next-state/issue-<N>.json`）を無条件に走査し、`current_issue` が
設定されていて TTL 内であればブロックする。state ファイルに発行元セッションの概念が無く、
複数ターミナルが並行稼働する運用では、別ターミナルが作業中の Issue の state が無関係な
セッションの stop を誤ってブロックする事故が実際に発生した（#3772 の実測ケース）。

本 hook は `tidd issue-next-state init ...` を実行した Bash 呼び出しの直後に発火し、
その Bash 呼び出し専用の PostToolUse payload から `session_id` を取り出して対応する
per-issue state ファイルへ書き込む。CLI サブプロセス自身は Claude Code の session_id を
知り得ない（hook payload にのみ含まれる情報）ため、`stamp-merge-summary-session.py`
（#2752）と同型のパターンで後付けする。

`require-issue-next-completion.py` はこの識別子を Stop hook payload の session_id と
照合し、不一致（別セッション発行）の state はブロック対象から除外する。session_id が
記録されていない旧 state は後方互換として常にチェック対象にする。

hook 失敗原則（`docs/reference/hooks.md` §失敗原則 参照）:
  - 対象コマンドでない・session_id が取れない・state ファイルが存在しない等はすべて no-op（exit 0）
  - 記録の成否を stderr にログする（silent success だが可視化のためログは出す）

stdlib のみ使用。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (  # type: ignore[import-not-found]
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)

# `tidd issue-next-state init ...` / `python -m tidd_tools issue-next-state init ...` の
# 両方を捕捉する（`consume`・`clear` 等の他サブコマンドは対象外）
_INIT_CMD_RE = re.compile(r"issue-next-state\s+init\b")
_BARE_NUMBER_RE = re.compile(r"^\d+$")
_STATE_SUBDIR = "issue-next-state"


def _extract_current_issue(command: str) -> str | None:
    """`init` コマンド文字列から `current` 位置引数（Issue 番号）を抜き出す.

    `init` は ``init [--enforce-session-limit] <current> [queue...] [--unattended]`` の形式
    （`issue_next_state.py` 参照）。`--` で始まるフラグは値を取らない bool flag のみのため、
    フラグをスキップして最初に現れる裸の数値トークンを `current` として扱う。
    """
    match = _INIT_CMD_RE.search(command)
    if not match:
        return None
    rest = command[match.end() :]
    for token in rest.replace('"', " ").replace("'", " ").split():
        if token.startswith("--"):
            continue
        if _BARE_NUMBER_RE.match(token):
            return token
        # 最初の非フラグトークンが数値でなければ抽出不能（想定外のコマンド形式）
        break
    return None


def _resolve_state_dir(payload: dict) -> Path:
    """issue-next-state ディレクトリを解決する（stamp-merge-summary-session.py と同規則）.

    優先順: ISSUE_NEXT_STATE_ROOT 環境変数 > payload の cwd > プロセス CWD。
    """
    root_override = os.environ.get("ISSUE_NEXT_STATE_ROOT", "")
    if root_override:
        return Path(root_override) / "cache" / _STATE_SUBDIR
    payload_cwd = payload.get("cwd", "")
    if isinstance(payload_cwd, str) and payload_cwd:
        return Path(payload_cwd) / "cache" / _STATE_SUBDIR
    return Path.cwd() / "cache" / _STATE_SUBDIR


def _main() -> int:
    payload = read_hook_input(hook_name="PostToolUse")

    if get_tool_name(payload) != "Bash":
        return 0

    command = get_command(payload)
    if not command or not _INIT_CMD_RE.search(command):
        return 0

    session_id = payload.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        sys.stderr.write(
            "stamp-issue-next-session: skip: payload に session_id がありません\n"
        )
        return 0

    issue_num = _extract_current_issue(command)
    if not issue_num:
        sys.stderr.write(
            "stamp-issue-next-session: skip: コマンドから Issue 番号を抽出できません\n"
        )
        return 0

    state_dir = _resolve_state_dir(payload)
    state_path = state_dir / f"issue-{issue_num}.json"
    if not state_path.is_file():
        sys.stderr.write(
            f"stamp-issue-next-session: skip: state ファイルが見つかりません: {state_path}\n"
        )
        return 0

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            f"stamp-issue-next-session: state ファイルの読み取りに失敗しました: {exc}\n"
        )
        return 0
    if not isinstance(data, dict):
        sys.stderr.write(
            "stamp-issue-next-session: skip: state ファイルの内容が dict ではありません\n"
        )
        return 0

    data["session_id"] = session_id
    try:
        state_path.write_text(
            json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        sys.stderr.write(
            f"stamp-issue-next-session: state ファイルへの書き込みに失敗しました: {exc}\n"
        )
        return 0

    sys.stderr.write(
        f"stamp-issue-next-session: state ファイルに session_id を記録しました (issue #{issue_num})\n"
    )
    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("stamp-issue-next-session"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
