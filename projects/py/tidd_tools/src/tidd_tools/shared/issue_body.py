"""PR/Issue 本文パース処理の単一ソース（Issue #2943）.

``closes #N`` 抽出 regex と ``## やること`` セクション切り出し（+ 未チェック項目抽出）が
``ai_review/yaru_merge_gate.py``・``ai_review/yaru_auto_tick.py``・
``transfer_issue_test_items.py``・``test_plan.py``・``ai_review/scope_diff_checker.py`` の
5 箇所に独立実装されており、抽出仕様のズレ（``\\b`` 有無の差異など）が Issue 消化 gate・
yaru auto-tick・転記の全系統に波及するリスクを抱えていた。本モジュールへ一本化する。

**旧実装との差異:** ``test_plan.py`` の ``_CLOSES_ISSUE_RE`` は ``\\b`` なしだったが、
他 4 箇所の厳密側（``\\b`` あり）に統一した。
"""

from __future__ import annotations

import dataclasses
import re

# close(s)|fixes|fix|resolves #N（同義語 + 大文字小文字無視）。
# #3550: ai_review/prompts.py の旧 ISSUE_REFERENCE_RE（`(?:closes?|fix(?:es?)?)[:\s]+#(\d+)`）が
# 拾えていた fix 単数形・close 単数形・コロン区切り（`closes:#N`）を統合後も落とさないよう
# `closes?` と `[:\s]+` を追加している。
CLOSES_RE = re.compile(r"\b(?:closes?|fixes|fix|resolves)[:\s]+#(\d+)", re.IGNORECASE)
_NEXT_H2_RE = re.compile(r"^##\s+", re.MULTILINE)
_UNCHECKED_ITEM_RE = re.compile(r"^\s*-\s*\[\s\]\s+(.+?)\s*$")
_CODEBLOCK_FENCE_RE = re.compile(r"^[ \t]*```")


def extract_closes_issues(body: str) -> list[int]:
    """PR/Issue 本文から ``closes|fixes|resolves #N`` を抽出する.

    空文字列・該当なしでは例外を発生させず空リストを返す。
    """
    if not body:
        return []
    return [int(m.group(1)) for m in CLOSES_RE.finditer(body)]


def extract_section(body: str, header: str) -> str | None:
    """``## <header>`` セクションの本文を返す（次の ``##`` 見出し直前まで）.

    ヘッダ行自体は含まない。``body`` にヘッダが存在しない場合は None を返す。
    """
    if not body:
        return None
    header_re = re.compile(rf"^##\s*{re.escape(header)}\s*$", re.MULTILINE)
    header_match = header_re.search(body)
    if not header_match:
        return None
    start = header_match.end()
    next_h2 = _NEXT_H2_RE.search(body, pos=start)
    end = next_h2.start() if next_h2 else len(body)
    return body[start:end]


@dataclasses.dataclass(frozen=True)
class UncheckedItem:
    """``iter_unchecked_items()`` が返す未チェック項目1件."""

    text: str
    """``- [ ] `` prefix を除いた項目テキスト."""

    raw_line: str
    """元の行そのもの（インデント込み・改行なし）."""


def iter_unchecked_items(section_text: str) -> list[UncheckedItem]:
    """セクション本文（``extract_section()`` の戻り値等）から未チェック項目を抽出する.

    ``- [ ] <text>`` 形式の行のみを対象とする。コードブロック（```` ``` ````）内の行は
    対象外（境界ケース）。
    """
    if not section_text:
        return []
    items: list[UncheckedItem] = []
    in_code = False
    for line in section_text.splitlines():
        if _CODEBLOCK_FENCE_RE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _UNCHECKED_ITEM_RE.match(line)
        if m:
            items.append(UncheckedItem(text=m.group(1), raw_line=line))
    return items
