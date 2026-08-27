"""Issue #1756: PR merge gate として Issue やること 全消化を検査するモジュール.

**設計:**

- PR body から ``closes|fixes|resolves #N`` を辿って Issue 本文を取得する
- Issue 本文の ``## やること`` セクションを parse する
- ``- [ ]`` の項目のうち ``[手動]`` / ``[AI確認]`` / ``[AI確認-post-merge]``
  prefix なしのものを検出する
- 未 tick 項目があれば ``GateResult.passed = False`` を返し、
  呼び出し元 (``core.py::_handle_approve`` / ``subcommands.py::_continue_approve``)
  で exit 4 に変換して merge を block する

停止条件ファイルによる gate は廃止（ファイル種別は AI の計画完遂度と相関しないため）。

**関連:**

- Issue #1534: yaru_auto_tick (evidence-based auto-tick)
- Hook #1533: require-yaru-consistency.py (gh issue close 時の checkbox gate)
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from tidd_tools.shared.issue_body import extract_closes_issues, extract_section, iter_unchecked_items

# ── パース regex（Issue #2943: closes/セクション/未チェック項目抽出は shared/issue_body.py へ集約） ──

_EXCLUDE_PREFIX_RE = re.compile(r"^\s*\[(手動|AI確認(-post-merge)?)\]")
# Issue #2376: 見送り明記項目の除外パターン（「本 Issue では対応しない」「見送り」等）
_WAIVE_PATTERN_RE = re.compile(r"本\s*Issue\s*では?(対応しない|見送り)|見送り")


@dataclass
class GateResult:
    """merge gate 判定結果."""

    passed: bool
    """True = auto-merge OK / False = 人間マージ待ち."""

    unchecked_items: list[dict[str, object]] = field(default_factory=list)
    """未 tick 項目 (``{"issue": int, "item": str}`` のリスト)."""

    reason: str = ""
    """人間向け説明文."""

    fetch_error: bool = False
    """Issue 本文取得失敗 (gh コマンドエラー / None 返却)."""


def find_gating_unchecked_items(issue_body: str) -> list[str]:
    """Issue body の ``## やること`` から merge gate を trigger する未 tick 項目を抽出する.

    ``[手動]`` / ``[AI確認]`` / ``[AI確認-post-merge]`` prefix 付き項目は
    「Issue 側で意図的に人間確認・後続検証に委ねる」項目のため gate 対象外。

    ``## やること`` セクションが存在しない場合は空リストを返す（docs Issue 等）。
    """
    if not issue_body:
        return []
    section = extract_section(issue_body, "やること")
    if section is None:
        return []

    items: list[str] = []
    for unchecked in iter_unchecked_items(section):
        text = unchecked.text
        if _EXCLUDE_PREFIX_RE.match(text):
            continue
        # Issue #2376: 見送り明記項目は merge gate 対象外
        if _WAIVE_PATTERN_RE.search(text):
            continue
        items.append(text)
    return items


def check_pr(
    pr_body: str,
    *,
    fetch_issue_body: Callable[[int], str | None],
) -> GateResult:
    """PR body から closes #N を辿って Issue やること 消化状態を検査する.

    :param pr_body: PR 本文（GitHub API ``pulls/N`` の ``body`` フィールド）
    :param fetch_issue_body: ``issue_number: int -> str | None`` の関数。
        本 module を testable にするため IO を注入する（None 返却は取得失敗を意味する）。
    :returns: :class:`GateResult`
    """
    closes = extract_closes_issues(pr_body)
    if not closes:
        return GateResult(
            passed=True,
            reason="closes #N が PR body に記載されていないため Issue やること チェックを skip します",
        )

    unchecked_items: list[dict[str, object]] = []
    for issue_num in closes:
        body = fetch_issue_body(issue_num)
        if body is None:
            return GateResult(
                passed=False,
                fetch_error=True,
                reason=f"Issue #{issue_num} の本文取得に失敗しました。安全のため人間マージに委ねます。",
            )
        items = find_gating_unchecked_items(body)
        for item in items:
            unchecked_items.append({"issue": issue_num, "item": item})

    if unchecked_items:
        # 対象 Issue 番号を集約して人間向けメッセージに含める（feature 準拠）
        issue_nums = sorted({int(uc["issue"]) for uc in unchecked_items if isinstance(uc["issue"], (int, str))})
        issues_txt = ", ".join(f"#{n}" for n in issue_nums)
        return GateResult(
            passed=False,
            unchecked_items=unchecked_items,
            reason=(
                f"Issue {issues_txt} の やること に未完了項目があります。"
                "AI が計画を完遂できなかったため人間の確認が必要です。"
            ),
        )
    return GateResult(passed=True, reason="Issue やること は全て消化済みです")
