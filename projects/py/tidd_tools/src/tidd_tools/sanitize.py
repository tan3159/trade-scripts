"""非信頼テキストのサニタイズ（Issue #1845）.

Issue/PR 本文を subagent プロンプトへ埋め込む前に、プロンプトインジェクションの
隠れ蓑になりうる不可視要素を除去する。stdlib のみ使用。
参考: anthropics/claude-code-action の security.md
（docs/research/claude-code-action-research.md §4-2）。
"""

from __future__ import annotations

import re

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# 未閉鎖コメントは行頭開始（CommonMark HTML block 型 2）のみ末尾まで不可視のため全除去する。
# 行中の未閉鎖 <!-- はインライン扱いで GitHub 上可視テキストとなり隠れ蓑にならないため保持する
# （Gherkin Then 句等のリテラル "<!--" で後続テキストが消えるのを防ぐ。PR #1952 codex 指摘）
_UNCLOSED_HTML_COMMENT_RE = re.compile(r"^[ \t]{0,3}<!--.*\Z", re.DOTALL | re.MULTILINE)
_ALT_ATTR_RE = re.compile(r"""\balt\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_HTML_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]{1,31});")
# ゼロ幅文字・BOM・双方向制御文字・不可視整形文字
_INVISIBLE_CHARS_RE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def sanitize_untrusted_text(text: str) -> str:
    """Issue/PR 本文から不可視の injection ベクタを除去して返す.

    除去対象: HTML コメント（行頭開始の未閉鎖含む）・``alt=`` 属性・
    HTML エンティティ・ゼロ幅文字等の不可視 Unicode。通常のテキストは変更しない。
    """
    result = _HTML_COMMENT_RE.sub("", text)
    result = _UNCLOSED_HTML_COMMENT_RE.sub("", result)
    result = _ALT_ATTR_RE.sub("", result)
    result = _HTML_ENTITY_RE.sub("", result)
    return _INVISIBLE_CHARS_RE.sub("", result)
