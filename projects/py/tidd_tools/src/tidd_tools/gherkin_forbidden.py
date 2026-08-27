"""Gherkin Then 句禁止語辞書の loader（Issue #1284）.

`.claude/rules/gherkin-forbidden-words.yaml` から禁止語 regex を読み込む共通ヘルパー。
stdlib のみで実装（PyYAML 依存なし）。

**利用者:**

- ``issue_quality_check.py``: STRICT テキストチェックの fallback
- ``ai_review/prompts.py``: LLM プロンプトに動的に禁止語を注入

**YAML フォーマット:**

.. code-block:: yaml

    forbidden_words:
      - id: correct_behavior
        regex: '(正しく動く|正しく動作する)'
        description: '...'
        example_bad: '...'
        example_good: '...'

stdlib の re のみで簡易パースする。フォーマットが崩れた場合は空リストを返す（フェイルオープン）。
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path


@dataclasses.dataclass
class ForbiddenWord:
    """禁止語エントリ."""

    id: str
    regex: re.Pattern[str]
    description: str
    example_bad: str = ""
    example_good: str = ""


def _find_yaml(start: Path | None = None) -> Path | None:
    """カレントディレクトリから遡って yaml を探す."""
    here = (start or Path.cwd()).resolve()
    for parent in (here, *here.parents):
        candidate = parent / ".claude" / "rules" / "gherkin-forbidden-words.yaml"
        if candidate.is_file():
            return candidate
    return None


def load_forbidden_words(yaml_path: Path | None = None) -> list[ForbiddenWord]:
    """禁止語 YAML を読み込んで ForbiddenWord のリストを返す.

    Args:
        yaml_path: 明示的な YAML パス。None の場合カレントから遡って探す

    Returns:
        禁止語のリスト。YAML が見つからない・パース失敗時は空リスト（フェイルオープン）。
    """
    path = yaml_path or _find_yaml()
    if path is None or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    # ざっくり YAML パース: forbidden_words: 配下の各エントリ
    # 各エントリは: - id: <id>\n regex: '<regex>'\n description: '<desc>'\n ...
    entries: list[ForbiddenWord] = []
    # 各エントリのフィールドを 1 つのブロックとして抽出
    entry_re = re.compile(
        r"^\s*-\s+id:\s*(\w+)\s*\n"
        r"\s+regex:\s*'([^']+)'\s*\n"
        r"\s+description:\s*'([^']*)'"
        r"(?:\s*\n\s+example_bad:\s*'([^']*)')?"
        r"(?:\s*\n\s+example_good:\s*'([^']*)')?",
        re.MULTILINE,
    )
    for match in entry_re.finditer(text):
        try:
            pattern = re.compile(match.group(2), re.MULTILINE)
        except re.error:
            continue
        entries.append(
            ForbiddenWord(
                id=match.group(1),
                regex=pattern,
                description=match.group(3),
                example_bad=match.group(4) or "",
                example_good=match.group(5) or "",
            )
        )
    return entries


def check_forbidden_words(text: str, words: list[ForbiddenWord] | None = None) -> list[str]:
    """text 内で禁止語にヒットした ID + description のリストを返す.

    Args:
        text: チェック対象文字列（PR ボディや Issue 本文）
        words: 事前ロード済みリスト。None なら load_forbidden_words() を使う

    Returns:
        ヒットしたエントリのメッセージリスト（ID: description 形式）。ヒットなしなら空リスト。
    """
    if words is None:
        words = load_forbidden_words()
    hits: list[str] = []
    for word in words:
        match = word.regex.search(text)
        if match:
            matched_snippet = match.group(0)
            hits.append(f"禁止語 '{matched_snippet}' が Then 句に含まれます ({word.id}: {word.description})")
    return hits
