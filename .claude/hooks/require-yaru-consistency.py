#!/usr/bin/env python3
"""PreToolUse hook: `gh issue close` 時に Issue の やること checkbox 消化状態を検査する.

Issue #1533: Wave 5 で 22 Issue の やること checkbox 更新漏れが発生した retrospective 対応。
close 時の最終防波堤として機械強制する。

**ハルシネーション対策:** 本 hook は checkbox の物理状態のみを検査する静的ルール
(LLM 判定を含まない)。よってハルシネーションのリスクはゼロ。

**ブロック条件:**

以下 3 条件のいずれかを満たせば通過 (exit 0)、そうでなければ block (exit 2):

1. すべての `- [ ]` が `- [x]` に更新済み
2. 残っている `- [ ]` が全て `[手動]` または `[AI確認]` プレフィックス付き
3. Issue body に `<!-- yaru-tracking: #<num> -->` marker で follow-up 明示

**参照:** #1533 / Wave 5 #1447 の やること update 漏れ retrospective。
stdlib のみ使用。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import get_command, get_tool_name, is_hook_enabled, read_hook_input  # noqa: E402

# gh issue close コマンドから issue 番号を抽出
_GH_ISSUE_CLOSE_RE = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*gh\s+issue\s+close\b([^\n&|;]*)",
    re.MULTILINE,
)
# 引数中の Issue 番号 (最初の純粋な整数トークン)
_ISSUE_NUM_RE = re.compile(r"(?<!\S)(\d+)(?!\S)")

# やること セクション抽出
_YARU_SECTION_RE = re.compile(
    r"^##\s*やること\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_UNCHECKED_ITEM_RE = re.compile(r"^\s*-\s*\[\s\]\s*(.+)$", re.MULTILINE)

# override marker: <!-- yaru-tracking: #NNN -->
_YARU_TRACKING_RE = re.compile(
    r"<!--\s*yaru-tracking\s*:\s*((?:(?!-->).)+?)\s*-->",
    re.DOTALL,
)
# 無効書式 (コロンなし or 理由空)
_YARU_TRACKING_INVALID_RE = re.compile(
    r"<!--\s*yaru-tracking\s*-->" r"|" r"<!--\s*yaru-tracking\s*:\s*-->",
    re.DOTALL,
)

# [手動] / [AI確認] / [AI確認-post-merge] プレフィックス
# Issue #1543: Issue #1402 で新設した [AI確認-post-merge] タグも許容する
_MANUAL_OR_AI_CONFIRM_RE = re.compile(r"^\s*\[(手動|AI確認(-post-merge)?)\]")


def _extract_issue_number(command: str) -> int | None:
    """`gh issue close <N>` から Issue 番号を抽出する."""
    m = _GH_ISSUE_CLOSE_RE.search(command)
    if not m:
        return None
    args_part = m.group(1)
    for token in _ISSUE_NUM_RE.finditer(args_part):
        try:
            return int(token.group(1))
        except ValueError:
            continue
    return None


def _fetch_issue_body(issue_number: int) -> str | None:
    """gh issue view で Issue body を取得する.

    テスト用に ``HOOK_TEST_ISSUE_BODY`` 環境変数で override 可能。
    """
    override = os.environ.get("HOOK_TEST_ISSUE_BODY")
    if override is not None:
        return override
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--json", "body", "-q", ".body"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _extract_yaru_section(body: str) -> str | None:
    m = _YARU_SECTION_RE.search(body)
    if not m:
        return None
    return m.group(1)


def _extract_unchecked_items(yaru_section: str) -> list[str]:
    return [m.group(1).strip() for m in _UNCHECKED_ITEM_RE.finditer(yaru_section)]


def _has_valid_tracking_marker(body: str) -> bool:
    m = _YARU_TRACKING_RE.search(body)
    if m is None:
        return False
    reason = m.group(1).strip()
    return bool(reason)


def _has_invalid_tracking_marker(body: str) -> bool:
    m = _YARU_TRACKING_INVALID_RE.search(body)
    if m is None:
        return False
    # 有効書式が同時に存在する場合は valid が優先
    return not _has_valid_tracking_marker(body)


def _all_remaining_are_manual_or_ai_confirm(unchecked_items: list[str]) -> bool:
    return all(_MANUAL_OR_AI_CONFIRM_RE.match(it) for it in unchecked_items)


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)
    if tool_name != "Bash":
        return 0

    command = get_command(payload)
    if not command:
        return 0

    issue_number = _extract_issue_number(command)
    if issue_number is None:
        # gh issue close コマンドでなければ対象外
        return 0

    body = _fetch_issue_body(issue_number)
    if body is None:
        # 取得失敗は silent skip (network エラー等で close 自体を止めない)
        return 0

    # 無効な yaru-tracking marker を先にチェック
    if _has_invalid_tracking_marker(body):
        sys.stderr.write(
            f"Blocked: Issue #{issue_number} の yaru-tracking marker の書式が不正です。\n"
            "正しい書式: <!-- yaru-tracking: #<follow-up-issue-num> -->\n"
            "詳細: docs/reference/hooks.md#require-yaru-consistencypy\n"
        )
        return 2

    yaru_section = _extract_yaru_section(body)
    if yaru_section is None:
        # やること セクション自体がなければ対象外 (docs Issue 等)
        return 0

    unchecked_items = _extract_unchecked_items(yaru_section)
    if not unchecked_items:
        # 全 checked
        return 0

    # 残 [手動] / [AI確認] のみなら OK
    if _all_remaining_are_manual_or_ai_confirm(unchecked_items):
        return 0

    # tracking marker で bypass
    if _has_valid_tracking_marker(body):
        return 0

    # Block
    sys.stderr.write(
        f"Blocked: Issue #{issue_number} の やること に未チェック項目があります。\n\n"
        f"未チェック項目 ({len(unchecked_items)} 件):\n"
    )
    for item in unchecked_items[:10]:  # 最大 10 件表示
        sys.stderr.write(f"  - [ ] {item[:150]}\n")
    if len(unchecked_items) > 10:
        sys.stderr.write(f"  ... 他 {len(unchecked_items) - 10} 件\n")
    sys.stderr.write(
        "\n解決方法 (いずれか):\n"
        "  1. Issue body の やること checkbox を `- [x]` に更新してから再度 close する\n"
        "  2. 残す項目に `[手動]` / `[AI確認]` / `[AI確認-post-merge]` プレフィックスを追加する\n"
        "  3. Issue body に `<!-- yaru-tracking: #<follow-up-issue-num> -->` marker を追加する\n"
        "\n詳細: docs/reference/hooks.md#require-yaru-consistencypy\n"
    )
    return 2


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("require-yaru-consistency"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
