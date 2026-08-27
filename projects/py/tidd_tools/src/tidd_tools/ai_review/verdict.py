"""VERDICT パース + 指摘事項抽出（旧 ai-review.sh の §4〜§5 を移植）.

公開 API:
- :func:`parse_verdict` — レビュー出力から ``APPROVE`` / ``REQUEST_CHANGES`` を取り出す
- :func:`extract_issues` — 指摘事項行を抽出（``[CRITICAL]``..``[DEFERRED]``）
- :func:`count_blocking_issues` — CRITICAL / HIGH の件数
- :func:`is_blocking_issues_zero` — blocking 指摘がゼロか（``## 指摘事項`` セクションがない場合は False）
- :func:`is_nit_only` — MEDIUM / LOW のみか
- :func:`has_needs_human_review` — ``[NEEDS_HUMAN_REVIEW]`` を含むか
- :func:`is_same_issues` — 前回指摘との 80% 双方向一致判定（全 severity 対応・Issue #2665）
- :func:`is_prev_blocking_resolved` — 前回の CRITICAL/HIGH 指摘がすべて解消されたか
- :func:`capture_canary_fixture` — Canary 回帰テスト用 fixture を蓄積する（Issue #1289）
- :func:`validate_approve_authenticity` — APPROVE が実レビュー由来であることを検証する（Issue #2530）

Anthropic SDK による構造化抽出（Issue #1243 の ``parse_verdict_with_tool_calling``）は
Issue #1303 で廃止。verdict 構造化抽出は `/ai-review` skill + `verdict-extractor` subagent
経由（Agent tool）に移行した。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Issue #2530: APPROVE が実レビュー由来であることを保証するための下限値定数
#
# 選定根拠:
# - llm_duration: 実レビューは 30〜120 秒（実測 30 秒）、stub / モックは 0〜1 秒。
#   誤検知が最も起きにくい保守的な値として 5 秒を採用（実レビューの下限より十分低い）。
# - 本文バイト数: 実レビューは 1000 バイト超（実測 1398 バイト）、stub は 35 バイト。
#   誤検知を避けるために 200 バイトを採用（実レビューの最小実測値より十分低い）。
#
# escape hatch: AI_REVIEW_SKIP_VERDICT_AUTHENTICITY=1 でスキップ（CI・テスト用）。
APPROVE_MIN_LLM_DURATION_SEC: int = 5
APPROVE_MIN_REVIEW_BYTES: int = 200

VERDICT_RE = re.compile(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)")
ISSUE_LINE_RE = re.compile(r"\[(CRITICAL|HIGH|MEDIUM|LOW|DEFERRED)\][^\n]*")
ISSUE_SECTION_HEADER_RE = re.compile(r"^## 指[摘引]事項", re.MULTILINE)
BLOCKING_RE = re.compile(r"\[(CRITICAL|HIGH)\]")
ADVISORY_RE = re.compile(r"\[(MEDIUM|LOW)\]")
DEFERRED_RE = re.compile(r"\[DEFERRED\]")
NEEDS_HUMAN_REVIEW_RE = re.compile(r"\[NEEDS_HUMAN_REVIEW\]")


def _issues_section(review_output: str) -> str:
    """``## 指摘事項`` （または ``## 指引事項`` typo）以降のテキストを返す."""
    if not review_output:
        return ""
    lines = review_output.splitlines()
    start = -1
    for idx, line in enumerate(lines):
        if ISSUE_SECTION_HEADER_RE.match(line):
            start = idx
            break
    if start == -1:
        return ""
    return "\n".join(lines[start:])


def has_issues_section(review_output: str) -> bool:
    return _issues_section(review_output) != ""


def parse_verdict(review_output: str) -> str:
    """``VERDICT: APPROVE`` または ``VERDICT: REQUEST_CHANGES`` 行を取り出す."""
    if not review_output:
        return ""
    m = VERDICT_RE.search(review_output)
    return m.group(1) if m else ""


def extract_issues(review_output: str) -> str:
    """指摘事項セクションから ``[SEVERITY] ...`` 行をまとめて返す（改行区切り）.

    旧 sh: ``_issues_section | grep -oP '\\[(CRITICAL|HIGH|MEDIUM|LOW|DEFERRED)\\][^\\n]*'``
    """
    section = _issues_section(review_output)
    if not section:
        return ""
    found = [m.group(0) for m in ISSUE_LINE_RE.finditer(section)]
    return "\n".join(found)


def count_blocking_issues(review_output: str) -> int:
    section = _issues_section(review_output)
    if not section:
        return 0
    return len(BLOCKING_RE.findall(section))


def is_blocking_issues_zero(review_output: str) -> bool:
    """CRITICAL / HIGH がゼロかつ指摘事項セクションが存在するときに True."""
    if not has_issues_section(review_output):
        return False
    return count_blocking_issues(review_output) == 0


def is_nit_only(review_output: str) -> bool:
    """MEDIUM / LOW のみで blocking / DEFERRED がない場合に True."""
    section = _issues_section(review_output)
    if not section:
        return False
    # 旧 sh: blocking_count = grep -cP '\[(CRITICAL|HIGH|DEFERRED)\]'
    if BLOCKING_RE.search(section) or DEFERRED_RE.search(section):
        return False
    return bool(ADVISORY_RE.search(section))


def has_needs_human_review(review_output: str) -> bool:
    return bool(NEEDS_HUMAN_REVIEW_RE.search(review_output or ""))


def _normalize(text: str) -> str:
    """ASCII 大文字を小文字化し空白を圧縮する（旧 sh ``_normalize``）.

    日本語マルチバイトは触らない。
    """
    # ASCII のみ lower 化
    out_chars = [c.lower() if "A" <= c <= "Z" else c for c in text]
    # 連続する半角空白を1つに圧縮
    normalized = re.sub(r" +", " ", "".join(out_chars))
    return normalized.strip(" ")


def _blocking_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"^\[(CRITICAL|HIGH)\]", line)]


def _all_severity_lines(text: str) -> list[str]:
    """CRITICAL/HIGH/MEDIUM/LOW/DEFERRED を含む全 severity 行を返す.

    severity マーカーのない非空行も含める（Issue #2665: null 相当の境界値対応）。
    """
    return [line for line in text.splitlines() if line.strip()]


def is_same_issues(prev_issues_file: Path | None, curr_issues: str) -> bool:
    """前回保存指摘と今回の指摘が双方向 80% 以上一致するか（Issue #2665: 全 severity 対応）.

    比較対象の選択ルール:
    - CRITICAL/HIGH が両方に存在する場合: CRITICAL/HIGH 行のみで比較（既存動作を維持）
    - CRITICAL/HIGH が一方でも欠ける場合: 全非空行で比較（MEDIUM/LOW のみ・severity マーカーなし行を検出）

    この設計により CRITICAL/HIGH 同一判定の感度は変わらず、MEDIUM/LOW のみが
    2 回連続した場合のエスカレーション漏れを防ぐ（Issue #2665 で修正）。
    """
    if prev_issues_file is None or not prev_issues_file.is_file():
        return False
    try:
        prev_text = prev_issues_file.read_text(encoding="utf-8")
    except OSError:
        return False
    if not prev_text:
        return False

    prev_blocking = _blocking_lines(prev_text)
    curr_blocking = _blocking_lines(curr_issues or "")

    # CRITICAL/HIGH が両方にある場合は従来どおり blocking 行のみで比較
    if prev_blocking and curr_blocking:
        comparison_prev = prev_blocking
        comparison_curr = curr_blocking
    else:
        # CRITICAL/HIGH がない場合（MEDIUM/LOW のみ・マーカーなし）は全非空行で比較
        comparison_prev = _all_severity_lines(prev_text)
        comparison_curr = _all_severity_lines(curr_issues or "")

    if not comparison_prev or not comparison_curr:
        return False

    prev_norm = [_normalize(line) for line in comparison_prev]
    curr_norm = [_normalize(line) for line in comparison_curr]
    prev_set = set(prev_norm)
    curr_set = set(curr_norm)

    fwd_match = sum(1 for line in prev_norm if line in curr_set)
    rev_match = sum(1 for line in curr_norm if line in prev_set)

    prev_count = len(comparison_prev)
    curr_count = len(comparison_curr)

    # 旧 sh: ((fwd*10 >= prev*8)) && ((rev*10 >= curr*8))
    return fwd_match * 10 >= prev_count * 8 and rev_match * 10 >= curr_count * 8


def is_prev_blocking_resolved(prev_issues_file: Path | None, curr_issues: str) -> bool:
    """前回の CRITICAL/HIGH 指摘がすべて解消され、今回新規 CRITICAL/HIGH がない場合に True."""
    if prev_issues_file is None or not prev_issues_file.is_file():
        return False
    try:
        prev_text = prev_issues_file.read_text(encoding="utf-8")
    except OSError:
        return False

    if not prev_text:
        return True

    prev_blocking = _blocking_lines(prev_text)
    if not prev_blocking:
        return True

    curr_blocking = _blocking_lines(curr_issues or "")
    return not curr_blocking


def validate_approve_authenticity(
    review_output: str,
    llm_duration: int,
    *,
    min_duration: int = APPROVE_MIN_LLM_DURATION_SEC,
    min_bytes: int = APPROVE_MIN_REVIEW_BYTES,
) -> bool:
    """APPROVE が実レビュー由来であることを検証する（Issue #2530）.

    ``AI_REVIEW_SKIP_VERDICT_AUTHENTICITY=1`` を設定すると検証をスキップする（CI・テスト用）。

    検証基準:
    - ``llm_duration`` が下限未満（stub / モックは 0〜1 秒）
    - ``review_output`` のバイト数が下限未満（stub / モックは 35 バイト程度）

    どちらの条件でも失敗した場合は stderr に判定根拠を出力し、False を返す。
    REQUEST_CHANGES には適用しない（呼び出し元で APPROVE の場合のみ呼ぶこと）。

    Returns:
        True  — 実レビュー由来と判断（APPROVE を採用）
        False — fake と判断（APPROVE を棄却して exit 3 にする）
    """
    if os.environ.get("AI_REVIEW_SKIP_VERDICT_AUTHENTICITY") == "1":
        return True

    actual_bytes = len(review_output.encode("utf-8")) if review_output else 0

    if llm_duration < min_duration:
        print(
            f"ERROR: APPROVE authenticity check failed: "
            f"llm_duration={llm_duration}s < min={min_duration}s "
            f"(fake APPROVE の疑い). "
            f"escape hatch: AI_REVIEW_SKIP_VERDICT_AUTHENTICITY=1",
            file=sys.stderr,
        )
        return False

    if actual_bytes < min_bytes:
        print(
            f"ERROR: APPROVE authenticity check failed: "
            f"review_bytes={actual_bytes} < min={min_bytes} "
            f"(fake APPROVE の疑い). "
            f"escape hatch: AI_REVIEW_SKIP_VERDICT_AUTHENTICITY=1",
            file=sys.stderr,
        )
        return False

    return True


def capture_canary_fixture(
    pr_num: str,
    backend: str,
    llm_output: str,
    verdict: str,
) -> None:
    """``AI_REVIEW_CANARY_CAPTURE=1`` のとき LLM 出力を fixture として保存する（Issue #1289）."""
    if os.environ.get("AI_REVIEW_CANARY_CAPTURE") != "1":
        return
    fixture_dir = Path(__file__).parents[3] / "tests" / "regressions" / "verdict"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = fixture_dir / f"pr-{pr_num}.json"
    if fixture_path.exists():
        logger.debug("canary fixture already exists, skipping: %s", fixture_path)
        return
    data = {
        "pr_num": pr_num,
        "backend": backend,
        "llm_output": llm_output,
        "expected_verdict": verdict,
        "description": f"PR #{pr_num} の LLM 出力 fixture（AI_REVIEW_CANARY_CAPTURE=1 で自動生成）",
    }
    fixture_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("canary fixture saved: %s", fixture_path)
