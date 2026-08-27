"""ブランチ名・Issue キー解析の単一ソース（tidd_tools 側・Issue #3550）.

``issue-<N>`` 抽出とブランチ type 一覧が以下 6 箇所に独立実装され、
type 一覧が ``pre_flight.py``（``feat``/``fix`` のみ）と ``.claude/hooks/validate-issue.py``
（7 種）で別々の形で写経されていた:

- ``pre_flight.py:114-115`` ``_BRANCH_TYPE_RE`` / ``_BRANCH_ISSUE_RE``
- ``worktree_add.py:23`` ``_ISSUE_NUM_RE``
- ``merge_summary.py:56`` ``_ISSUE_KEY_RE``（``^issue-(\\d+)$`` 全体一致）

**hook 側との関係:** ``.claude/hooks/`` は stdlib のみで動作し tidd_tools の venv を
import できないため、hook 側は独立した ``_lib/issue_ref.py`` を持つ（プロセス境界による
意図的な重複・詳細は同モジュールの docstring 参照）。

**pre_flight.py への適用時の注意（Issue #3550 やること 2 番目）:** ``pre_flight.py`` は
``feat``/``fix`` ブランチのみを対象とする 3 箇所（``_resolve_issue_key_from_branch()`` 等）を
持つが、``parse_branch()`` は 7 種すべてにマッチするため、``feat``/``fix`` への絞り込みは
呼び出し側（``pre_flight.py``）が ``parsed[0] not in ("feat", "fix")`` で行う。
本モジュール側で type を絞り込むと挙動が変わってしまうため意図的にしていない
（type 一覧の拡張は sub-issue「pre-flight のブランチ type 制限」で扱う）。
"""

from __future__ import annotations

import re

# `docs/conventions.md` の type 一覧（7 種）。`.claude/hooks/validate-issue.py`
# `VALID_TYPES_RE` と同一集合。
VALID_BRANCH_TYPES: frozenset[str] = frozenset({"feat", "fix", "docs", "refactor", "build", "ci", "research"})

_TYPE_ALTERNATION = "|".join(sorted(VALID_BRANCH_TYPES))
# `<type>/` プレフィックス（全 type にマッチ）。
BRANCH_TYPE_RE = re.compile(rf"^({_TYPE_ALTERNATION})/")

# `issue-<N>` の汎用検索（ブランチ名・worktree パス等、文字列中のどこにあってもよい）。
_ISSUE_NUM_RE = re.compile(r"issue-(\d+)")
# `key` 文字列全体が厳密に `issue-<N>` 形式であることを要求する（`merge_summary.py` の
# Issue 番号バリデーション用途・部分一致を許容しない）。
_ISSUE_KEY_FULL_RE = re.compile(r"^issue-(\d+)$")


def extract_issue_number(text: str) -> int | None:
    """``text`` 中のどこかにある ``issue-<N>`` から Issue 番号を抽出する（汎用検索）.

    最初に出現した番号を返す。一致しない・空文字列の場合は None。
    """
    if not text:
        return None
    match = _ISSUE_NUM_RE.search(text)
    if not match:
        return None
    return int(match.group(1))


def match_issue_key(key: str) -> int | None:
    """``key`` 文字列全体が厳密に ``issue-<N>`` 形式の場合のみ Issue 番号を返す.

    ``extract_issue_number()`` と異なり、前後に余分な文字がある場合（例:
    ``"prefix-issue-123"``・``"issue-123-suffix"``）は None を返す。
    """
    if not key:
        return None
    match = _ISSUE_KEY_FULL_RE.match(key)
    return int(match.group(1)) if match else None


def parse_branch(branch: str) -> tuple[str, int] | None:
    """``<type>/issue-<N>-slug`` 形式のブランチ名から ``(type, issue_num)`` を返す.

    ``type`` は ``VALID_BRANCH_TYPES`` のいずれか（``BRANCH_TYPE_RE`` が判定）。
    型プレフィックスに一致しない、または ``issue-<N>`` が見つからない場合は None。
    """
    if not branch:
        return None
    type_match = BRANCH_TYPE_RE.match(branch)
    if not type_match:
        return None
    issue_num = extract_issue_number(branch)
    if issue_num is None:
        return None
    return type_match.group(1), issue_num


def resolve_issue_key(text: str) -> str | None:
    """``text``（ブランチ名・worktree パス等）から ``issue-<N>`` 形式のキーを返す.

    ``parse_branch()`` と異なり ``<type>/`` プレフィックスは要求しない汎用版
    （``worktree_add.py`` のようにブランチ名・パスどちらからも解決したい用途向け）。
    """
    issue_num = extract_issue_number(text)
    if issue_num is None:
        return None
    return f"issue-{issue_num}"
