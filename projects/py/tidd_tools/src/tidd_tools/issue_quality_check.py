"""`tidd issue-quality-check` サブコマンド（Issue #1245・#1301 で API 全廃）.

**Issue #1301 で Anthropic API 直接呼び出しを全廃した。** 意味的品質チェック（Pain 深さ・
Gherkin の検証可能性）は Claude Code の `/issue-review` skill 経由で subagent に外出しした。
本サブコマンドは互換性のため残っているが、常に fallback PASS を返す no-op である。

- 入力: ``--body TEXT`` または ``--body-file PATH``、``--type <feat|fix|...>``
- 出力: stdout に ``{"verdict": "PASS", "fallback": true, "reason": "deferred-to-skill"}`` JSON
- 終了コード: 常に 0
- 意味的品質チェックが必要な場合: Claude Code セッション内で ``/issue-review <N>`` を実行する

hook からサブプロセスで呼び出される互換性を維持する。stdlib のみ使用。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any


@dataclasses.dataclass
class QualityResult:
    """Issue 品質チェック結果（互換性のため保持・常に fallback PASS）."""

    verdict: str = "PASS"
    pain_score: int = 3
    pain_reason: str = ""
    gherkin_issues: list[str] = dataclasses.field(default_factory=list)
    fallback: bool = True
    reason: str = "deferred-to-skill"

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "verdict": self.verdict,
            "pain_score": self.pain_score,
            "pain_reason": self.pain_reason,
            "gherkin_issues": list(self.gherkin_issues),
            "fallback": self.fallback,
        }
        if self.reason:
            payload["reason"] = self.reason
        return json.dumps(payload, ensure_ascii=False)


def check_issue_quality(
    body: str,
    issue_type: str = "feat",
) -> QualityResult:
    """STRICT テキストチェックによる Gherkin 禁止語検出（Issue #1284）.

    意味的な深い判定は ``/issue-review`` skill に外出しされているが、
    ``.claude/rules/gherkin-forbidden-words.yaml`` の禁止語辞書によるテキストレベルの
    ゲートは本関数が担当する（旧: 常に PASS フォールバック → 新: 主観語検出で FAIL）。

    Args:
        body: Issue 本文全体
        issue_type: Issue の type（feat/fix でのみ Gherkin 品質チェックを有効化）

    Returns:
        主観語ヒットなし → PASS + fallback=True + reason="deferred-to-skill"
        主観語ヒットあり → FAIL + fallback=False + gherkin_issues=[detected words]
    """
    # feat/fix 以外は STRICT チェックの対象外（Gherkin セクション不要な type）
    if issue_type not in {"feat", "fix"}:
        return QualityResult()

    # Gherkin セクションがない場合は静的 hook 側で validate-issue.py が別途チェック済み
    if "## 振る舞い" not in body:
        return QualityResult()

    # 遅延 import: hook 経路の subprocess 起動時間短縮のため
    import re as _re

    from tidd_tools.gherkin_forbidden import check_forbidden_words

    # `## 振る舞い` セクションの Then/And 継続行のみを対象にする（背景・設計欄の語句を除外）
    behavior_match = _re.search(r"##\s*振る舞い\s*(.*?)(?=^##|\Z)", body, _re.MULTILINE | _re.DOTALL)
    if not behavior_match:
        return QualityResult()
    section = behavior_match.group(1)

    then_lines: list[str] = []
    in_then_block = False
    for line in section.splitlines():
        stripped = line.strip()
        if _re.match(r"^Then\s+", stripped):
            in_then_block = True
            then_lines.append(stripped)
        elif _re.match(r"^(Given|When)\s+", stripped):
            in_then_block = False
        elif in_then_block and _re.match(r"^(And|But)\s+", stripped):
            then_lines.append(stripped)

    if not then_lines:
        return QualityResult()

    hits = check_forbidden_words("\n".join(then_lines))
    if not hits:
        return QualityResult()
    return QualityResult(
        verdict="FAIL",
        gherkin_issues=hits,
        fallback=False,
        reason="strict-forbidden-words",
    )


def run_cli(args: argparse.Namespace) -> int:
    """CLI エントリポイント。常に PASS + fallback=True を返す（exit 0）."""
    body = args.body
    if body is None and args.body_file:
        try:
            body = Path(args.body_file).read_text(encoding="utf-8")
        except OSError as exc:
            sys.stderr.write(f"ERROR: body-file の読み込みに失敗しました: {exc}\n")
            return 2
    if body is None:
        body = sys.stdin.read()
    result = check_issue_quality(body or "", issue_type=args.type or "feat")
    if result.verdict == "FAIL":
        sys.stderr.write("tidd issue-quality-check: STRICT テキストチェックで Gherkin 禁止語を検出しました。\n")
    else:
        sys.stderr.write(
            "tidd issue-quality-check: 意味判定は /issue-review skill に外出しされました。"
            "静的チェックは validate-issue.py hook で実行されます。\n"
        )
    print(result.to_json())
    return 1 if result.verdict == "FAIL" else 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "issue-quality-check",
        help="[互換] Issue 品質チェック（#1301 で意味判定は /issue-review skill に外出し）",
        description=(
            "**互換性のためのスタブ実装。** Issue #1301 で Anthropic API 直接呼び出しは全廃され、"
            "意味的品質チェックは Claude Code の /issue-review skill 経由 subagent に移行しました。"
            "本コマンドは常に fallback PASS を返します。"
        ),
    )
    parser.add_argument("--body", help="Issue 本文（--body-file 未指定時は stdin から読む）")
    parser.add_argument("--body-file", help="Issue 本文のファイルパス")
    parser.add_argument(
        "--type",
        default="feat",
        help="Issue の type（feat/fix/docs/... 既定: feat）",
    )
    parser.set_defaults(func=run_cli)
