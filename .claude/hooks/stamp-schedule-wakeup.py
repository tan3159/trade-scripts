#!/usr/bin/env python3
"""PostToolUse hook: `ScheduleWakeup` 呼び出し直後に待機予定を記録する（Issue #4050）.

**背景:** `require-issue-next-completion.py`（Stop hook）は issue-next state の
`current_issue` が設定されている間、turn 終了のたびに無条件で exit 2（Stop ブロック）を
返していた。`/issue-next-all` 実行中に issue-implementer subagent の完了を
`ScheduleWakeup(delaySeconds=1800, ...)` で待とうとしても、本 hook が exit 2 で turn 終了
（sleep）そのものをブロックしてしまい、指定した待機時間が一切尊重されず直後の turn で
即座に再ループする事象が観測された（Issue #4048・7 回以上連続呼び出しでほぼ待機なし）。
Issue #3868 は同一警告の連続出力という症状（メッセージ縮小）のみに対応しており、
待機時間そのものが無視される根本原因は未解決だった。

**対策:** `ScheduleWakeup` ツール呼び出しの直後に発火する本 PostToolUse hook が、
その呼び出し専用の payload から `session_id` / `tool_input.delaySeconds` を取り出して
`cache/schedule-wakeup/session-<session_id>.json` に `{"scheduled_at": ..., "delay_seconds": ...}`
を記録する（`stamp-issue-next-session.py` #3779 と同型のパターン。ScheduleWakeup 呼び出し自体は
Claude Code ハーネス内で完結し、hook 以外の経路で `delaySeconds` を知る手段が無いため）。
`require-issue-next-completion.py` はこの記録を読み、記録時刻から `delay_seconds` 未満の
経過時間であれば Stop をブロックしない（待機時間を実際に尊重する）。

同一セッションで複数回 `ScheduleWakeup` が呼ばれた場合はマーカーを上書きする（sliding window。
直近の呼び出しが待機時間の起点になる）。

hook 失敗原則（`docs/reference/hooks.md` §失敗原則 参照）:
  - 対象ツールでない・`session_id`/`delaySeconds` が取れない等はすべて no-op（exit 0）
  - 記録の成否を stderr にログする（silent success だが可視化のためログは出す）

`config.json` で default ON（`stamp-issue-next-session` #3826 と同じ理由: 本 hook が書き込む
記録に `require-issue-next-completion.py` の待機共存機能が依存するため、config.json 未設定でも
動作する必要がある）。

stdlib のみ使用。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (  # type: ignore[import-not-found]
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_STATE_SUBDIR = "schedule-wakeup"


def _resolve_state_dir(payload: dict) -> Path:
    """schedule-wakeup マーカーディレクトリを解決する（stamp-issue-next-session.py と同規則）.

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

    if get_tool_name(payload) != "ScheduleWakeup":
        return 0

    session_id = payload.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        sys.stderr.write(
            "stamp-schedule-wakeup: skip: payload に session_id がありません\n"
        )
        return 0

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        sys.stderr.write(
            "stamp-schedule-wakeup: skip: tool_input が dict ではありません\n"
        )
        return 0

    delay_raw = tool_input.get("delaySeconds")
    if delay_raw is None:
        sys.stderr.write(
            "stamp-schedule-wakeup: skip: tool_input.delaySeconds を取得できません\n"
        )
        return 0
    try:
        delay_seconds = int(delay_raw)
    except (TypeError, ValueError):
        sys.stderr.write(
            "stamp-schedule-wakeup: skip: tool_input.delaySeconds を取得できません\n"
        )
        return 0
    if delay_seconds <= 0:
        sys.stderr.write(
            "stamp-schedule-wakeup: skip: delaySeconds が正の値ではありません\n"
        )
        return 0

    state_dir = _resolve_state_dir(payload)
    marker_path = state_dir / f"session-{session_id}.json"
    now = datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {"scheduled_at": now, "delay_seconds": delay_seconds},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        sys.stderr.write(
            f"stamp-schedule-wakeup: マーカーファイルへの書き込みに失敗しました: {exc}\n"
        )
        return 0

    sys.stderr.write(
        f"stamp-schedule-wakeup: ScheduleWakeup 待機を記録しました "
        f"(session={session_id}, delaySeconds={delay_seconds})\n"
    )
    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off（Issue #4050: default ON。理由は本ファイル docstring 参照）
    if not is_hook_enabled("stamp-schedule-wakeup"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
