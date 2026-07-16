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

stdlib のみ使用。
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_HOOKS_DIR))

from _lib.bypass_audit import record_bypass as _record_bypass  # noqa: E402
from _lib.hook_io import get_command, get_tool_name, is_hook_enabled, read_hook_input  # noqa: E402
from _lib.override_markers import (  # noqa: E402
    extract_reason,
    find_invalid_syntax,
    has_override_marker,
)
from _lib.session_detector import is_claude_code_session  # noqa: E402

_HOOK_NAME = "detect-ai-confirm-misuse"

# [AI確認] 項目にマッチする regex（チェックボックス形式）
_AI_CONFIRM_ITEM_RE = re.compile(r"^\s*-\s*\[[ x]\]\s*\[AI確認\]\s*(.+)$", re.MULTILINE)

# gh pr create / edit コマンドを捕捉する
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


def _extract_body(command: str) -> str:
    """gh pr create / edit コマンドから --body / --body-file の値を抽出する."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    body = ""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--body", "-b") and i + 1 < len(tokens):
            body = tokens[i + 1]
            i += 2
        elif tok.startswith("--body="):
            body = tok[len("--body=") :]
            i += 1
        elif tok in ("--body-file", "-F") and i + 1 < len(tokens):
            body_file = Path(tokens[i + 1])
            try:
                body = body_file.read_text(encoding="utf-8")
            except OSError:
                pass
            i += 2
        elif tok.startswith("--body-file="):
            body_file = Path(tok[len("--body-file=") :])
            try:
                body = body_file.read_text(encoding="utf-8")
            except OSError:
                pass
            i += 1
        else:
            i += 1
    return body


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
                    f"'[手動]' プレフィックスに変更を検討してください。\n"
                    f"詳細: docs/reference/hooks.md#detect-ai-confirm-misusepy"
                )
    return warnings


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

    keywords = _load_keywords()
    if not keywords:
        return 0

    body = _extract_body(command)
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
