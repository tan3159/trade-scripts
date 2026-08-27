"""PR body / Issue やること チェックボックス分類ロジックの単一ソース（Issue #2942）.

``ai_review/core.py`` の ``_classify_unchecked()`` と ``ai_review/subcommands.py`` の
``_classify()`` は同一の分類ロジック（prefix 判定・legacy キーワード判定・コードブロック
スキップ）を独立に実装していた。``test_plan.py`` の ``classify_items()`` も入力形状
（``- [ ] `` prefix を剥がした項目テキストを受け取る）は異なるが、同じ prefix 優先順位
（post_merge > ai_confirm > manual）で分類していた。

#2026（``[手動]`` 廃止）のような仕様変更のたびに 3 箇所の同期修正が必要になり分類ズレの
原因になっていたため、本モジュールに判定ロジックを一本化する。
"""

from __future__ import annotations

import dataclasses
import re

# 手動扱いキーワード（旧 test-plan.sh と同一）
LEGACY_HUMAN_KEYWORDS_RE = re.compile(r"目視|ブラウザ|browser|visual|環境依存", re.IGNORECASE)

# Issue #1402: `[AI確認-post-merge]` は `[AI確認]` の startswith と衝突するため、
# より具体的なプレフィックスを先に判定する。
_POST_MERGE_TEXT_RE = re.compile(r"^\[AI確認-post-merge\]")
_AI_CONFIRM_TEXT_RE = re.compile(r"^\[AI確認\]")
# Issue #2026: `[手動]` prefix は廃止。後方互換のため読んだ場合は "manual" 扱いにする。
_MANUAL_TEXT_RE = re.compile(r"^\[手動\]")

# 行ベース走査用（``classify_checkboxes()`` が使用）
UNCHECKED_RE = re.compile(r"^[ \t]*- \[ \]")
CODEBLOCK_RE = re.compile(r"^[ \t]*```")

CheckboxCategory = str  # "post_merge" | "ai_confirm" | "manual" | "auto"


def classify_checkbox_text(item_text: str) -> CheckboxCategory:
    """``- [ ] `` prefix を剥がしたチェックボックス項目テキストを分類する.

    判定優先順位: post_merge > ai_confirm > manual（legacy キーワード含む）> auto。

    Returns:
        ``"post_merge"`` / ``"ai_confirm"`` / ``"manual"`` / ``"auto"`` のいずれか。
    """
    if _POST_MERGE_TEXT_RE.match(item_text):
        return "post_merge"
    if _AI_CONFIRM_TEXT_RE.match(item_text):
        return "ai_confirm"
    if _MANUAL_TEXT_RE.match(item_text) or LEGACY_HUMAN_KEYWORDS_RE.search(item_text):
        return "manual"
    return "auto"


@dataclasses.dataclass
class ClassifiedLines:
    """``classify_checkboxes()`` の戻り値."""

    auto: list[str] = dataclasses.field(default_factory=list)
    ai_confirm: list[str] = dataclasses.field(default_factory=list)
    manual: list[str] = dataclasses.field(default_factory=list)
    post_merge: list[str] = dataclasses.field(default_factory=list)


def classify_checkboxes(body: str) -> ClassifiedLines:
    """PR/Issue 本文全体を走査し、未チェック（``- [ ]``）行を分類する.

    旧 ``ai_review/core.py._classify_unchecked()`` /
    ``ai_review/subcommands.py._classify()`` の共通実装。コードブロック内の行は
    対象外。返り値の各リストには元の行（``- [ ] `` prefix 込み）を格納する。
    """
    result = ClassifiedLines()
    in_code = False
    for line in body.splitlines():
        if CODEBLOCK_RE.match(line):
            in_code = not in_code
            continue
        match = UNCHECKED_RE.match(line)
        if in_code or not match:
            continue
        item_text = line[match.end() :].lstrip()
        category = classify_checkbox_text(item_text)
        if category == "post_merge":
            result.post_merge.append(line)
        elif category == "ai_confirm":
            result.ai_confirm.append(line)
        elif category == "manual":
            result.manual.append(line)
        else:
            result.auto.append(line)
    return result
