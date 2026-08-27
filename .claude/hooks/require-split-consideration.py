#!/usr/bin/env python3
"""PreToolUse hook: 分割検討の根拠なしに issue-implementer 起動を禁止する (#3993).

issue-reviewer subagent が `size_over_1000_possible: true` を返しても、`verdict` の
合否には使われない非ブロッキングシグナルのため、オーケストレータが自分の見積もりで
上書きして STEP 2（issue-implementer 起動）へ直行できてしまう。実測では 2 回とも
シグナルが当たっており、無視すると実装 1 サイクルが丸ごと無駄になる事例が発生した
（Issue #3993 背景）。

本 hook は issue-implementer 起動直前に、統一日誌（timing-events/timing.db）の
`step1.5-quality-check` イベントの `meta.size_over_1000_possible` を確認し、
true の場合のみ対象 Issue に分割検討の根拠コメント（`## 分割検討` マーカー）が
存在するかを検証する。存在しなければ exit 2 でブロックする。

判定フロー:
  1. timing イベントが存在しない、または size_over_1000_possible が true でない
     → 対象外（exit 0・機械強制なし。false の場合は分割検討ステップ自体が不要）
  2. size_over_1000_possible が true → Issue コメントに `## 分割検討` マーカーが
     含まれるか `gh issue view --json comments` で確認する
     - 含まれる → exit 0（根拠あり）
     - 含まれない・gh 実行失敗 → exit 2（安全側。require-quality-check.py の
       `_has_quality_check_comment()` と同じ fail-closed 方針）

escape hatch: 環境変数 SKIP_SPLIT_CONSIDERATION_GATE=1 でバイパスできる。

stdlib のみ使用（gh コマンドのみ外部依存）。

Claude Code の Agent tool（tool_name="Agent"）と Codex の spawn_agent
（tool_name="spawn_agent"）の両スキーマに対応する（require-quality-check.py と同型・#3203）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import get_latest_timing_event_meta, is_hook_enabled, read_hook_input

DETAIL = "詳細: 上流リポジトリ本体の docs/reference/ 配下・`hooks.md#require-split-considerationpy`（consumer 未配布）\n"

_ISSUE_NUM_RE = re.compile(r"^Issue番号:\s*(\d+)\s*$")
_QUALITY_CHECK_STEP = "step1.5-quality-check"
_SPLIT_RATIONALE_COMMENT_MARKER = "## 分割検討"


def _has_split_rationale_comment(issue_num: int) -> bool:
    """Issue #N の GitHub コメントに `## 分割検討` マーカーが含まれるか確認する.

    gh コマンドが利用可能な場合のみ実行し、失敗時は安全側（False）に倒す
    （require-quality-check.py._has_quality_check_comment と同じ方針）。
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_num),
                "--json",
                "comments",
                "--jq",
                f'.comments[].body | select(contains("{_SPLIT_RATIONALE_COMMENT_MARKER}"))',
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")

    tool_name = str(payload.get("tool_name", ""))
    if tool_name not in {"Agent", "spawn_agent"}:
        return 0

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    subagent_type = str(
        tool_input.get("subagent_type") or tool_input.get("task_name") or ""
    ).replace("-", "_")
    if subagent_type != "issue_implementer":
        return 0

    if os.environ.get("SKIP_SPLIT_CONSIDERATION_GATE") == "1":
        return 0

    prompt = str(tool_input.get("prompt") or tool_input.get("message") or "")
    match = _ISSUE_NUM_RE.match(prompt)
    if not match:
        return 0

    issue_num = int(match.group(1))

    meta = get_latest_timing_event_meta(f"issue-{issue_num}", _QUALITY_CHECK_STEP)
    if not meta or meta.get("size_over_1000_possible") is not True:
        return 0  # size フラグが立っていない（または証跡なし）ため対象外

    if _has_split_rationale_comment(issue_num):
        return 0

    sys.stderr.write(
        f"BLOCK: Issue #{issue_num} は size_over_1000_possible: true と判定されましたが、\n"
        "分割検討の根拠コメント（`## 分割検討` マーカー）が確認できません（Issue #3993）。\n"
        "分割する場合は Epic 化してサブ Issue を作成するか、分割せず進む場合は想定 diff 行数の\n"
        "内訳を含む根拠コメントを投稿してから再実行してください。\n"
        "手順: `.claude/skills/issue-next/SKILL.md`「STEP 1.5-e: 規模警告時の分割検討」参照。\n"
    )
    sys.stderr.write(DETAIL)
    return 2


def main() -> int:
    if not is_hook_enabled("require-split-consideration"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
