"""Gherkin `## 振る舞い` セクションの Then/And 継続行抽出 + 禁止語検査（Issue #2956）.

`.claude/hooks/validate-issue.py` の `_check_gherkin_forbidden_words`（Issue #1457）と
`_check_gherkin_positive_markers`（Issue #1305）に同一の Then/And 継続行抽出ループが
2 重実装され、`_load_gherkin_forbidden_words` は `tidd_tools/gherkin_forbidden.py` の
劣化コピー（example_bad/example_good 非対応）になっていた問題を解消するために新設した。
stdlib のみで実装（`.claude/hooks/` 全体の原則）。

**リスト同期契約:** `tidd_tools.gherkin_forbidden.load_forbidden_words()`（tidd_tools 側の
同等ローダー）と同一 YAML から同一の (id, regex パターン, description, example_bad,
example_good) を返すことを `test_gherkin_forbidden_words_sync.py` の契約テストで保証する。

**利用者:** `.claude/hooks/validate-issue.py` のみ（tidd_tools 側は独立実装のまま）。
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import NamedTuple

_FORBIDDEN_ENTRY_RE = re.compile(
    r"^\s*-\s+id:\s*(\w+)\s*\n"
    r"\s+regex:\s*'([^']+)'\s*\n"
    r"\s+description:\s*'([^']*)'"
    r"(?:\s*\n\s+example_bad:\s*'([^']*)')?"
    r"(?:\s*\n\s+example_good:\s*'([^']*)')?",
    re.MULTILINE,
)

_SCENARIO_SPLIT_RE = re.compile(r"^\s*Scenario:", re.MULTILINE)
_THEN_RE = re.compile(r"^Then\s+")
_GIVEN_WHEN_RE = re.compile(r"^(Given|When)\s+")
_AND_BUT_RE = re.compile(r"^(And|But)\s+")


@dataclasses.dataclass
class ForbiddenWord:
    """禁止語エントリ（`tidd_tools.gherkin_forbidden.ForbiddenWord` と同一フィールド構成）."""

    id: str
    regex: re.Pattern[str]
    description: str
    example_bad: str = ""
    example_good: str = ""


class ForbiddenWordHit(NamedTuple):
    """`find_forbidden_word` のヒット結果."""

    marker_id: str
    matched_word: str


def find_forbidden_words_yaml(start: Path | None = None) -> Path | None:
    """カレントディレクトリから遡って `.claude/rules/gherkin-forbidden-words.yaml` を探す.

    見つからない場合、本モジュール自身の場所（`.claude/hooks/_lib/`）から相対的に
    `.claude/rules/gherkin-forbidden-words.yaml` を探すフォールバックを行う。
    """
    here = (start or Path.cwd()).resolve()
    for parent in (here, *here.parents):
        candidate = parent / ".claude" / "rules" / "gherkin-forbidden-words.yaml"
        if candidate.is_file():
            return candidate
    hooks_dir = (
        Path(__file__).resolve().parent.parent
    )  # .claude/hooks/_lib -> .claude/hooks
    candidate = hooks_dir.parent / "rules" / "gherkin-forbidden-words.yaml"
    return candidate if candidate.is_file() else None


def load_forbidden_words(yaml_path: Path | None = None) -> list[ForbiddenWord]:
    """禁止語 YAML を読み込んで `ForbiddenWord` のリストを返す.

    stdlib のみで簡易 YAML パース。フォーマットが崩れた行・regex コンパイル不能な
    エントリはスキップする（フェイルオープン）。YAML が見つからない・読み込み失敗時は
    空リストを返す。
    """
    path = yaml_path or find_forbidden_words_yaml()
    if path is None or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    entries: list[ForbiddenWord] = []
    for match in _FORBIDDEN_ENTRY_RE.finditer(text):
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


def split_scenarios(section: str) -> list[str]:
    """`## 振る舞い` セクション本文を `Scenario:` ごとに分割する.

    先頭要素は最初の `Scenario:` より前のプレアンブル（Feature: 行等）。
    `Scenario:` が 1 つもなければ長さ 1 のリスト（プレアンブルのみ）を返す。
    """
    return _SCENARIO_SPLIT_RE.split(section or "")


def extract_then_lines(scenario_text: str) -> list[str]:
    """1 Scenario テキストから Then 句 + 直後の And/But 継続行のみを抽出する.

    Given/When ブロック配下の And/But は対象外（Then ブロック開始後のみ収集）。
    空文字列を渡しても例外を送出せず空リストを返す。
    """
    lines: list[str] = []
    in_then_block = False
    for raw_line in (scenario_text or "").split("\n"):
        stripped = raw_line.strip()
        if _THEN_RE.match(stripped):
            in_then_block = True
            lines.append(stripped)
        elif _GIVEN_WHEN_RE.match(stripped):
            in_then_block = False
        elif in_then_block and _AND_BUT_RE.match(stripped):
            lines.append(stripped)
    return lines


def find_forbidden_word(
    then_text: str, forbidden: list[ForbiddenWord] | list[tuple[str, re.Pattern[str]]]
) -> ForbiddenWordHit | None:
    """Then 句結合テキストに禁止語がヒットしたら最初のヒットを返す.

    `then_text` が空文字列、または `forbidden` が空リストの場合は None を返す
    （例外を送出しない）。`forbidden` の regex は事前コンパイル済みの前提のため、
    正規表現特殊文字を含むエントリが混在していてもクラッシュしない
    （不正な regex は `load_forbidden_words` 側で既に除外済み）。

    `forbidden` は `ForbiddenWord` のリスト、または `(id, compiled_regex)` タプルの
    リストのどちらでも受け付ける（呼び出し側の柔軟性のため）。
    """
    if not then_text:
        return None
    for entry in forbidden:
        marker_id, pattern = (
            (entry.id, entry.regex) if isinstance(entry, ForbiddenWord) else entry
        )
        m = pattern.search(then_text)
        if m:
            matched_word = m.group(1) if m.groups() else m.group(0)
            return ForbiddenWordHit(marker_id=marker_id, matched_word=matched_word)
    return None
