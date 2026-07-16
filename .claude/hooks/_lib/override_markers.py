"""共通 override marker regex ヘルパ (Issue #1460).

hook 間で override marker（`<!-- allow-*: <理由> -->`）の検知方式が
plain string search と regex compile で不整合になっていた（監査 §4-8）。

本モジュールは全 override marker を統一 regex パターンで検知するための helper を提供する。

パターン: ``<!--\\s*<marker-name>\\s*:\\s*(.+?)\\s*-->``（改行含む場合は `re.DOTALL`）

理由（`.+?`）が空 / コロンなしなど「無効書式」の判定は `is_valid_marker_syntax()` を使う。

stdlib のみ使用。
"""

from __future__ import annotations

import re
from typing import Iterable

# Issue #1460: 全 override marker の統一 regex パターン。
# - `<!--` と `-->` の間の空白・改行を吸収
# - marker 名の前後の空白を許容
# - `:` の前後の空白を許容
# - 理由部分は非貪欲マッチ (`.+?`)、DOTALL で改行を含めて捕捉
# - marker 名は f-string の呼び出し側が渡す（re.escape で埋め込み）


def compile_marker_regex(marker_name: str) -> re.Pattern[str]:
    """指定 marker 名の統一 regex パターンを compile する.

    Args:
        marker_name: 例 ``"allow-test-update"``・``"allow-single-commit"``

    Returns:
        `re.Pattern` — `pattern.search(body)` で marker + reason を検出する。
        match の group(1) は理由文字列。

    実装メモ (AI review PR #1489 attempt 2 指摘反映):
        理由部分は ``(?:(?!-->).)+?`` で ``-->`` を跨がないように制限する。
        naive な ``(.+?)`` + ``re.DOTALL`` だと理由が空のマーカーの後に別の
        HTML コメントがあると跨ってマッチして意図せず bypass する。
    """
    escaped = re.escape(marker_name)
    return re.compile(
        rf"<!--\s*{escaped}\s*:\s*((?:(?!-->).)+?)\s*-->",
        re.DOTALL,
    )


def has_override_marker(body: str, marker_name: str) -> bool:
    """PR ボディに指定 marker が「有効書式で」含まれるか判定する.

    無効書式（コロンなし・空理由）は False を返す。
    Gherkin Scenario 4 対応: `<!-- allow-test-update -->` (colon なし) → False
    """
    if not body:
        return False
    pattern = compile_marker_regex(marker_name)
    m = pattern.search(body)
    if m is None:
        return False
    reason = m.group(1).strip()
    return bool(reason)


def extract_reason(body: str, marker_name: str) -> str | None:
    """有効な marker から理由文字列を抽出する.

    Returns:
        理由文字列（strip 済み）または None。
    """
    if not body:
        return None
    pattern = compile_marker_regex(marker_name)
    m = pattern.search(body)
    if m is None:
        return None
    reason = m.group(1).strip()
    return reason or None


# AI review PR #1489 attempt 2 (LOW) 反映: パターンをキャッシュしてループ内 compile を排除
_INVALID_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _compile_invalid_pattern(marker_name: str) -> re.Pattern[str]:
    """無効書式（コロンなし OR コロンありで理由が空）を検出する pattern を compile する.

    AI review PR #1489 attempt 3 指摘（MEDIUM）反映:
    - ``<!-- allow-x -->`` (コロンなし)
    - ``<!-- allow-x: -->`` (コロンありだが理由が空)
    - ``<!-- allow-x :   -->`` (コロン + 空白のみ) も同様
    """
    cached = _INVALID_PATTERN_CACHE.get(marker_name)
    if cached is not None:
        return cached
    escaped = re.escape(marker_name)
    # コロンなし OR コロン + 空白のみ の 2 パターンを alternation で捕捉
    pat = re.compile(
        rf"<!--\s*{escaped}\s*-->" r"|" rf"<!--\s*{escaped}\s*:\s*-->",
        re.DOTALL,
    )
    _INVALID_PATTERN_CACHE[marker_name] = pat
    return pat


def find_invalid_syntax(body: str, marker_names: Iterable[str]) -> list[str]:
    """marker 名が「コロンなし」の不完全書式で書かれている場合を検出する.

    ``<!-- allow-test-update -->`` のようにコロン・理由がないケースを見つけて、
    marker 名のリストを返す（`stderr` に「override marker の書式が不正です」を
    出すための入力）。

    valid な marker がすでに存在する場合は無効判定に含めない。
    """
    if not body:
        return []
    invalid: list[str] = []
    for name in marker_names:
        if _compile_invalid_pattern(name).search(body) and not has_override_marker(body, name):
            invalid.append(name)
    return invalid
