"""`tidd issue-next-timing` サブコマンド（Issue #2312・#3559）.

#3559 で `mark` サブコマンドを廃止した。計測境界は hook / ツール自己記録
（record-timing-boundaries / pre-flight / ai-review / check-pr-conflicts /
cleanup-merged-branch）に一本化され、md への mark 指示書き戻しは
`ban-timing-mark-instruction` hook がブロックする。残るサブコマンドは
`mark-quality-check-done`（require-quality-check.py の証跡・#3158）のみ。

**設計選択（Issue #2312 やること4項目目・#3340）:** 記録先は統一日誌
（``tidd_tools.timing_log``・SQLite ``timing-events/timing.db``）のみ。旧
``ai-review-timing`` jsonl 基盤（``~/.cache/ai-review-timing/<key>.jsonl``）への
書き込み（``mark_boundary``）は #2936/#3340 で撤去した。`require-quality-check`
hook の証跡読みも統一日誌参照へ移行済み（#3340）。

**設計検討（Issue #2326 やること3・#3559 で結論に至る）:** SKILL.md のプロンプト
指示のみに依存する現状（エージェントが呼び出しを忘れると記録が欠落する）を踏まえ、
`gh pr create`・`tidd ai-review` 等の既存 tidd_tools サブコマンド実行時にコード側
から自動で mark を打てるか検討した。結論として計測境界をコード側自己記録へ
一本化し、LLM が手打ちできる CLI エントリポイント自体を撤去した。

**action log 連携（Issue #4037）:** `mark-quality-check-done` は上記の統一日誌への
記録に加え、`issue_next_state.append_action()` 経由で
`cache/issue-next-state/issue-<N>.json` の `actions` 配列にも 1 エントリを追記する
（`init`/`consume` と同じ action log 基盤への参加）。対象の state ファイルが存在しない
場合（init 未実行・孤立呼び出し等）は `append_action()` が no-op で返るため、
本コマンド自体の exit code・従来の記録処理には影響しない。
"""

from __future__ import annotations

import argparse

from tidd_tools import issue_next_state, timing_log
from tidd_tools.shared.cli import add_common_flags


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "issue-next-timing",
        help="/issue-next SKILL の STEP 境界でタイムスタンプを記録する（#2312）",
        description=__doc__,
    )
    add_common_flags(parser)
    sub = parser.add_subparsers(dest="action", required=True)

    # Issue #3559: `mark` サブコマンドは廃止した。計測境界は hook / ツール自己記録
    # （record-timing-boundaries / pre-flight / ai-review / check-pr-conflicts /
    # cleanup-merged-branch）に一本化し、md への mark 指示書き戻しは
    # `ban-timing-mark-instruction` hook がブロックする。残るのは
    # `mark-quality-check-done`（require-quality-check.py の証跡）のみ。
    p_qc = sub.add_parser(
        "mark-quality-check-done",
        help="STEP 1.5（Issue品質チェック）終了を issue-<N> キーへ記録する（#3158）",
    )
    p_qc.add_argument("issue_number", type=int, help="対象 Issue 番号")
    p_qc.add_argument(
        "--verdict",
        choices=("pass", "fail", "needs-human-input", "epic-split"),
        required=True,
        help="品質チェック判定（pass / fail / needs-human-input / epic-split）",
    )
    p_qc.add_argument(
        "--size-over-1000-possible",
        choices=("true", "false"),
        default="false",
        help=(
            "issue-reviewer が返した size_over_1000_possible の値（true/false・既定 false）。"
            " require-split-consideration.py hook が issue-implementer 起動前の"
            " 分割検討根拠ゲート判定に使う（Issue #3993）"
        ),
    )

    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    if args.action == "mark-quality-check-done":
        # Issue #3158: step1.5-quality-check を issue-<N> キーへ記録し、
        # verdict（pass/fail）を meta に含める（require-quality-check.py の証跡になる）。
        # Issue #3993: size_over_1000_possible も meta に含め、
        # require-split-consideration.py hook の分割検討根拠ゲート判定材料にする。
        key = f"issue-{args.issue_number}"
        timing_log.record_event_safe(
            key,
            "step1.5-quality-check",
            "point",
            "issue-next-timing",
            meta={
                "verdict": args.verdict,
                "size_over_1000_possible": getattr(args, "size_over_1000_possible", "false") == "true",
            },
        )
        # Issue #4037: cache/issue-next-state/issue-<N>.json の action log にも記録する。
        # state ファイルが存在しない（init 未実行・テスト用の孤立呼び出し等）場合は
        # append_action() が例外を出さず no-op（exit 1 相当）で戻る。
        issue_next_state.append_action(args.issue_number, "orchestrator", "mark-quality-check-done")
    return 0
