#!/usr/bin/env python3
"""PreToolUse hook: issue-implementer 起動前に Issue 品質チェック証跡を検証する (#2770).

issue-implementer subagent が起動される前に、対象 Issue の品質チェック（STEP 1.5）が
実行済みであることを以下いずれかの証跡で確認する:

1. 統一日誌（timing-events/timing.db）の issue-<N> に "step1.5-quality-check" が存在する
2. Issue #N の GitHub コメントに "## Issue品質チェック結果" が含まれる

証跡がない場合は exit 2 でブロックし、'/issue-review <N>' の実行を促す。

escape hatch: 環境変数 SKIP_QUALITY_CHECK_GATE=1 でバイパスできる。

stdlib のみ使用（gh コマンドのみ外部依存）。

Claude Code の Agent tool（tool_name="Agent"）と Codex の spawn_agent
（tool_name="spawn_agent"）の両スキーマに対応する（#3203）:

  - Claude Code: tool_input.subagent_type / tool_input.prompt
  - Codex: tool_input.task_name / tool_input.message

Codex の task_name は snake_case が標準だが、Claude Code の agent 名
（kebab-case）との互換のため `-` を `_` に正規化して同一視する。

実物確認（Claude Code 2026-07-27 に transcript から確認した payload 構造）:
  {
    "tool_name": "Agent",
    "tool_input": {
      "subagent_type": "issue-implementer",
      "prompt": "Issue番号: 2770"
    }
  }

Codex の spawn_agent は以下で届く（#3203）:
  {
    "tool_name": "spawn_agent",
    "tool_input": {
      "task_name": "issue_implementer",
      "message": "Issue番号: 2770",
      "fork_turns": "all"
    }
  }
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import has_timing_event, is_hook_enabled, read_hook_input

DETAIL = "詳細: docs/reference/hooks.md#require-quality-checkpy\n"

_ISSUE_NUM_RE = re.compile(r"^Issue番号:\s*(\d+)\s*$")
_QUALITY_CHECK_STEP = "step1.5-quality-check"
_QUALITY_CHECK_COMMENT_MARKER = "## Issue品質チェック結果"


def _has_timing_mark(issue_num: int) -> bool:
    """統一日誌（timing-events/timing.db）に step1.5-quality-check が存在するか確認する（#3340）."""
    return has_timing_event(f"issue-{issue_num}", _QUALITY_CHECK_STEP)


def _has_quality_check_comment(issue_num: int) -> bool:
    """Issue #N の GitHub コメントに品質チェック結果が含まれるか確認する.

    gh コマンドが利用可能な場合のみ実行し、失敗時は安全側（False）に倒す。
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
                f'.comments[].body | select(contains("{_QUALITY_CHECK_COMMENT_MARKER}"))',
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


def _is_ai_review_timing_enabled() -> bool:
    """config.json の ``ai-review-timing`` が有効か判定する（Issue #3158・stdlib のみ）.

    設定ファイルなし・キーなし・不正 JSON・非 bool 値は全て False（安全側 default）。
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or ""
        if appdata:
            config_path = os.path.join(appdata, "tidd_tools", "config.json")
        else:
            home = os.environ.get("HOME") or os.path.expanduser("~")
            config_path = os.path.join(home, ".config", "tidd_tools", "config.json")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or ""
        if xdg:
            config_path = os.path.join(xdg, "tidd_tools", "config.json")
        else:
            home = os.environ.get("HOME") or os.path.expanduser("~")
            config_path = os.path.join(home, ".config", "tidd_tools", "config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(config, dict):
        return False
    return config.get("ai-review-timing") is True


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")

    # tool_name が Agent tool（Claude Code）または spawn_agent（Codex）でなければ対象外
    tool_name = str(payload.get("tool_name", ""))
    if tool_name not in {"Agent", "spawn_agent"}:
        return 0

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    # issue-implementer のみ対象（kebab-case / snake_case を同一視・#3203）
    subagent_type = str(
        tool_input.get("subagent_type") or tool_input.get("task_name") or ""
    ).replace("-", "_")
    if subagent_type != "issue_implementer":
        return 0

    # escape hatch
    if os.environ.get("SKIP_QUALITY_CHECK_GATE") == "1":
        return 0

    # Issue 番号を抽出（取れなければ安全側に倒す。require-subagent-prompt-contract が別途検証する）
    prompt = str(tool_input.get("prompt") or tool_input.get("message") or "")
    match = _ISSUE_NUM_RE.match(prompt)
    if not match:
        return 0

    issue_num = int(match.group(1))

    # 証跡チェック（Issue #3158: ai-review-timing 有効時は timing マークのみを証跡とする）
    if _is_ai_review_timing_enabled():
        # timing マークのみ（コメント代替は無効・#3158）
        if _has_timing_mark(issue_num):
            return 0
    else:
        # 後方互換（ai-review-timing 無効時）: timing マーク OR コメント
        if _has_timing_mark(issue_num):
            return 0
        if _has_quality_check_comment(issue_num):
            return 0

    sys.stderr.write(
        f"BLOCK: Issue #{issue_num} の品質チェック証跡が確認できません。\n"
        f"実装前に '/issue-review {issue_num}' を実行して品質チェックを完了してください。\n"
        "チェック済みの証跡（timing mark が必要です。ai-review-timing 無効時のみ GitHub コメント可）\n"
    )
    sys.stderr.write(DETAIL)
    return 2


def main() -> int:
    if not is_hook_enabled("require-quality-check"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
