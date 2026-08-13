#!/usr/bin/env python3
"""PreToolUse hook: PR ボディの [AI確認] 項目に禁止キーワードが含まれていないか検査する.

Issue #1310。「Human-out-of-the-loop」化を防ぐため、AI で観測不能な確認事項が
[AI確認] で誤って書かれている場合に warning を出す（block ではなく warning のみ）。

動作:
- `gh pr create` / `gh pr edit` コマンドを捕捉する
- PR ボディの [AI確認] 項目を抽出する
- `.claude/rules/manual-check-keywords.yaml` のキーワード regex と照合する
- 禁止キーワードが含まれていれば stderr に warning を出す（exit 0 でブロックしない）
- PR ボディに `<!-- allow-ai-confirm-keyword: <理由> -->` がある場合は warning を抑制する
- Claude Code セッション外では skip 警告のみ（session_detector 使用）

Issue #2711: `gh pr create`（`gh pr edit` は対象外）時点で [AI確認] 項目が既に
`- [x]`（チェック済み）になっている場合を検知する。PR 作成時点では
`ai-confirm-verifier` による検証が発生し得ないため、常に不正な状態
（未検証の自己申告）として exit 2 でブロックする。
`gh pr edit` は検証結果を反映する正規経路（`.claude/skills/issue-next/ai-confirm-verification.md`）
のため block 対象から除外する。

Issue #3422: `.claude/rules/test-plan-checklist.md` で `[手動]` prefix は廃止済み
（新規記述禁止・`[AI確認-post-merge]` を使う・#2026）のため、PR ボディのチェックリスト
項目に `[手動]` prefix があれば `gh pr create` は exit 2 でブロックする。
`gh pr edit` は既存 PR 本文由来の `[手動]`（後方互換・自動転記由来）を誤ブロックする
恐れがあるため、ブロックせず stderr 警告のみ（exit 0）とする。

stdlib のみ使用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_HOOKS_DIR))

from _lib.bypass_audit import record_bypass as _record_bypass
from _lib.gh_command import extract_pr_body as _extract_body
from _lib.gh_command import is_gh_pr_create as _is_gh_pr_create
from _lib.hook_io import (
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)
from _lib.override_markers import (
    extract_reason,
    find_invalid_syntax,
    has_override_marker,
)
from _lib.session_detector import is_claude_code_session

_HOOK_NAME = "detect-ai-confirm-misuse"

# [AI確認] 項目にマッチする regex（チェックボックス形式）
_AI_CONFIRM_ITEM_RE = re.compile(r"^\s*-\s*\[[ x]\]\s*\[AI確認\]\s*(.+)$", re.MULTILINE)

# Issue #2711: チェック済み（- [x]）の [AI確認] 項目にのみマッチする regex
_CHECKED_AI_CONFIRM_ITEM_RE = re.compile(
    r"^\s*-\s*\[x\]\s*\[AI確認\]\s*(.+)$", re.MULTILINE
)

# Issue #3422: 廃止済み [手動] prefix（#2026）のチェックリスト項目にマッチする regex
# （チェック・未チェック両方。`[手動]` は prefix としてのみ検出する）
_MANUAL_PREFIX_ITEM_RE = re.compile(
    r"^\s*-\s*\[[ x]\]\s*\[手動\]\s*(.+)$", re.MULTILINE
)

# gh pr create / edit コマンドを捕捉する（`gh pr create` 単体検出は Issue #2952 で
# `_lib/gh_command.is_gh_pr_create` へ集約済みのため、ここでは create|edit の
# 二択判定のみをローカルに保持する）
_PR_CREATE_RE = re.compile(
    r"(?:^|&&|\|\||\||;|\n)[ \t]*gh[ \t]+pr[ \t]+(create|edit)\b"
)

_KEYWORDS_YAML = _HOOKS_DIR.parent / "rules" / "manual-check-keywords.yaml"


def _load_keywords() -> list[dict[str, str]]:
    """manual-check-keywords.yaml からキーワードリストを読み込む."""
    if not _KEYWORDS_YAML.is_file():
        return []
    try:
        text = _KEYWORDS_YAML.read_text(encoding="utf-8")
    except OSError:
        return []
    # stdlib のみで YAML をパース（キーワードリストのみ対応する簡易パーサー）
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- id:"):
            if current:
                entries.append(current)
            current = {"id": stripped[len("- id:") :].strip()}
        elif stripped.startswith("id:") and not current:
            current = {"id": stripped[len("id:") :].strip()}
        elif ":" in stripped and current:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip("'\"")
            if key in ("category", "regex", "reason", "example_bad", "example_good"):
                current[key] = val
    if current:
        entries.append(current)
    return [e for e in entries if "id" in e and "regex" in e]


def _check_body(body: str, keywords: list[dict[str, str]]) -> list[str]:
    """[AI確認] 項目に禁止キーワードが含まれているか検査し、warning メッセージのリストを返す."""
    if not body:
        return []
    # Issue #1460: 無効書式 marker を検出したら空リスト＋stderr で警告（block はしないが警告）
    invalid_markers = find_invalid_syntax(body, ["allow-ai-confirm-keyword"])
    if invalid_markers:
        sys.stderr.write(
            f"detect-ai-confirm-misuse: WARNING: override marker の書式が不正です: "
            f"{', '.join(invalid_markers)}\n"
            "正しい書式: <!-- allow-ai-confirm-keyword: <理由> -->\n"
        )
    # 共通 helper で override マーカー判定
    if has_override_marker(body, "allow-ai-confirm-keyword"):
        # Issue #1625: バイパス使用を audit log に記録
        reason = extract_reason(body, "allow-ai-confirm-keyword")
        _record_bypass(event="allow-ai-confirm-keyword", reason=reason)
        return []
    ai_confirm_items = _AI_CONFIRM_ITEM_RE.findall(body)
    if not ai_confirm_items:
        return []
    warnings: list[str] = []
    # Issue #1461: 全 [AI確認] 項目を全カテゴリで scan し、1 項目内の複数カテゴリ違反も全て報告する。
    # 監査 §4-5 で「1 項目 = 1 warning」の break が誤用検知漏れになるリスクが指摘された。
    # 重複警告を避けるため、1 項目内で同一 keyword id が複数回 hit した場合は 1 回のみ計上する。
    for item_text in ai_confirm_items:
        seen_ids_in_item: set[str] = set()
        for kw in keywords:
            regex = kw.get("regex", "")
            if not regex:
                continue
            try:
                pattern = re.compile(regex, re.IGNORECASE)
            except re.error:
                continue
            m = pattern.search(item_text)
            if m:
                kw_id = kw.get("id", "")
                if kw_id in seen_ids_in_item:
                    continue
                seen_ids_in_item.add(kw_id)
                matched_word = m.group(0)
                warnings.append(
                    f"manual-check-warning: '[AI確認]' 項目に禁止キーワード "
                    f"'{matched_word}' が含まれます。\n"
                    f"このキーワードは AI が自動確認できない項目"
                    f"（ブラウザ目視・実機確認・法的判断等）に使われる言葉です。\n"
                    f"'[AI確認-post-merge]' プレフィックスに変更を検討してください。\n"
                    f"詳細: docs/reference/hooks.md#detect-ai-confirm-misusepy"
                )
    return warnings


def _find_checked_ai_confirm_items(body: str) -> list[str]:
    """PR ボディからチェック済み（`- [x]`）の [AI確認] 項目を抽出する.

    Issue #2711: PR 作成時点では `ai-confirm-verifier` による検証が発生し得ないため、
    チェック済み状態は常に不正（未検証の自己申告）として扱う。
    """
    if not body:
        return []
    return _CHECKED_AI_CONFIRM_ITEM_RE.findall(body)


def _find_manual_prefix_items(body: str) -> list[str]:
    """PR ボディから廃止済み `[手動]` prefix 付きチェックリスト項目を抽出する.

    Issue #3422: `.claude/rules/test-plan-checklist.md` で `[手動]` prefix は廃止済み
    （新規記述禁止・`[AI確認-post-merge]` を使う・#2026）。`gh pr create` では
    exit 2 でブロックし、`gh pr edit` では警告のみ（後方互換・自動転記由来の誤ブロック防止）。
    """
    if not body:
        return []
    return _MANUAL_PREFIX_ITEM_RE.findall(body)


def _main() -> int:
    if not is_claude_code_session():
        print(
            f"{_HOOK_NAME} skipped (outside Claude Code session)",
            file=sys.stderr,
        )
        return 0

    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)
    if tool_name != "Bash":
        return 0

    command = get_command(payload)
    if not command:
        return 0

    if not _PR_CREATE_RE.search(command):
        return 0

    body = _extract_body(command)

    # Issue #2711: gh pr create 限定で [AI確認] チェック済み項目を常にブロックする
    if _is_gh_pr_create(command):
        checked_items = _find_checked_ai_confirm_items(body)
        if checked_items:
            items_text = "\n".join(f"  - {item}" for item in checked_items)
            sys.stderr.write(
                "detect-ai-confirm-misuse: BLOCK: [AI確認] 項目が未検証のままチェック済み"
                "（- [x]）状態で PR が作成されようとしています。\n"
                f"{items_text}\n"
                "[AI確認] 項目は PR 作成時点では検証が発生し得ないため、"
                "- [ ]（未チェック）で作成してください。\n"
                "検証は tidd ai-review が exit 4 を返した後、"
                ".claude/skills/issue-next/ai-confirm-verification.md の手順で行います。\n"
                "詳細: docs/reference/hooks.md#detect-ai-confirm-misusepy\n"
            )
            return 2

    # Issue #3422: 廃止済み [手動] prefix の新規記述を検出する
    manual_items = _find_manual_prefix_items(body)
    if manual_items:
        items_text = "\n".join(f"  - {item}" for item in manual_items)
        if _is_gh_pr_create(command):
            # 新規記述はブロック（後方互換不要）
            sys.stderr.write(
                "detect-ai-confirm-misuse: BLOCK: [手動] は廃止済みです。"
                "[AI確認-post-merge] に書き換えてください。\n"
                f"{items_text}\n"
                "詳細: docs/reference/hooks.md#detect-ai-confirm-misusepy\n"
            )
            return 2
        # gh pr edit は既存 PR 本文由来の [手動]（後方互換・自動転記由来）を
        # 誤ブロックしないよう警告のみ（exit 0）
        sys.stderr.write(
            "detect-ai-confirm-misuse: WARNING: [手動] は廃止済みです。"
            "[AI確認-post-merge] に書き換えてください。\n"
            f"{items_text}\n"
            "詳細: docs/reference/hooks.md#detect-ai-confirm-misusepy\n"
        )

    keywords = _load_keywords()
    if not keywords:
        return 0

    warnings = _check_body(body, keywords)

    for w in warnings:
        print(w, file=sys.stderr)

    # warning のみ・block しない
    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("detect-ai-confirm-misuse"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
