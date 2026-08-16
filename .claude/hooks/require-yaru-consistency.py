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
from _lib.override_markers import find_invalid_syntax, has_override_marker

# Issue #2998 レビュー指摘: `_lib.yaru_sections` は `_lib.gh_cache` を経由し import 時に
# `git remote get-url origin` を実行するため、matcher: "Bash" であらゆる Bash コマンドに
# 反応する本 hook では、`gh issue close` と特定できた後にのみ遅延 import する
# （auto-tick-issue-items.py と同様のパターン）。

# gh issue close コマンドから issue 番号を抽出
_GH_ISSUE_CLOSE_RE = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*gh\s+issue\s+close\b([^\n&|;]*)",
    re.MULTILINE,
)
# 引数中の Issue 番号 (最初の純粋な整数トークン)
_ISSUE_NUM_RE = re.compile(r"(?<!\S)(\d+)(?!\S)")

# override marker: <!-- yaru-tracking: #NNN --> (Issue #2954: _lib.override_markers に統一)
_YARU_TRACKING_MARKER = "yaru-tracking"


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


def _has_valid_tracking_marker(body: str) -> bool:
    return has_override_marker(body, _YARU_TRACKING_MARKER)


def _has_invalid_tracking_marker(body: str) -> bool:
    return bool(find_invalid_syntax(body, [_YARU_TRACKING_MARKER]))


def _write_invalid_marker_message(issue_number: int) -> None:
    sys.stderr.write(
        f"Blocked: Issue #{issue_number} の yaru-tracking marker の書式が不正です。\n"
        "正しい書式: <!-- yaru-tracking: #<follow-up-issue-num> -->\n"
        "詳細: docs/reference/hooks.md#require-yaru-consistencypy\n"
    )


def _all_remaining_are_manual_or_ai_confirm(unchecked_items: list[str]) -> bool:
    from _lib.yaru_sections import has_manual_or_ai_confirm_prefix

    return all(has_manual_or_ai_confirm_prefix(it) for it in unchecked_items)


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

    from _lib.yaru_sections import extract_section as _extract_yaru_section
    from _lib.yaru_sections import fetch_issue_body as _fetch_issue_body
    from _lib.yaru_sections import parse_unchecked_items as _extract_unchecked_items

    def _evaluate(target_body: str) -> tuple[str, list[str]]:
        """body を評価し、("ok", []) / ("invalid_marker", []) / ("blocked", 未チェック項目) を返す.

        Issue #3961 codex レビュー指摘: 無効 marker チェックを他の判定より先に
        `return` してしまうと、#3119 のブロック確定前 cache bypass 再取得
        （stale body 基準で block 判定が確定した後にのみ実行）に到達できず、
        marker の書式修正が fresh_body に反映されていても stale body の無効
        marker のみを理由に即 block してしまう。判定ロジックを 1 関数へ集約し、
        `_main` 側で stale/fresh 両方に同じ評価を適用できるようにする。
        """
        if _has_invalid_tracking_marker(target_body):
            return "invalid_marker", []

        yaru_section = _extract_yaru_section(target_body)
        if yaru_section is None:
            # やること セクション自体がなければ対象外 (docs Issue 等)
            return "ok", []

        items = _extract_unchecked_items(yaru_section)
        if not items:
            # 全 checked
            return "ok", []

        # 残 [手動] / [AI確認] のみなら OK
        if _all_remaining_are_manual_or_ai_confirm(items):
            return "ok", []

        # tracking marker で bypass
        if _has_valid_tracking_marker(target_body):
            return "ok", []

        return "blocked", items

    body = _fetch_issue_body(issue_number)
    if body is None:
        # 取得失敗は silent skip (network エラー等で close 自体を止めない)
        return 0

    verdict, unchecked_items = _evaluate(body)
    if verdict == "ok":
        return 0

    # Issue #3119: gh_cache（TTL 300 秒・SWR）が編集前 body を保持し続け、直後に
    # やること checkbox を更新して close し直しても stale hit で誤ブロックし続ける
    # 問題への対応。ブロックを確定させる前に 1 回だけ cache を bypass して最新
    # body を再取得し、再判定する。
    # Issue #3959/#3961: stale body が無効/欠落 yaru-tracking marker を理由に
    # block 判定 (`verdict != "ok"`) となった場合も、fresh_body で marker が
    # 修正済みなら再評価してブロックしない。
    from _lib.yaru_sections import (
        fetch_issue_body_bypass_cache as _fetch_issue_body_fresh,
    )

    fresh_body = _fetch_issue_body_fresh(issue_number)
    if fresh_body is not None:
        fresh_verdict, fresh_unchecked_items = _evaluate(fresh_body)
        if fresh_verdict == "ok":
            return 0
        if fresh_verdict == "invalid_marker":
            _write_invalid_marker_message(issue_number)
            return 2
        # 再取得しても未チェックが残る場合は、最新の状態を使って従来どおりブロックする
        unchecked_items = fresh_unchecked_items

    if verdict == "invalid_marker" and fresh_body is None:
        # 再取得自体が失敗した場合は stale body の無効 marker 判定を維持する
        _write_invalid_marker_message(issue_number)
        return 2

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
