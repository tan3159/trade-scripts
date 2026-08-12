"""`issue-<N>` 抽出の共通ユーティリティ（hook 側・Issue #3550）.

`record-timing-boundaries.py`（``_WORKTREE_ISSUE_RE``）・`require-ruff-format.py`
（``_ISSUE_NUM_RE``）・`stamp-merge-summary-session.py`（``_ISSUE_KEY_RE``）の 3 hook が
``issue-(\\d+)`` 相当の正規表現をそれぞれ独立実装していたため、境界規則（単語境界の有無）が
微妙に異なる状態になっていた。本モジュールへ一本化する。

**tidd_tools 側との関係:** ``.claude/hooks/`` は stdlib のみで動作し tidd_tools の venv を
import できないため（hook は ``uvx`` でサブプロセス起動する経路しか持たない）、tidd_tools 側の
正 ``projects/py/tidd_tools/src/tidd_tools/shared/branch_ref.py`` とは意図的に別モジュールに
分離している（プロセス境界による重複であり、統合しない）。

stdlib のみ使用。
"""

from __future__ import annotations

import re

# `issue-<N>` の単純検索（境界条件なし・`stamp-merge-summary-session.py` 由来）。
ISSUE_KEY_RE = re.compile(r"issue-(\d+)")

# `issue-<N>` の直前が `/`・空白・文字列先頭のいずれかであることを要求する検索
# （`record-timing-boundaries.py` の旧 `_WORKTREE_ISSUE_RE` 由来）。
_ISSUE_NUM_BOUNDARY_RE = re.compile(r"(?:^|[/\s])issue-(\d+)")

# `Issue番号: <N>` 行（issue-implementer の prompt 契約・SKILL.md STEP 2・#3385）。
# 複数行 prompt の途中にあっても一致するよう re.MULTILINE + .search() で使う。
_PROMPT_ISSUE_LINE_RE = re.compile(r"^Issue番号:\s*(\d+)\s*$", re.MULTILINE)

# `PR番号: <N>` 行（ai-confirm-verifier / issue-fixer の prompt 契約・#3558）。
# Issue 番号は含まれず、PR 本文の closes #N から解決する（hook 側で gh_pr_view_json）。
_PROMPT_PR_LINE_RE = re.compile(r"^PR番号:\s*(\d+)\s*$", re.MULTILINE)

# `Issue #<N>`（issue-reviewer の description 契約・SKILL.md STEP 1.5-c・#3557）。
_DESCRIPTION_ISSUE_PREFIX_RE = re.compile(r"Issue #(\d+)")

# 先頭の `#<N>`（duplicate-detector の description 契約・duplicate-suspect-triage.md・#3557）。
_FIRST_HASH_NUMBER_RE = re.compile(r"#(\d+)")


def extract_issue_number_from_branch(branch: str) -> int | None:
    """ブランチ名（または worktree 作成コマンド等の文字列全般）から Issue 番号を抽出する.

    `issue-` の直前が ``/``・空白・文字列先頭のいずれかの場合のみ一致させる
    （``myissue-123`` のような単語境界のない部分一致は拾わない）。
    見つからない・空文字列の場合は None を返す。
    """
    if not branch:
        return None
    match = _ISSUE_NUM_BOUNDARY_RE.search(branch)
    if not match:
        return None
    return int(match.group(1))


def extract_issue_number_from_prompt(prompt: str) -> int | None:
    """`Issue番号: <N>` 行から Issue 番号を抽出する（issue-implementer 契約・#3385）.

    複数行 prompt の途中にあっても一致する（re.MULTILINE + .search()・#3385）。
    見つからない・空文字列の場合は None を返す。
    """
    if not prompt:
        return None
    match = _PROMPT_ISSUE_LINE_RE.search(prompt)
    if not match:
        return None
    return int(match.group(1))


def extract_pr_number_from_prompt(prompt: str) -> int | None:
    """`PR番号: <N>` 行から PR 番号を抽出する（ai-confirm-verifier / issue-fixer 契約・#3558）.

    複数行 prompt の途中にあっても一致する（re.MULTILINE + .search()）。
    Issue 番号を含まないため、PR 番号は ``gh pr view`` + ``closes #N`` 解決の起点として
    使われる（``_lib.gh_command.gh_pr_view_json``）。見つからない・空文字列の場合は None。
    """
    if not prompt:
        return None
    match = _PROMPT_PR_LINE_RE.search(prompt)
    if not match:
        return None
    return int(match.group(1))


def extract_issue_number_from_issue_description(description: str) -> int | None:
    """`Issue #<N>` から Issue 番号を抽出する（issue-reviewer 契約・#3557）.

    SKILL.md STEP 1.5-c の呼び出し形式 `description="Issue #<N> の品質チェック"` に対応。
    見つからない・空文字列の場合は None を返す。
    """
    if not description:
        return None
    match = _DESCRIPTION_ISSUE_PREFIX_RE.search(description)
    if not match:
        return None
    return int(match.group(1))


def extract_first_hash_number(text: str) -> int | None:
    """先頭の `#<N>` から Issue 番号を抽出する（duplicate-detector 契約・#3557）.

    `duplicate-suspect-triage.md` の呼び出し形式
    `description="Phase 2 厳密判定 #<N> vs #<M>"` のうち、最初の `#<N>`（判定対象の
    Issue 番号）を返す。見つからない・空文字列の場合は None を返す。
    """
    if not text:
        return None
    match = _FIRST_HASH_NUMBER_RE.search(text)
    if not match:
        return None
    return int(match.group(1))
