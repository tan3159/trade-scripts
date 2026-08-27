"""Claude fallback JSON → agy/codex 互換 markdown 変換（Issue #1697）.

`tidd ai-review` の exit code 3 経路（agy・codex 全滅）で `.claude/agents/ai-fallback-reviewer.md`
subagent が返す JSON

    {"verdict": "APPROVE" | "REQUEST_CHANGES",
     "issues": [{"severity": "CRITICAL"|"HIGH"|"MEDIUM"|"LOW"|"DEFERRED",
                 "file": "...", "line": 42, "message": "..."}],
     "rationale": "..."}

を、agy/codex が出力するのと同じ

    VERDICT: APPROVE|REQUEST_CHANGES

    ## サマリー
    <rationale>

    ## 指摘事項
    - [CRITICAL] file:line: 説明
    - [HIGH] file:line: 説明
    ...

の markdown に **決定的に** 変換する純関数を提供する。

呼び出し元セッションが LLM で手動書き換えしていた（→ 実行ごとに見出し文言・指摘の順序・
severity 表記がゆらいでいた）のを、コードで一意に固定するのが目的（#1697）。
"""

from __future__ import annotations

from typing import Any

__all__ = ["format_fallback_verdict"]


_VALID_VERDICTS: frozenset[str] = frozenset({"APPROVE", "REQUEST_CHANGES"})
_VALID_SEVERITIES: frozenset[str] = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW", "DEFERRED"})


def _format_issue_line(issue: dict[str, Any]) -> str:
    """1 件の issue dict を ``- [SEVERITY] file:line: 説明`` 形式の 1 行に変換する.

    file / line が特定できない場合は ``prompts.py`` の指示に従い
    ``- [SEVERITY] 説明`` のみにフォールバックする。
    未知の severity は ``LOW`` に fallback する（LLM 側の typo 対策）。
    """
    severity = str(issue.get("severity", "")).upper()
    if severity not in _VALID_SEVERITIES:
        severity = "LOW"

    message = str(issue.get("message", "")).strip()

    file_path = issue.get("file")
    line = issue.get("line")
    if file_path and line is not None:
        return f"- [{severity}] {file_path}:{line}: {message}"
    if file_path:
        return f"- [{severity}] {file_path}: {message}"
    return f"- [{severity}] {message}"


def format_fallback_verdict(payload: dict[str, Any]) -> str:
    """`ai-fallback-reviewer` subagent の JSON を agy/codex 互換 markdown に変換する.

    :param payload: subagent 返り値の dict。``verdict`` (str)・``issues`` (list[dict])・
        ``rationale`` (str) を含む。
    :returns: ``VERDICT: ...\\n\\n## サマリー\\n<rationale>\\n[\\n## 指摘事項\\n<lines>]``
        形式の markdown 文字列。issues が空リストなら ``## 指摘事項`` セクションは省略する。
    :raises ValueError: ``verdict`` キーが欠落 or ``APPROVE`` / ``REQUEST_CHANGES``
        以外の値だった場合。例外メッセージには受け取った値を含める。
    """
    if "verdict" not in payload:
        raise ValueError("payload に 'verdict' キーがありません")

    verdict = str(payload["verdict"])
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"verdict は APPROVE / REQUEST_CHANGES のいずれかである必要があります: {verdict}")

    rationale = str(payload.get("rationale", "")).strip()
    issues = payload.get("issues") or []

    parts: list[str] = []
    parts.append(f"VERDICT: {verdict}")
    parts.append("")
    parts.append("## サマリー")
    parts.append(rationale if rationale else "(サマリーなし)")

    if issues:
        parts.append("")
        parts.append("## 指摘事項")
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            parts.append(_format_issue_line(issue))

    # 末尾に改行を1つ入れる（agy/codex 出力と同様）
    return "\n".join(parts) + "\n"
