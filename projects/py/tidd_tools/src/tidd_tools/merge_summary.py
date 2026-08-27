"""マージ完了時の所要時間サマリ表を出力する `tidd merge-summary`（Issue #2313）.

workflow.md 準拠フロー・`/issue-next` フロー共通で、マージ完了時にフェーズ
（Issue品質チェック / ブランチ作成 / 実装 / 検証・テスト / プルリクエスト /
AIレビュー(プライマリ) / PRテスト計画検証 / Issueやること検証 / CI・マージ / 後処理）
ごとの所要時間・主担当・結果を標準フォーマットの表で出力する。
セカンダリレビューの実行記録が存在する場合のみ AIレビュー(セカンダリ) 行を追加する（Issue #2734）。

加えて「自動チェック結果」セクションも出力する（Issue #2453）。

**計測方式（Issue #2395 自動導出方式・Issue #2453 mark 拡張・#3322 統一日誌専用化）:**

- データソースは統一イベントログ（`timing_log.read_events` + `filter_latest_attempt`）のみ。
  旧 5 系統ファイル（`ai-review-timing/*.jsonl`・`pre-flight/preflight-record.json`・
  `pre-flight/issue-N.jsonl`・`ai-reviewer/pr-<N>/timing.json`）への読み側フォールバックは
  #3322 で撤去済み。過去 Issue の再レポートは対象 Issue の旧 JSONL を統一日誌へ
  手動 import（lazy migration 相当）してから実行する。
- Issue品質チェック: step1.5-quality-check mark（既存）
- ブランチ作成: step2-implementation ～ step2-branch-created
- 実装: step2-branch-created ～ 最初の step3-preflight-start（#3558・step3-edit-done 廃止）
- 検証・テスト: step3-preflight-start ～ step3-preflight-end（PR 作成前のラウンド）
- プルリクエスト: PR 作成前の最後の step3-preflight-end ～ step4-pr-created（#2748）
- AIレビュー(プライマリ): ai-review-verdict + step5-airview-start/end（#3127）
- PRテスト計画検証: test-plan-gate + step5-aiconfirm-start/end（Issue #2773）
- Issueやること検証: yaru-evidence-tick ～ issue-exhaustion-gate（Issue #2772）
- CI・マージ: step6-merge-start ～ step6-merged（フォールバック: auto-merge レコード・PR リードタイム）
- 後処理: step6-merged ～ step6-cleanup-done

リトライ時の「実装（修正）」・PR 作成後の「検証・テスト」行は step5-fix-start /
step3-preflight-start/end（統一日誌）から組み立てる（#2733・#3322）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tidd_tools import loop_error_log, timing_log
from tidd_tools.shared import gh_client
from tidd_tools.shared.branch_ref import match_issue_key as _match_issue_key
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.issue_body import extract_closes_issues
from tidd_tools.shared.paths import cache_dir

# bare な Issue 番号（純粋な数字列）を受理する正規表現（#2543）
_BARE_NUMBER_RE = re.compile(r"^(\d+)$")

# マーカーファイルのサブディレクトリ名（#2391）
_MARKER_SUBDIR = "merge-summary-emitted"

# ── データモデル ──────────────────────────────────────────────────────────


@dataclass
class PhaseEvent:
    """サマリ表 1 行分の元データ（Issue #2453: 5 列化対応）.

    ``duration_display`` を指定すると ``duration_seconds`` から算出される表示値を
    上書きできる（例: セカンダリレビュー未実行時の「なし」）。

    ``start_at`` / ``end_at`` は JST 表示用の datetime（UTC）。
    ``owner`` は主担当（Claude Code / pytest・mypy・ruff 等）。
    """

    name: str
    duration_seconds: float | None = None
    result: str = "-"
    duration_display: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    owner: str = "-"


# ── 自動チェック結果セクション ─────────────────────────────────────────────────


@dataclass
class AutoCheckResult:
    """自動チェック結果セクションのデータ."""

    review_attempts: int = 0
    preflight_success: bool | None = None
    preflight_run_count: int = 0
    """pre-flight の実行回数（Issue #2637: Issue 別ファイルから取得・0 は不明）."""
    ai_confirm_count: int = 0
    pytest_duplicate: bool = False
    review_wait_ratio: float | None = None
    changed_files: int = 0
    additions: int = 0
    deletions: int = 0
    size_label: str = "-"
    review_backend: str = "-"


# ── フォーマット ──────────────────────────────────────────────────────────


def _format_duration(seconds: float) -> str:
    """秒数を「H時間M分S秒」形式の日本語表記に変換する."""
    total = round(seconds)
    if total < 60:
        return f"{total}秒"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}時間{minutes:02d}分{secs:02d}秒"
    return f"{minutes}分{secs:02d}秒"


def _to_jst(dt: datetime) -> datetime:
    """datetime（UTC）を JST（tzinfo なし）に変換する共通ヘルパー.

    UTC → JST（+9時間）。Python 3.12 以降での DeprecationWarning を避けるため、
    fromtimestamp + timezone.utc を使用する（Issue #2520）。
    """
    import datetime as _dt

    ts = dt.timestamp() + 9 * 3600
    return _dt.datetime.fromtimestamp(ts, tz=_dt.UTC).replace(tzinfo=None)


def _format_jst(dt: datetime | None, *, show_date: bool = False) -> str:
    """datetime（UTC）を JST 時刻文字列（HH:MM:SS）に変換する.

    dt が None の場合は "-" を返す。

    ``show_date`` が True の場合は ``MM/DD HH:MM:SS`` 形式で日付を先頭に付記する。
    表全体が JST で複数日にまたがるかどうかで判定すべきであり（Issue #3389）、
    呼び出し元（``build_rows``）が表全体を見て決定した値をそのまま渡す。
    """
    if dt is None:
        return "-"
    local = _to_jst(dt)
    if show_date:
        return local.strftime("%m/%d %H:%M:%S")
    return local.strftime("%H:%M:%S")


# 異常表示マーカー（Issue #3386: 壊れた計測値を正常時と同じ見た目にしない）。
_ANOMALY_REVERSED = "⚠️異常(終了<開始)"
_ANOMALY_MEASUREMENT_GAP = "⚠️計測欠落"

# AIレビュー系フェーズ名の接頭辞（プライマリ・セカンダリ共通）。
_AI_REVIEW_PHASE_PREFIX = "AIレビュー"


def _is_reversed_timestamps(event: PhaseEvent) -> bool:
    """終了が開始より前という物理的にありえない値かどうかを判定する（Issue #3386）.

    ``duration_seconds`` が直接負値で渡されるケースに加え、`_merge_events_from_unified`
    等の一部ビルダーは負の差分を ``None`` に潰してから ``start_at``/``end_at`` を
    そのまま保持するため、両方のケースを判定する。
    """
    if event.duration_seconds is not None and event.duration_seconds < 0:
        return True
    return event.start_at is not None and event.end_at is not None and event.end_at < event.start_at


def _is_measurement_gap(event: PhaseEvent) -> bool:
    """AIレビュー系フェーズで所要時間が 0 秒になる計測欠落を判定する（Issue #3386）.

    `step5-airview-end` mark 欠落時、`ai-review-verdict` レコードの
    `started_at == ended_at == 書き込み時刻` にフォールバックし、実際には
    レビューに要した時間が計測できていないにもかかわらず所要時間が 0 秒になる
    （実レビューが 0 秒で完了することはありえない）。
    """
    return (
        event.name.startswith(_AI_REVIEW_PHASE_PREFIX)
        and event.start_at is not None
        and event.end_at is not None
        and event.start_at == event.end_at
    )


def _display_duration(event: PhaseEvent) -> str:
    if event.duration_display is not None:
        return event.duration_display
    if _is_reversed_timestamps(event):
        return _ANOMALY_REVERSED
    if _is_measurement_gap(event):
        return _ANOMALY_MEASUREMENT_GAP
    if event.duration_seconds is None:
        return "計測不可"
    return _format_duration(event.duration_seconds)


def build_rows(events: list[PhaseEvent], *, show_date: bool = False) -> list[tuple[str, str, str, str, str, str]]:
    """同名フェーズの再試行に発生順で「（N回目）」を付記した行リストを返す.

    Returns: list of (フェーズ, 開始(JST), 終了(JST), 所要時間, 主担当, 結果)

    ``show_date`` は ``_format_jst`` にそのまま渡す（Issue #3389）。
    """
    seen: dict[str, int] = {}
    rows: list[tuple[str, str, str, str, str, str]] = []
    for event in events:
        seen[event.name] = seen.get(event.name, 0) + 1
        occurrence = seen[event.name]
        label = event.name if occurrence == 1 else f"{event.name}（{occurrence}回目）"
        rows.append(
            (
                label,
                _format_jst(event.start_at, show_date=show_date),
                _format_jst(event.end_at, show_date=show_date),
                _display_duration(event),
                event.owner,
                event.result,
            )
        )
    return rows


def detect_anomalous_phases(events: list[PhaseEvent]) -> list[tuple[str, str]]:
    """異常な所要時間表示（終了<開始・AIレビュー計測欠落）になったフェーズを検出する（Issue #3386）.

    Returns: ``(フェーズ名, 異常マーカー文字列)`` のリスト（発生順・異常なしなら空リスト）。
    呼び出し元（``tidd merge-summary report``）はこれを stderr 警告として出力する。
    """
    anomalies: list[tuple[str, str]] = []
    for event in events:
        display = _display_duration(event)
        if display in (_ANOMALY_REVERSED, _ANOMALY_MEASUREMENT_GAP):
            anomalies.append((event.name, display))
    return anomalies


def total_duration_seconds(events: list[PhaseEvent]) -> float:
    """全フェーズの実経過時間（最小 start_at 〜 最大 end_at）を返す（Issue #2642）.

    ``start_at`` / ``end_at`` が少なくとも 1 件以上のフェーズから取得できる場合は
    全フェーズを横断した最小 ``start_at`` と最大 ``end_at`` の差を実経過時間として返す。
    実経過時間が負になる不正タイムスタンプ（時計逆転等）の場合は 0 を返す。

    どのフェーズからも ``start_at`` / ``end_at`` が取得できない場合は
    従来どおり計測可能なフェーズの ``duration_seconds`` の積算にフォールバックする。
    """
    start_candidates = [e.start_at for e in events if e.start_at is not None]
    end_candidates = [e.end_at for e in events if e.end_at is not None]

    if start_candidates and end_candidates:
        earliest = min(start_candidates)
        latest = max(end_candidates)
        elapsed = (latest - earliest).total_seconds()
        return max(elapsed, 0.0)

    # フォールバック: タイムスタンプが取得できない場合は積算
    return sum(e.duration_seconds for e in events if e.duration_seconds is not None)


_SUMMARY_HEADER = "## 所要時間サマリ"


def _table_spans_multiple_jst_dates(events: list[PhaseEvent]) -> bool:
    """表に含まれる全行の start_at/end_at が JST で 2 日以上にまたがるかを判定する（Issue #3389）.

    レンダリング時点の「今日」（実行日）を基準にすると、2 日をまたぐ表で片方の行にしか
    日付が付かない不整合が起きるため、表自体が持つ日付の集合で判定する。
    """
    dates = {_to_jst(dt).date() for event in events for dt in (event.start_at, event.end_at) if dt is not None}
    return len(dates) >= 2


def format_summary_table(events: list[PhaseEvent]) -> str:
    """`## 所要時間サマリ` の Markdown テーブルを生成する（Issue #2453 6 列フォーマット）.

    表全体が JST で 2 日以上にまたがる場合は全行に日付（``MM/DD``）を付記し、
    単日に収まる場合は時刻のみを表示する（Issue #3389: 実行日基準ではなく表自体の
    日付範囲で判定する）。
    """
    show_date = _table_spans_multiple_jst_dates(events)
    lines = [
        _SUMMARY_HEADER,
        "",
        "| フェーズ | 開始(JST) | 終了(JST) | 所要時間 | 主担当 | 結果 |",
        "|---|---|---|---|---|---|",
    ]
    for phase, start, end, duration, owner, result in build_rows(events, show_date=show_date):
        lines.append(f"| {phase} | {start} | {end} | {duration} | {owner} | {result} |")
    total_display = _format_duration(total_duration_seconds(events))
    lines.append(f"| **合計** | | | **{total_display}** | | |")
    return "\n".join(lines) + "\n"


def _report_reference(issue_key: str, pr_num: str) -> str:
    """quiet 出力の「表の参照先」文字列を返す（Issue #3432）.

    - PR 指定時: ``https://github.com/<owner>/<repo>/pull/<N>``（`gh repo view` が失敗した場合は
      ``PR #<N>`` にフォールバック）
    - PR 未指定時: 保存済み txt のパス（``cache/merge-summary-emitted/<N>.txt``）
    """
    if pr_num:
        repo = gh_client.repo_name_with_owner()
        if repo:
            return f"https://github.com/{repo}/pull/{pr_num}"
        return f"PR #{pr_num}"
    issue_num = _extract_issue_num(issue_key)
    if issue_num is not None:
        return str(_marker_dir() / f"{issue_num}.txt")
    return ""


def format_quiet_summary(events: list[PhaseEvent], issue_key: str, pr_num: str) -> str:
    """quiet 出力（デフォルト・Issue #3432）の 1〜2 行サマリを生成する.

    表全文（``format_summary_table`` の Markdown 表）は出力せず、
    Issue 番号・合計時間・異常フェーズ有無・表の参照先のみを 1〜2 行で stdout に出す。
    PR コメント投稿・txt 保存は常に表全文で行われるため、副作用は変わらない。
    """
    total_display = _format_duration(total_duration_seconds(events))
    anomalies = detect_anomalous_phases(events)
    if anomalies:
        names = "・".join(name for name, _ in anomalies)
        anomaly_text = f"あり（{names}）"
    else:
        anomaly_text = "なし"
    lines = [f"merge-summary: {issue_key} 合計 {total_display} 異常フェーズ{anomaly_text}"]
    reference = _report_reference(issue_key, pr_num)
    if reference:
        lines.append(f"表の参照先: {reference}")
    return "\n".join(lines) + "\n"


def format_rollup_table(entries: list[tuple[str, str]]) -> str:
    """`/issue-next` 複数 Issue 連続消化時の最終ロールアップ表を生成する.

    ``entries`` は ``(issue番号, 合計時間表示文字列)`` のリスト（発生順）。
    """
    lines = ["| issue番号 | 合計時間 |", "|---|---|"]
    for issue_num, total_display in entries:
        lines.append(f"| {issue_num} | {total_display} |")
    return "\n".join(lines) + "\n"


def format_auto_check_section(result: AutoCheckResult) -> str:
    """「## 自動チェック結果」セクションを生成する（Issue #2453）."""
    lines = [
        "## 自動チェック結果",
        "",
        "### ✅ うまくいったこと",
        "",
    ]
    # AIレビュー試行回数
    if result.review_attempts > 0:
        lines.append(f"- AIレビュー試行回数: {result.review_attempts} 回")
    # 自動テスト成否
    if result.preflight_success is True:
        lines.append("- 自動テスト（pre-flight）: 成功")
    elif result.preflight_success is False:
        lines.append("- 自動テスト（pre-flight）: 失敗")
    # AI確認項目件数
    if result.ai_confirm_count > 0:
        lines.append(f"- AI確認項目: {result.ai_confirm_count} 件")

    lines += [
        "",
        "### ⚠️ 効率化できそうなこと",
        "",
    ]
    has_warning = False
    if result.pytest_duplicate:
        # Issue #2694: pre-flight が複数周した場合は実回数（run_count + ai-review 1 回）を表示する。
        # run_count が 0（Issue 別記録なし・旧単一ファイルフォールバック時）は回数を断定できないため
        # 従来どおりの固定表現にフォールバックする。
        if result.preflight_run_count > 0:
            total_pytest_runs = result.preflight_run_count + 1
            lines.append(f"- 同じ pytest が {total_pytest_runs} 回実行された（pre-flight + ai-review）")
        else:
            lines.append("- 同じ pytest が 2 回実行された（pre-flight + ai-review）")
        has_warning = True
    if result.review_wait_ratio is not None and result.review_wait_ratio > 0.5:
        pct = round(result.review_wait_ratio * 100)
        lines.append(f"- AIレビュー待ち割合が高い（{pct}%）")
        has_warning = True
    # pre-flight 複数回実行（Issue #2637）
    if result.preflight_run_count >= 2:
        lines.append(f"- pre-flight が {result.preflight_run_count} 回実行された（修正の繰り返しが発生した可能性）")
        has_warning = True
    if not has_warning:
        lines.append("- なし")

    lines += [
        "",
        "### ℹ️ 参考情報",
        "",
    ]
    if result.changed_files > 0:
        lines.append(f"- 変更ファイル数: {result.changed_files}")
    if result.additions > 0 or result.deletions > 0:
        lines.append(f"- 変更行数: +{result.additions} / -{result.deletions}")
    if result.size_label != "-":
        lines.append(f"- size ラベル: {result.size_label}")
    if result.review_backend != "-":
        lines.append(f"- レビューバックエンド: {result.review_backend}")

    return "\n".join(lines) + "\n"


def _closed_duration_seconds(record: dict[str, object]) -> float | None:
    started = record.get("started_at")
    ended = record.get("ended_at")
    if not started or not ended:
        return None
    try:
        started_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        ended_dt = datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (ended_dt - started_dt).total_seconds()


def _parse_dt(value: object) -> datetime | None:
    """ISO 8601 文字列を UTC datetime に変換する（失敗時は None）."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_marker_age(marker_written_at: object, reference: datetime | None) -> str | None:
    """マーカーの書き込み時刻を、検証開始時刻からの相対経過として整形する（Issue #2800）.

    「このマーカーは今回の検証が始まるより前に書かれていた（＝過去の実行を再利用した）」
    ことを一目で確認できるようにするための表示。パース失敗・未来時刻（不整合）の場合は
    None を返し、呼び出し元は詳細なしの表示にフォールバックする。
    """
    written = _parse_dt(marker_written_at)
    if written is None or reference is None:
        return None
    delta = (reference - written).total_seconds()
    if delta < 0:
        return None
    if delta < 60:
        return f"{int(delta)}秒前"
    if delta < 3600:
        return f"{int(delta // 60)}分前"
    if delta < 86400:
        return f"{int(delta // 3600)}時間前"
    return f"{int(delta // 86400)}日前"


# ── 統一イベントログ第一ソース（Issue #2935・#3322 専用化）──────────────────
#
# `tidd_tools.timing_log`（Issue #2933/#2934）の per-issue 統一イベントログを
# report 生成の唯一のデータソースとして扱う。旧 5 系統ファイル（`ai-review-timing/*.jsonl`・
# `pre-flight/preflight-record.json`・`pre-flight/issue-N.jsonl`・`ai-reviewer/pr-<N>/timing.json`）
# への読み側フォールバックは #3322 で撤去済み（過去 Issue は手動 import で対応）。

# レポート生成に必須の step 名（#2902: マーク未完了のまま report が呼ばれた場合、
# 別ラウンドの値を誤って再利用してしまう不具合の再発防止・やること3項目目）。
# いずれかが最新 attempt に欠けていれば report 生成をブロックする。
REQUIRED_REPORT_STEPS: tuple[str, ...] = ("step6-merged", "step6-cleanup-done")


def filter_latest_attempt(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """統一イベントログのレコードを最新 attempt_id のみに絞り込む（やること2項目目）.

    中断・再着手（#2901/#2911）した Issue では過去 attempt のイベントが同じログに
    残り続けるため、最新 attempt 以外は集計対象から除外する。attempt 境界イベント
    （``kind == "attempt-start"``）自体も集計対象ではないため除外する。

    ``records`` が空、または attempt_id を持つレコードが 1 件もない場合は空リストを返す
    （呼び出し元はこれを「統一イベントログ未移行」の合図として旧フォールバックへ切り替える）。
    """
    attempt_ids: list[int] = [aid for r in records if isinstance(aid := r.get("attempt_id"), int)]
    if not attempt_ids:
        return []
    latest = max(attempt_ids)
    return [r for r in records if r.get("attempt_id") == latest and r.get("kind") != "attempt-start"]


def _unified_log_records(issue_key: str) -> list[dict[str, Any]]:
    """統一イベントログの最新 attempt レコードを返す（#3127・Phase 6〜9 第一ソース化）.

    ``timing_log.read_events``（DB・lazy JSONL migration）から最新 attempt のみを
    取り出す。レコードが 1 件も無い場合は空リストを返し、呼び出し元は
    旧ファイルフォールバックへ切り替える（#3126 マージ前 PR・config off 環境対応）。
    """
    return filter_latest_attempt(timing_log.read_events(issue_key))


def _unified_step_records(records: list[dict[str, Any]], step: str) -> list[dict[str, Any]]:
    """統一ログレコードから指定 step のイベントを返す（#3127）."""
    return [r for r in records if r.get("step") == step]


def _unified_event_dt(record: dict[str, Any] | None, *, field: str = "timestamp") -> datetime | None:
    """統一ログレコードから時刻を取り出す（timestamp / meta.started_at / meta.ended_at）."""
    if record is None:
        return None
    if field == "timestamp":
        return _parse_dt(record.get("timestamp"))
    meta = record.get("meta") or {}
    return _parse_dt(meta.get(field))


def _impl_delegation_owner(records: list[dict[str, Any]]) -> str:
    """実装フェーズの主担当を解決する（Issue #3155）.

    対象 `issue-<N>` キーの統一ログに `impl-delegation-used` イベントが存在する場合、
    meta.backend（agy / codex / custom）に応じて主担当を上書きする
    （custom のみ表記を大文字化）。イベントが無い・backend 欠落の場合は
    従来どおり ``"Claude Code"`` を返す（フォールバック）。
    """
    delegated = [
        (r.get("meta") or {}).get("backend")
        for r in records
        if r.get("step") == "impl-delegation-used" and (r.get("meta") or {}).get("backend")
    ]
    if not delegated:
        return "Claude Code"
    backend = str(delegated[-1])
    if backend == "custom":
        return "Custom"
    return backend


def _build_review_events_from_unified(
    verdict_records: list[dict[str, Any]],
    issue_records: list[dict[str, Any]],
) -> list[PhaseEvent] | None:
    """統一ログ（ai-review-verdict + step5-airview marks）から AIレビュー行を組み立てる（#3127）.

    ai-review-verdict 1 件 = 1 試行行。airview mark 件数が試行数と一致する場合は
    i 番目の start/end を対応付け、一致しない場合は両端割り当て（airview_starts[0]
    〜 airview_ends[-1]）に縮退する（#3553・旧実装 #2779 と同じルール）。
    meta.started_at が欠落しているレコードが混在する場合は None を返し、
    呼び出し元は旧ソースへフォールバックする。

    Issue #4157: 両端割り当てフォールバックは verdict_records が 1 件のときのみ
    適用する。verdict_records が複数件（リトライ）の場合に airview mark が
    1 組だけ存在すると、pairwise 不一致により全試行行へ同一の両端割り当て値が
    重複適用され、2 回目以降の行が 1 回目と同じ（誤った）時刻で表示される
    不具合があった（PR #4140 実例）。複数件の場合は各 verdict の meta
    started_at/ended_at をそのまま使う。
    """
    airview_starts = _unified_step_records(issue_records, "step5-airview-start")
    airview_ends = _unified_step_records(issue_records, "step5-airview-end")
    pairwise = (
        len(verdict_records) > 0
        and len(airview_starts) == len(verdict_records)
        and len(airview_ends) == len(verdict_records)
    )

    events: list[PhaseEvent] = []
    for i, rec in enumerate(verdict_records):
        meta = rec.get("meta") or {}
        if not meta.get("started_at"):
            return None  # #3126 マージ前レコード → 統一ログ非対応としてフォールバック
        verdict = str(meta.get("verdict") or "")
        result = _VERDICT_RESULT_LABELS.get(verdict, verdict or "-")
        backend = str(meta.get("backend") or "")
        owner = backend.split(":", 1)[0] if backend else "-"
        start_at = _parse_dt(meta.get("started_at"))
        end_at = _parse_dt(meta.get("ended_at"))
        duration: float | None = None
        if start_at is not None and end_at is not None:
            diff = (end_at - start_at).total_seconds()
            duration = diff if diff >= 0 else None

        if pairwise and i < len(airview_starts) and i < len(airview_ends):
            start_at = _unified_event_dt(airview_starts[i])
            end_at = _unified_event_dt(airview_ends[i])
            if start_at is not None and end_at is not None:
                diff = (end_at - start_at).total_seconds()
                duration = diff if diff >= 0 else duration
        elif len(verdict_records) == 1 and airview_starts and airview_ends:
            # Issue #3553: mark の二重記録等で件数が一致しない場合も、両端割り当て
            # （最初の start 〜 最後の end）で実測を復元する。verdict meta の 0 秒表示
            # （旧バグ形状）を防ぎ、⚠️計測欠落はこの両端結果が 0 秒のときのみ表示する。
            # Issue #4157: verdict_records が複数件の場合はここに入らず、各 verdict の
            # meta 値（start_at/end_at・上で算出済み）のまま使う（重複割り当て防止）。
            start_at = _unified_event_dt(airview_starts[0])
            end_at = _unified_event_dt(airview_ends[-1])
            if start_at is not None and end_at is not None:
                diff = (end_at - start_at).total_seconds()
                duration = diff if diff >= 0 else duration

        events.append(
            PhaseEvent(
                "AIレビュー(プライマリ)",
                duration_seconds=duration,
                result=result,
                owner=owner,
                start_at=start_at,
                end_at=end_at,
            )
        )
    return events


def _build_test_plan_event_from_unified(
    gate_records: list[dict[str, Any]],
    aiconfirm_start_records: list[dict[str, Any]],
    aiconfirm_end_records: list[dict[str, Any]],
) -> list[PhaseEvent] | None:
    """統一ログから「PRテスト計画検証」行を組み立てる（#3127・対応表 2 行目）."""
    gate_record = gate_records[0] if gate_records else None
    gate_start_at = _unified_event_dt(gate_record, field="started_at") if gate_record else None
    gate_end_at = _unified_event_dt(gate_record, field="ended_at") if gate_record else None
    gate_duration: float | None = None
    if gate_start_at is not None and gate_end_at is not None:
        diff = (gate_end_at - gate_start_at).total_seconds()
        gate_duration = diff if diff >= 0 else None

    aiconfirm_start_at: datetime | None = None
    aiconfirm_end_at: datetime | None = None
    aiconfirm_duration: float | None = None
    if aiconfirm_start_records and aiconfirm_end_records:
        aiconfirm_start_at = _unified_event_dt(aiconfirm_start_records[0])
        aiconfirm_end_at = _unified_event_dt(aiconfirm_end_records[-1])
        if aiconfirm_start_at is not None and aiconfirm_end_at is not None:
            diff = (aiconfirm_end_at - aiconfirm_start_at).total_seconds()
            aiconfirm_duration = diff if diff >= 0 else None

    if gate_record is None and aiconfirm_start_at is None:
        return []

    total_duration: float | None = None
    if gate_duration is not None and aiconfirm_duration is not None:
        total_duration = gate_duration + aiconfirm_duration
    elif gate_duration is not None:
        total_duration = gate_duration
    elif aiconfirm_duration is not None:
        total_duration = aiconfirm_duration

    start_at = gate_start_at if gate_start_at is not None else aiconfirm_start_at
    end_at = aiconfirm_end_at if aiconfirm_end_at is not None else gate_end_at
    parts: list[str] = []
    if gate_duration is not None:
        parts.append(f"ゲート {_format_duration(gate_duration)}")
    if aiconfirm_duration is not None:
        parts.append(f"AI確認 {_format_duration(aiconfirm_duration)}")
    result = " / ".join(parts) if parts else "-"
    return [
        PhaseEvent(
            "PRテスト計画検証",
            duration_seconds=total_duration,
            result=result,
            owner="tidd test-plan / Claude",
            start_at=start_at,
            end_at=end_at,
        )
    ]


def _build_yaru_event_from_unified(
    pr_records: list[dict[str, Any]],
    yaru_record: dict[str, Any],
) -> list[PhaseEvent] | None:
    """統一ログから「Issueやること検証」行を組み立てる（#3127・対応表 3 行目）."""
    start_at = _unified_event_dt(yaru_record, field="started_at")
    if start_at is None:
        return None  # 統一ログで判別できない場合は旧ソースへフォールバック
    exhaustion_records = _unified_step_records(pr_records, "issue-exhaustion-gate")
    if exhaustion_records:
        end_at = _unified_event_dt(exhaustion_records[-1], field="ended_at")
    else:
        end_at = _unified_event_dt(yaru_record, field="ended_at")
    duration: float | None = None
    if start_at is not None and end_at is not None:
        diff = (end_at - start_at).total_seconds()
        duration = diff if diff >= 0 else None
    return [
        PhaseEvent(
            "Issueやること検証",
            duration_seconds=duration,
            owner="tidd ai-review (yaru)",
            start_at=start_at,
            end_at=end_at,
        )
    ]


def _merge_events_from_unified(issue_key: str | None, pr_num: str | None) -> list[PhaseEvent] | None:
    """統一ログから「CI・マージ」行を組み立てる（#3127・対応表 4 行目）.

    step6-merge-start 〜 step6-merged（issue-<N>）が無い場合は auto-merge span
    （pr-<N>）を試す。両方無ければ None（旧ソースへフォールスルー）。
    """
    if issue_key is not None:
        unified_issue = _unified_log_records(issue_key)
        merge_start_records = _unified_step_records(unified_issue, "step6-merge-start")
        merged_records = _unified_step_records(unified_issue, "step6-merged")
        if merge_start_records and merged_records:
            start_at = _unified_event_dt(merge_start_records[0])
            end_at = _unified_event_dt(merged_records[-1])
            duration: float | None = None
            if start_at is not None and end_at is not None:
                diff = (end_at - start_at).total_seconds()
                duration = diff if diff >= 0 else None
            return [
                PhaseEvent(
                    "CI・マージ",
                    duration_seconds=duration,
                    owner="gh",
                    start_at=start_at,
                    end_at=end_at,
                )
            ]
        if pr_num is not None:
            unified_pr = _unified_log_records(f"pr-{pr_num}")
            auto_merge_records = _unified_step_records(unified_pr, "auto-merge")
            if auto_merge_records:
                auto_start_at = _unified_event_dt(auto_merge_records[0], field="started_at")
                auto_end_at = _unified_event_dt(auto_merge_records[-1], field="ended_at")
                auto_duration: float | None = None
                if auto_start_at is not None and auto_end_at is not None:
                    diff_auto = (auto_end_at - auto_start_at).total_seconds()
                    auto_duration = diff_auto if diff_auto >= 0 else None
                return [
                    PhaseEvent(
                        "CI・マージ",
                        duration_seconds=auto_duration,
                        owner="gh",
                        start_at=auto_start_at,
                        end_at=auto_end_at,
                    )
                ]
    return None


def check_report_completeness(
    records: list[dict[str, Any]], required_steps: tuple[str, ...] = REQUIRED_REPORT_STEPS
) -> list[str]:
    """レポート生成に必須の step が最新 attempt に揃っているかを確認する（やること3項目目）.

    ``records`` は ``filter_latest_attempt`` 済みであることを前提とする。
    auto-merge 直後のように後処理がまだ実行されていない経路では、呼び出し元が
    ``required_steps`` を短縮して cleanup 完了を後続処理に委ねる。
    欠落している必須 step 名のリストを返す（揃っていれば空リスト）。
    """
    present_steps = {r.get("step") for r in records}
    return [step for step in required_steps if step not in present_steps]


def _sorted_log_events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """統一イベントログのレコードを timestamp 昇順に安定ソートする.

    書き込み順や取得順に依存せず常に時系列順で処理するため（やること6項目目:
    「順序逆転」異常系への耐性）。timestamp が欠落・不正なレコードは末尾に送る。
    """

    def _key(record: dict[str, Any]) -> tuple[int, str]:
        ts = record.get("timestamp")
        return (0, str(ts)) if ts else (1, "")

    return sorted(records, key=_key)


def _log_event_dt(record: dict[str, Any] | None) -> datetime | None:
    """統一イベントログの 1 レコードから timestamp（UTC datetime）を取り出す."""
    if record is None:
        return None
    return _parse_dt(record.get("timestamp"))


def _pr_created_dt(record: dict[str, Any] | None) -> datetime | None:
    """step4-pr-created レコードから PR 作成時刻を返す（meta.created_at 優先・#3552）.

    hook（record-timing-boundaries.py）が ``gh pr view --json createdAt`` で取得した
    実 createdAt（``meta.created_at``）を優先し、欠落時は event timestamp
    （hook 発火時刻）にフォールバックする（#3516 の GitHub API フォールバックは
    呼び出し側 ``_build_report_events`` が引き続き担う）。
    """
    if record is None:
        return None
    meta = record.get("meta")
    if isinstance(meta, dict):
        created_at = meta.get("created_at")
        if isinstance(created_at, str) and created_at:
            parsed = _parse_dt(created_at)
            if parsed is not None:
                return parsed
    return _log_event_dt(record)


def _first_log_event(records: list[dict[str, Any]], step: str) -> dict[str, Any] | None:
    """指定 step の最初のレコードを返す（見つからなければ None）."""
    for r in records:
        if r.get("step") == step:
            return r
    return None


def _last_log_event(records: list[dict[str, Any]], step: str) -> dict[str, Any] | None:
    """指定 step の最後のレコードを返す（見つからなければ None）."""
    result: dict[str, Any] | None = None
    for r in records:
        if r.get("step") == step:
            result = r
    return result


def _make_log_row(
    name: str,
    owner: str,
    start_event: dict[str, Any] | None,
    end_event: dict[str, Any] | None,
    *,
    result: str = "-",
    end_dt_override: datetime | None = None,
) -> PhaseEvent:
    """統一イベントログの 2 イベントから PhaseEvent を組み立てる.

    ``start_event`` / ``end_event`` それぞれが取得できたぶんだけ ``start_at`` /
    ``end_at`` に反映する。``end_dt_override`` を指定すると ``end_event`` の
    timestamp の代わりにその値を終了時刻として使う
    （例: step4-pr-created の meta.created_at を優先する場合・#3552）。
    終了時刻が開始時刻より前（別ラウンドの値を誤ってペアリング
    した等の不正な組み合わせ）の場合は終了時刻を破棄し「-」のままにする
    （やること4項目目: 別ラウンドのタイムスタンプを再利用しない）。
    """
    start_at = _log_event_dt(start_event)
    end_at = end_dt_override if end_dt_override is not None else _log_event_dt(end_event)
    if start_at is not None and end_at is not None and end_at < start_at:
        end_at = None
    duration = (end_at - start_at).total_seconds() if start_at is not None and end_at is not None else None
    return PhaseEvent(name, duration_seconds=duration, owner=owner, start_at=start_at, end_at=end_at, result=result)


def _adjacent_pair_rows(
    events: list[dict[str, Any]],
    start_step: str,
    end_step: str,
    label: str,
    owner: str,
) -> list[PhaseEvent]:
    """``start_step`` の出現ごとに、同ラウンド内の ``end_step`` とだけペアリングして行を作る.

    ``start_step`` が複数回記録されている場合（リトライラウンド）、各出現の
    探索範囲を「その出現時刻 ～ 次の ``start_step`` 出現時刻」に限定することで、
    別ラウンドの ``end_step`` を誤って再利用しない（やること4項目目）。
    範囲内に対応する ``end_step`` が見つからない場合は「計測不可」の行を返す
    （＝別ラウンドの値で埋めない）。
    """
    starts = [r for r in events if r.get("step") == start_step]
    ends = [r for r in events if r.get("step") == end_step]
    rows: list[PhaseEvent] = []
    for i, start_event in enumerate(starts):
        start_at = _log_event_dt(start_event)
        window_end_at = _log_event_dt(starts[i + 1]) if i + 1 < len(starts) else None
        matched: dict[str, Any] | None = None
        for end_event in ends:
            end_at = _log_event_dt(end_event)
            if start_at is None or end_at is None or end_at < start_at:
                continue
            if window_end_at is not None and end_at >= window_end_at:
                continue
            matched = end_event
            break
        result = "-"
        if matched is not None:
            exit_code = matched.get("meta", {}).get("exit_code") if isinstance(matched.get("meta"), dict) else None
            if exit_code is not None:
                result = _exit_code_to_result(exit_code)
        rows.append(_make_log_row(label, owner, start_event, matched, result=result))
    return rows


# 「実装（修正）」行のラベル（Issue #2937）。
# tree hash が rework 前後で不変（＝ファイル内容が変化していない）と判定できた場合は
# レビュー起因の実装作業と誤認しないよう区別可能なラベルに変更する（#2919 統合）。
_REWORK_LABEL = "実装（修正）"
_REWORK_LABEL_NO_CHANGE = "実装（修正・ファイル変更なし）"


def _event_tree_hash(event: dict[str, Any] | None) -> object:
    """統一イベントログの 1 レコードから ``meta.tree_hash`` を取り出す（Issue #2937）."""
    if event is None:
        return None
    meta = event.get("meta")
    if not isinstance(meta, dict):
        return None
    return meta.get("tree_hash")


def _rework_rows_from_log(events: list[dict[str, Any]], *, owner: str = "Claude Code") -> list[PhaseEvent]:
    """「実装（修正）」行を統一イベントログから組み立てる（Issue #2735・#2937・#3555）.

    ``step3-rework-start`` を起点とせず、**連続する pre-flight ラウンドの隙間**
    （``step3-preflight-end``[k] ～ ``step3-preflight-start``[k+1]）から「実装（修正）」行を
    導出する（Issue #3555）。pre-flight が失敗した直後から次の pre-flight 実行までの区間は
    定義上「修正作業」以外に何も起こらないため、両端は実測イベントのみで一意に決まり、
    LLM 手打ち mark（``step3-rework-start``）に依存しない。

    ラウンドが 1 回だけ（``step3-preflight-start`` が 1 件）の場合は「実装（修正）」行を
    1 件も生成しない。``step3-rework-start`` レコードが存在しても無視する
    （過去データとの後方互換・二重行を出さない）。

    ``_REWORK_LABEL_NO_CHANGE`` の tree hash 判定: 区間の前後の ``step3-preflight-end``
    （``step3-preflight-end``[k] と ``step3-preflight-end``[k+1]）の ``meta.tree_hash`` を比較し、
    両方が取得でき値が一致する場合（＝ファイル内容が変化していない）はラベルを区別可能な
    名称に変更する（Issue #2919 統合。前 round が成功していたかどうかは問わない）。
    どちらか一方でも tree_hash が欠落している場合は従来どおり「実装（修正）」のままにする
    （旧形式レコードとの後方互換・過検出防止）。
    """
    pf_starts = [r for r in events if r.get("step") == "step3-preflight-start"]
    pf_ends = [r for r in events if r.get("step") == "step3-preflight-end"]
    rows: list[PhaseEvent] = []
    # 連続する pre-flight ラウンドの隙間（ラウンド数 - 1 件）ごとに 1 行生成する
    for i in range(len(pf_starts) - 1):
        prev_end = pf_ends[i] if i < len(pf_ends) else None
        next_end = pf_ends[i + 1] if i + 1 < len(pf_ends) else None
        prev_tree = _event_tree_hash(prev_end)
        next_tree = _event_tree_hash(next_end)
        label = _REWORK_LABEL_NO_CHANGE if prev_tree and next_tree and prev_tree == next_tree else _REWORK_LABEL
        rows.append(_make_log_row(label, "Claude Code", prev_end, pf_starts[i + 1]))
    return rows


def build_events_from_log(records: list[dict[str, Any]]) -> list[PhaseEvent]:
    """統一イベントログのレコード列から Phase 1〜5（プルリクエストまで）を組み立てる純関数.

    Issue #2935 やること1・6項目目: 「イベント列 → 表」を純粋関数として実装し、
    I/O を一切行わない（テーブル駆動テストで異常系を直接検証できるようにするため）。

    ``records`` は ``filter_latest_attempt`` 済み（最新 attempt のみ・attempt-start
    除外済み）であることを前提とする。入力の順序には依存しない（内部で timestamp
    昇順にソートしてから処理する）。

    Phase 6 以降（AIレビュー・PRテスト計画検証・Issueやること検証・CI・マージ・後処理）
    は引き続き既存の pr-<N> キーの旧ローダーが担当するため、本関数のスコープには含めない
    （呼び出し元 ``run_cli`` で連結する。理由: それらのローダーが読む記録先ファイルは
    新旧どちらの経路でも同一であり、複製すると二重行になる）。
    """
    events = _sorted_log_events(records)
    rows: list[PhaseEvent] = []

    # Issue品質チェック: step1-confirmed ～ step1.5-quality-check
    quality_start = _first_log_event(events, "step1-confirmed")
    quality_end = _first_log_event(events, "step1.5-quality-check")
    if quality_end is not None:
        rows.append(_make_log_row("Issue品質チェック", "Claude Code", quality_start, quality_end))
    else:
        rows.append(PhaseEvent("Issue品質チェック", duration_seconds=None, owner="Claude Code"))

    # ブランチ作成: step2-implementation ～ step2-branch-created
    rows.extend(
        _adjacent_pair_rows(events, "step2-implementation", "step2-branch-created", "ブランチ作成", "git worktree・uv")
    )

    # 実装: step2-branch-created ～ 最初の step3-preflight-start（#3558）
    # step3-edit-done は廃止。旧データに残るレコードは無視する（後方互換・二重行を出さない）。
    # step3-preflight-start は pre_flight.py が自己記録するため LLM に依存しない。
    # 実装（修正）行は _rework_rows_from_log が pre-flight ラウンド間から導出するため、
    # 本行は「worktree 作成 ～ 初回検証開始」の純粋な初回実装区間を表す。
    impl_owner = _impl_delegation_owner(records)
    impl_start = _first_log_event(events, "step2-branch-created")
    if impl_start is not None:
        impl_end = _first_log_event(events, "step3-preflight-start")
        rows.append(_make_log_row("実装", impl_owner, impl_start, impl_end))

    # step4-pr-created 時刻で PR 作成前後を区分する（検証・テスト／実装（修正）は PR 作成前のみ・#2733 相当）
    pr_created_event = _first_log_event(events, "step4-pr-created")
    # #3552: event timestamp（hook 発火時刻）より meta.created_at（PR の実 createdAt）を優先する
    pr_created_at = _pr_created_dt(pr_created_event)
    if pr_created_at is not None:
        pre_pr_events = [e for e in events if (dt := _log_event_dt(e)) is not None and dt <= pr_created_at]
    else:
        pre_pr_events = events

    # 実装（修正）: 連続する pre-flight ラウンドの隙間
    # （step3-preflight-end[k] ～ step3-preflight-start[k+1]、PR 作成前のラウンドのみ）
    # tree hash 不変（空 diff コミット等）の場合は区別可能なラベルに変更する（#2937・#2919統合・#3555）
    rows.extend(_rework_rows_from_log(pre_pr_events, owner=impl_owner))

    # 検証・テスト: step3-preflight-start ～ step3-preflight-end（PR 作成前のラウンドのみ）
    rows.extend(
        _adjacent_pair_rows(
            pre_pr_events, "step3-preflight-start", "step3-preflight-end", "検証・テスト", "pytest・mypy・ruff"
        )
    )

    # プルリクエスト: PR 作成前の最後の step3-preflight-end ～ step4-pr-created
    # end_dt_override=pr_created_at: meta.created_at（PR の実 createdAt）を終了時刻として優先する（#3552）
    last_preflight_end = _last_log_event(pre_pr_events, "step3-preflight-end")
    rows.append(
        _make_log_row(
            "プルリクエスト",
            "Claude Code",
            last_preflight_end,
            pr_created_event,
            end_dt_override=pr_created_at,
        )
    )

    # 各フェーズは直前フェーズの終了イベントを起点として構築しているため、
    # この時点で rows は自然に時系列昇順になっている。Phase 6 以降を連結した
    # 全体の最終ソートは呼び出し元 `_run_report_cli` が `_sort_events_by_start_at`
    # で行う（Issue #3389: ここでの部分ソートは二重ソートになるため撤去）。
    return rows


def _sort_events_by_start_at(events: list[PhaseEvent]) -> list[PhaseEvent]:
    """Phase 1〜9 全連結後のイベント列を start_at 昇順に安定ソートする（Issue #3389）.

    Phase 6 以降（AIレビュー・CI・マージ・後処理等）は個別ローダーが実測順とは無関係に
    リスト末尾へ連結されるため、全フェーズ連結後にこの関数で再ソートする必要がある
    （PR #3373 の実例: CI・マージ の開始が AIレビュー より先であるべきところ後に表示されていた）。
    start_at が取得できない行（計測不可）は末尾に送る。
    """
    return sorted(events, key=lambda e: e.start_at or datetime.max.replace(tzinfo=UTC))


def _exit_code_to_result(
    exit_code: object,
    failures: object = None,
    *,
    skipped: bool = False,
    pytest_skip_reason: object = None,
    marker_written_at: object = None,
    marker_reference_at: datetime | None = None,
) -> str:
    """pre-flight の `exit_code` から結果列の表示文字列を組み立てる（Issue #2694・#2732・#2756・#2778）.

    ``0`` → ``"成功"`` / それ以外の値 → ``"失敗"``（または ``"失敗（理由）"``） / ``None``（未取得） → ``"-"``。

    ``failures`` に文字列リストが指定されている場合、失敗時に
    ``"失敗（<failures を ' / ' で連結した文字列>）"`` の形式で表示する（Issue #2732）。
    旧フォーマット（``failures`` フィールドなし・``failures`` が空リスト）では従来どおり
    ``"失敗"`` を返す（後方互換・フォールバック保証）。

    ``skipped=True`` かつ ``exit_code == 0`` の場合は ``"スキップ（差分なし）"`` を返す（Issue #2756）。
    差分ゼロでチェックを実行しなかった実行を「成功（チェック実行済み）」と区別するために使う。
    ``exit_code`` が 0 以外の場合は ``skipped=True`` でも「失敗」を返す。

    ``pytest_skip_reason`` が指定された（None でない）かつ ``exit_code == 0`` の場合は
    ``"成功(スキップ)"`` を返す（Issue #2778）。pytest が未実行（マーカー hit・py プロジェクト
    未検出等）だった周回を「成功（実行済み）」と区別するために使う。
    ``exit_code`` が 0 以外の場合は ``pytest_skip_reason`` があっても「失敗」を返す。
    優先順: skipped（差分なし）> pytest_skip_reason > 通常の成功/失敗。

    ``pytest_skip_reason == "マーカー hit"`` かつ ``marker_written_at``/``marker_reference_at``
    が両方指定されている場合、マーカーが検証開始時刻の何分前に書かれたかを付記する
    （例: ``"成功(スキップ・マーカー12分前)"``・Issue #2800）。算出できない場合は
    従来どおり ``"成功(スキップ)"`` を返す（後方互換）。
    """
    if exit_code == 0:
        if skipped:
            return "スキップ（差分なし）"
        if pytest_skip_reason is not None:
            if pytest_skip_reason == "マーカー hit":
                age = _format_marker_age(marker_written_at, marker_reference_at)
                if age is not None:
                    return f"成功(スキップ・マーカー{age})"
            return "成功(スキップ)"
        return "成功"
    if exit_code is not None:
        if isinstance(failures, list) and failures:
            return "失敗（" + " / ".join(str(f) for f in failures) + "）"
        return "失敗"
    return "-"


# pre-flight 自己記録の `checks` フィールド値 → 表示ラベルの対応表（Issue #2859）。
# 同じ表示名になるチェック（ruff-format/ruff-lint → "ruff"）は重複排除して 1 個にまとめる。
_PREFLIGHT_CHECK_LABELS: dict[str, str] = {
    "pytest": "pytest",
    "jest": "Jest",
    "ruff-format": "ruff",
    "ruff-lint": "ruff",
    "mypy": "mypy",
    "gherkin-lint": "gherkin-lint",
    "mermaid-lint": "mermaid-lint",
    "health-check": "health-check",
    "context-budget": "context-budget",
    "prettier-css": "prettier",
    "stylelint-css": "stylelint",
}


def _preflight_checks_owner(record: dict[str, Any] | None) -> str:
    """pre-flight 自己記録の `checks` フィールドから「検証・テスト」行の owner 表示文字列を組み立てる.

    Issue #2859: 従来は実際の実行有無に関わらず固定文字列 ``"pytest・mypy・ruff"`` を表示していたが、
    ruff/mypy は変更対象 Python プロジェクトによってはスキップされ、pytest 自体もキャッシュにより
    スキップされることがあるため誤解を招いていた。`checks` フィールドに記録された
    実際の実行チェック名から表示文字列を組み立てる。

    - ``record`` が ``None`` または ``checks`` フィールドを持たない（旧形式・#2859 以前）
      → ``"不明"``（後方互換フォールバック）
    - ``checks`` が空リスト（新形式だが実行チェック 0 件） → ``"-"``
    - それ以外 → ``_PREFLIGHT_CHECK_LABELS`` でラベル変換し、重複排除して ``"・"`` で連結
    """
    if record is None or "checks" not in record:
        return "不明"
    checks = record.get("checks")
    if not isinstance(checks, list) or not checks:
        return "-"
    labels: list[str] = []
    for check in checks:
        label = _PREFLIGHT_CHECK_LABELS.get(str(check), str(check))
        if label not in labels:
            labels.append(label)
    return "・".join(labels)


# ── PR 作成後の「実装（修正）」・「検証・テスト」行（統一日誌専用・#3322）──────


def _preflight_rows_from_log(
    records: list[dict[str, Any]],
    *,
    pr_created_at: datetime | None = None,
) -> list[tuple[datetime, PhaseEvent]]:
    """統一日誌の pre-flight 記録から「検証・テスト」行を組み立てる（#3322）.

    pre-flight の完全レコードは ``step3-preflight-end``（kind=end）の meta
    （started_at / ended_at / exit_code / failures / checks / pytest_skip_reason /
    marker_written_at・pre_flight.py 参照）が担う。start レコードの meta に
    完全レコードが書かれる形式（#3127 互換）にも対応する。

    ``pr_created_at`` が指定されている場合は PR 作成後に開始したラウンドのみ返す
    （#2733 相当・旧 ``_get_post_pr_preflight_records`` の統一日誌専用版）。
    戻り値は ``(started_at, PhaseEvent)`` のリスト（started_at 昇順）。
    """
    complete: list[dict[str, Any]] = []
    seen: set[datetime] = set()

    for end_rec in _unified_step_records(records, "step3-preflight-end"):
        meta = end_rec.get("meta") or {}
        started_at = _parse_dt(meta.get("started_at"))
        ended_at = _parse_dt(meta.get("ended_at"))
        exit_code = meta.get("exit_code")
        if started_at is None or ended_at is None or exit_code is None:
            continue
        if started_at in seen:
            continue
        seen.add(started_at)
        complete.append(meta)

    # start レコードの meta に完全レコードが書かれる形式への互換（#3127）
    for start_rec in _unified_step_records(records, "step3-preflight-start"):
        meta = start_rec.get("meta") or {}
        started_at = _parse_dt(meta.get("started_at"))
        ended_at = _parse_dt(meta.get("ended_at"))
        exit_code = meta.get("exit_code")
        if started_at is None or ended_at is None or exit_code is None or started_at in seen:
            continue
        seen.add(started_at)
        complete.append(meta)

    rows: list[tuple[datetime, PhaseEvent]] = []
    for meta in complete:
        started_at = _parse_dt(meta.get("started_at"))
        ended_at = _parse_dt(meta.get("ended_at"))
        if started_at is None or ended_at is None:
            continue
        if pr_created_at is not None and started_at <= pr_created_at:
            continue
        duration = _closed_duration_seconds(meta)
        result = _exit_code_to_result(
            meta.get("exit_code"),
            meta.get("failures"),
            skipped=bool(meta.get("skipped")),
            pytest_skip_reason=meta.get("pytest_skip_reason"),
            marker_written_at=meta.get("marker_written_at"),
            marker_reference_at=started_at,
        )
        rows.append(
            (
                started_at,
                PhaseEvent(
                    "検証・テスト",
                    duration_seconds=duration,
                    result=result,
                    owner=_preflight_checks_owner(meta),
                    start_at=started_at,
                    end_at=ended_at,
                ),
            )
        )
    rows.sort(key=lambda item: item[0])
    return rows


def _build_fix_rounds_from_log(
    records: list[dict[str, Any]],
    verdict_records: list[dict[str, Any]],
    post_pr_preflight_rows: list[tuple[datetime, PhaseEvent]],
) -> list[tuple[int, PhaseEvent, PhaseEvent | None]]:
    """統一日誌から各レビュー試行後の「実装（修正）」・「検証・テスト」行を組み立てる（#3322）.

    旧実装 ``_load_fix_events_for_review``（timing.json + 旧 jsonl 読み・コンフリクト
    解消コミット検出）の統一日誌専用版。``step5-fix-start``（issue-<N>・point mark）を
    ``ai-review-verdict`` の ``meta.ended_at`` を試行境界として窓掛けし、
    「実装（修正）」行を生成する。mark 欠落時は「計測不可」の行を生成して
    行欠落を防ぐ（#2733 異常系仕様）。

    各レビュー試行後の「検証・テスト」行は ``post_pr_preflight_rows`` のうち
    fix 開始後・次レビュー開始前に開始した最初のラウンドを採用する。

    Returns:
        [(review_index, fix_event, preflight_event_or_None)] のリスト。
        最後のレビュー試行（APPROVE）の後には挿入しない。
    """
    fix_records = [r for r in _sorted_log_events(records) if r.get("step") == "step5-fix-start"]

    result: list[tuple[int, PhaseEvent, PhaseEvent | None]] = []
    n = len(verdict_records)
    for k in range(n - 1):
        review_end = _parse_dt((verdict_records[k].get("meta") or {}).get("ended_at"))
        next_review_end = _parse_dt((verdict_records[k + 1].get("meta") or {}).get("ended_at"))
        if review_end is None or next_review_end is None:
            continue

        # k 番目レビュー終了後・(k+1) 番目レビュー終了前にある step5-fix-start を探す
        fix_rec: dict[str, Any] | None = None
        for r in fix_records:
            r_started = _log_event_dt(r)
            if r_started is None:
                continue
            if review_end <= r_started <= next_review_end:
                fix_rec = r
                break

        if fix_rec is not None:
            # step5-fix-start は point mark（timestamp のみ・#3322）のため
            # 所要時間は「計測不可」として扱う（旧 close-previous/open-new の ended_at は
            # 統一日誌に存在しない）。
            fix_event = PhaseEvent(
                "実装（修正）",
                duration_seconds=None,
                result="-",
                owner="Claude Code",
                start_at=_log_event_dt(fix_rec),
            )
        else:
            # step5-fix-start 欠落: 「計測不可」の行を生成して行欠落を防ぐ（#2733）
            fix_event = PhaseEvent("実装（修正）", duration_seconds=None, result="-", owner="Claude Code")

        # k 番目レビュー後の「検証・テスト」行: post_pr_preflight_rows から対応するものを探す
        preflight_event: PhaseEvent | None = None
        fix_lower_dt = fix_event.start_at if fix_rec is not None else review_end
        for pf_started, pf_evt in post_pr_preflight_rows:
            if fix_lower_dt is not None and pf_started >= fix_lower_dt and pf_started <= next_review_end:
                preflight_event = pf_evt
                break

        result.append((k, fix_event, preflight_event))

    return result


def _fetch_pr_timestamps_dt(pr_num: str) -> tuple[datetime, datetime] | None:
    """GitHub API から PR の createdAt・mergedAt を datetime ペアで返す（Issue #2639）.

    `gh pr view --json createdAt,mergedAt` を使う。API 失敗・JSON 解析失敗・
    createdAt/mergedAt 欠落（未マージ含む）・日時パース失敗時は None を返す。
    """
    try:
        data = gh_client.pr_view(pr_num, fields=("createdAt", "mergedAt"))
    except GhCommandError:
        return None
    created_at = data.get("createdAt")
    merged_at = data.get("mergedAt")
    if not created_at or not merged_at:
        return None
    created_dt = _parse_dt(created_at)
    merged_dt = _parse_dt(merged_at)
    if created_dt is None or merged_dt is None:
        return None
    return (created_dt, merged_dt)


_VERDICT_RESULT_LABELS = {
    "APPROVE": "成功（APPROVE）",
    "REQUEST_CHANGES": "失敗（REQUEST_CHANGES）",
    "ESCALATED": "エスカレーション（ESCALATE）",
}


def load_review_events(
    pr_num: str,
    *,
    issue_key: str | None = None,
) -> list[PhaseEvent]:
    """統一日誌（ai-review-verdict + step5-airview marks）からレビュー試行ごとの行を組み立てる.

    データソースは統一日誌のみ（#3322: 旧 `ai-reviewer/pr-<N>/timing.json` 参照は撤去済み）。

    - ``ai-review-verdict``（pr-<N>）1 件 = 1 試行行。mark 件数が試行数と一致する場合は
      i 番目の step5-airview-start/end を対応付け、一致しない場合は両端割り当てに縮退する
      （#2779 と同じルール）。
    - verdict レコードが無い場合でも step5-airview-start が記録されていれば
      「計測不可」のプレースホルダー行を返し、行欠落を防ぐ（Issue #2644）。
    - セカンダリレビューは実行記録が存在しないため行を出力しない（Issue #2734）。
    """
    unified_issue = _unified_log_records(issue_key) if issue_key is not None else []
    unified_pr = _unified_log_records(f"pr-{pr_num}")
    verdict_records = _unified_step_records(unified_pr, "ai-review-verdict")

    if verdict_records and all((r.get("meta") or {}).get("started_at") for r in verdict_records):
        unified_events = _build_review_events_from_unified(verdict_records, unified_issue)
        if unified_events is not None:
            return unified_events

    # Issue #2644: verdict 記録なし（parser critical 等）でも airview mark があれば
    # 「計測不可」プレースホルダー行を出力して行欠落を防ぐ。
    airview_starts = _unified_step_records(unified_issue, "step5-airview-start")
    if airview_starts:
        airview_ends = _unified_step_records(unified_issue, "step5-airview-end")
        start_at = _unified_event_dt(airview_starts[0])
        end_at = _unified_event_dt(airview_ends[-1]) if airview_ends else None
        placeholder_duration: float | None = None
        if start_at is not None and end_at is not None:
            placeholder_duration = max((end_at - start_at).total_seconds(), 0.0)
        return [
            PhaseEvent(
                "AIレビュー(プライマリ)",
                duration_seconds=placeholder_duration,
                owner="-",
                start_at=start_at,
                end_at=end_at,
            ),
        ]
    return []


def load_test_plan_phase_event(
    pr_num: str,
    *,
    issue_key: str | None = None,
) -> list[PhaseEvent]:
    """統一日誌から「PRテスト計画検証」フェーズの PhaseEvent を返す（Issue #2773）.

    データソースは統一日誌のみ（#3322: 旧 `ai-review-timing/*.jsonl` 参照は撤去済み）。
    形式ゲート（``test-plan-gate``・pr-<N>）と AI確認（``step5-aiconfirm-start/end``・
    issue-<N>）の両方または一方の計測データを 1 行にまとめて返す。どちらも存在しない
    場合は空リストを返す（行を出力しない）。

    result 欄（備考欄）には内訳を ``ゲート Xs / AI確認 Ys`` 形式で出力する。
    """
    unified_pr = _unified_log_records(f"pr-{pr_num}")
    unified_issue = _unified_log_records(issue_key) if issue_key is not None else []
    gate_records = _unified_step_records(unified_pr, "test-plan-gate")
    aiconfirm_start_records = _unified_step_records(unified_issue, "step5-aiconfirm-start")
    aiconfirm_end_records = _unified_step_records(unified_issue, "step5-aiconfirm-end")
    return _build_test_plan_event_from_unified(gate_records, aiconfirm_start_records, aiconfirm_end_records) or []


def load_yaru_phase_event(pr_num: str) -> list[PhaseEvent]:
    """統一日誌から「Issueやること検証」フェーズの PhaseEvent を返す（Issue #2772）.

    データソースは統一日誌のみ（#3322: 旧 `ai-review-timing/<pr_num>.jsonl` 参照は撤去済み）。
    ``yaru-evidence-tick``（pr-<N>）の ``started_at`` から ``issue-exhaustion-gate`` の
    ``ended_at`` までを 1 行として返す。``yaru-evidence-tick`` が存在しない場合は
    空リストを返す（行を出力しない）。
    """
    unified_pr = _unified_log_records(f"pr-{pr_num}")
    yaru_records = _unified_step_records(unified_pr, "yaru-evidence-tick")
    if not yaru_records:
        return []
    return _build_yaru_event_from_unified(unified_pr, yaru_records[0]) or []


def load_merge_events(
    pr_num: str | None = None,
    *,
    issue_key: str | None = None,
) -> list[PhaseEvent]:
    """CI・マージフェーズの PhaseEvent を返す（Issue #2453・#2761・#2446 後方互換）.

    データソースは統一日誌のみ（#3322: 旧 `ai-review-timing/*.jsonl`・
    `issue-next-timing` jsonl 参照は撤去済み）。

    優先順:
    1. 統一日誌の step6-merge-start ～ step6-merged（issue-<N>）
    2. 統一日誌の auto-merge span（pr-<N>・#2761 APPROVE 自動マージ経路の実測値）
    3. gh pr view --json createdAt,mergedAt リードタイム（#2446 後方互換・注記あり）

    主担当（Issue #2779・#3793）: マージ実行・state 確認とも `gh` CLI 経由（GitHub MCP は
    #3773 で廃止済み）のため `"gh"` のみを表示する。
    """
    unified_events = _merge_events_from_unified(issue_key, pr_num)
    if unified_events is not None:
        return unified_events

    # PR リードタイム（#2446 後方互換・gh API ベース）
    # 起点が取得できないため AI レビュー期間を内包する可能性がある（注記として stderr に出力）
    if pr_num is not None:
        timestamps = _fetch_pr_timestamps_dt(pr_num)
        if timestamps is not None:
            created_at, merged_at = timestamps
            lead_time_seconds = max((merged_at - created_at).total_seconds(), 0.0)
            print(
                "注記: CI・マージ行は PR リードタイム（createdAt〜mergedAt）で算出しています。"
                " AI レビュー期間を内包するため合計が二重計上になる場合があります。",
                file=sys.stderr,
            )
            return [
                PhaseEvent(
                    "CI・マージ",
                    duration_seconds=lead_time_seconds,
                    owner="gh",
                    start_at=created_at,
                    end_at=merged_at,
                )
            ]
    return [PhaseEvent("CI・マージ", duration_seconds=None, owner="gh")]


def load_cleanup_event(
    key: str,
    *,
    _now: datetime | None = None,
) -> PhaseEvent:
    """後処理フェーズの PhaseEvent を返す（Issue #2453・#2633）.

    データソースは統一日誌のみ（#3322: 旧 `issue-next-timing` jsonl 参照は撤去済み）。
    step6-merged（issue-<N>）～ step6-cleanup-done で計測する。

    **フォールバック（Issue #2633）:**
    step6-cleanup-done レコードが存在するが ended_at が null の場合（境界ログ方式の
    最終ステップは後続 mark がないため ended_at が永久に null のまま残る）は、
    report 実行時刻を終端として所要時間を算出する。
    step6-cleanup-done レコード自体が存在しない場合は「計測不可」を返す。

    _now: テスト用にレポート実行時刻を差し替える（デフォルトは datetime.now(UTC)）。
    """
    unified_issue = _unified_log_records(key)
    merged_records = _unified_step_records(unified_issue, "step6-merged")
    cleanup_records = _unified_step_records(unified_issue, "step6-cleanup-done")
    if merged_records and cleanup_records:
        u_start_at = _unified_event_dt(merged_records[-1])
        u_end_at = _unified_event_dt(cleanup_records[-1])
        if u_end_at is None:
            u_end_at = _now if _now is not None else datetime.now(UTC)
        u_duration: float | None = None
        if u_start_at is not None and u_end_at is not None:
            diff = (u_end_at - u_start_at).total_seconds()
            u_duration = diff if diff >= 0 else None
        return PhaseEvent(
            "後処理",
            duration_seconds=u_duration,
            # Issue #2779・#3793: worktree/branch 削除は `git`・`tidd cleanup-merged-branch`、
            # Issue 更新は `gh issue edit`（旧 GitHub MCP は #3773 で廃止済み）が実行主体。
            owner="gh",
            start_at=u_start_at,
            end_at=u_end_at,
        )
    return PhaseEvent(
        "後処理",
        duration_seconds=None,
        # Issue #2779・#3793: worktree/branch 削除は `git`・`tidd cleanup-merged-branch`、
        # Issue 更新は `gh issue edit`（旧 GitHub MCP は #3773 で廃止済み）が実行主体。
        owner="gh",
    )


def _fetch_pr_change_stats(pr_num: str) -> dict[str, object] | None:
    """GitHub API から PR の変更規模（ファイル数・行数・size ラベル）を取得する（Issue #2694）.

    `gh pr view --json additions,deletions,changedFiles,labels` を使う。
    API 失敗・JSON 解析失敗時は None を返す。
    """
    try:
        return gh_client.pr_view(pr_num, fields=("additions", "deletions", "changedFiles", "labels"))
    except GhCommandError:
        return None


def _collect_unified_auto_check(
    pr_num: str | None,
    issue_key: str | None,
) -> tuple[int | None, int | None, bool | None]:
    """統一イベントログから自動チェック用データを収集する（#3127・#3315・#3322）.

    Returns:
        (review_attempts, preflight_count, pytest_executed) のタプル。
        該当レコードが無い要素は None。
    """
    unified_review_attempts: int | None = None
    unified_preflight_count: int | None = None
    unified_pytest_executed: bool | None = None
    if pr_num is not None:
        verdict_records = _unified_step_records(_unified_log_records(f"pr-{pr_num}"), "ai-review-verdict")
        if verdict_records:
            unified_review_attempts = len(verdict_records)
            unified_pytest_executed = bool((verdict_records[-1].get("meta") or {}).get("pytest_executed"))
    if issue_key is not None:
        preflight_ends = _unified_step_records(_unified_log_records(issue_key), "step3-preflight-end")
        complete_ends = [r for r in preflight_ends if (r.get("meta") or {}).get("exit_code") is not None]
        if complete_ends:
            unified_preflight_count = len(complete_ends)
    return unified_review_attempts, unified_preflight_count, unified_pytest_executed


def _fill_pr_stats(result: AutoCheckResult, pr_num: str | None) -> None:
    """PR 変更規模（changed_files・additions・deletions・size_label）を結果へ反映する（#3315・#3322）."""
    if pr_num is None:
        return
    # 取得失敗時は既定値のまま（format_auto_check_section が該当行を出力しない）
    stats = _fetch_pr_change_stats(pr_num)
    if stats is None:
        return
    changed_files = stats.get("changedFiles")
    if isinstance(changed_files, int):
        result.changed_files = changed_files
    additions = stats.get("additions")
    if isinstance(additions, int):
        result.additions = additions
    deletions = stats.get("deletions")
    if isinstance(deletions, int):
        result.deletions = deletions
    labels = stats.get("labels")
    if isinstance(labels, list):
        for label in labels:
            if isinstance(label, dict):
                name = label.get("name")
                if isinstance(name, str) and name.startswith("size/"):
                    result.size_label = name
                    break


def load_auto_check_result(
    pr_num: str | None = None,
    *,
    issue_key: str | None = None,
) -> AutoCheckResult:
    """自動チェック結果セクションのデータを収集する（Issue #2453・#2637）.

    データソースは統一日誌のみ（#3322: 旧 `pre-flight/issue-N.jsonl`・
    `preflight-record.json`・`ai-reviewer/pr-<N>/timing.json` 参照は撤去済み）。

    - review_attempts = pr-<N> の ai-review-verdict イベント数
    - preflight_run_count / preflight_success = issue-<N> の step3-preflight-end（exit_code を持つ完全レコード）
    - pytest_executed（pytest_duplicate 判定用）= 最後の ai-review-verdict の meta
    - review_backend = 最後の ai-review-verdict の meta.backend
    """
    result = AutoCheckResult()
    review_attempts, preflight_count, pytest_executed = _collect_unified_auto_check(pr_num, issue_key)
    if preflight_count is not None:
        result.preflight_run_count = preflight_count
    if review_attempts is not None:
        result.review_attempts = review_attempts

    preflight_success: bool | None = None
    backend = ""
    if issue_key is not None:
        preflight_ends = _unified_step_records(_unified_log_records(issue_key), "step3-preflight-end")
        complete_ends = [r for r in preflight_ends if (r.get("meta") or {}).get("exit_code") is not None]
        if complete_ends:
            last_meta = complete_ends[-1].get("meta") or {}
            exit_code = last_meta.get("exit_code")
            if exit_code == 0:
                preflight_success = True
            elif exit_code is not None:
                preflight_success = False
            result.preflight_success = preflight_success

    if pr_num is not None:
        # pytest 重複検知: pre-flight で pytest が走りかつ ai-review でも pytest が走った（Issue #2636）
        if preflight_success is not None and pytest_executed is True:
            result.pytest_duplicate = True
        # backend
        verdict_records = _unified_step_records(_unified_log_records(f"pr-{pr_num}"), "ai-review-verdict")
        if verdict_records:
            backend = str((verdict_records[-1].get("meta") or {}).get("backend") or "")
        if backend.startswith("codex"):
            result.review_backend = f"codex CLI ({backend.split(':', 1)[-1]})" if ":" in backend else "codex CLI"
        elif backend:
            result.review_backend = backend
        _fill_pr_stats(result, pr_num)

    return result


# ── CLI ────────────────────────────────────────────────────────────────


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "merge-summary",
        help="マージ完了時の所要時間サマリ表を出力する（#2313）",
        description=__doc__,
    )
    add_common_flags(parser)
    sub = parser.add_subparsers(dest="action", required=True)

    p_report = sub.add_parser("report", help="Issue（PR）1 件分の所要時間サマリ表を出力する")
    p_report.add_argument("issue", help="Issue 番号（issue-next-timing のキーに使われた番号）")
    p_report.add_argument(
        "--pr",
        dest="pr",
        required=True,
        help="レビュー行・PRリードタイムを含める場合の PR 番号（Issue #2715: 投稿漏れ防止のため必須）",
    )
    p_report.add_argument(
        "--branch",
        dest="branch",
        default=None,
        help="step3-implementation レコード不在時に実装フェーズを git log から算出するフォールバック用ブランチ名"
        "（省略時は計測不可）",
    )
    p_report.add_argument(
        "--full",
        dest="full",
        action="store_true",
        default=False,
        help="従来どおり Markdown 表全文（## 所要時間サマリ + 自動チェック結果）を stdout に出力する"
        "（省略時は quiet 1〜2 行のサマリのみ・#3432）",
    )

    p_rollup = sub.add_parser("rollup", help="複数 Issue 連続消化時の最終ロールアップ表を出力する")
    p_rollup.add_argument(
        "entries",
        nargs="?",
        default=None,
        help='[["issue番号","合計時間表示"], ...] 形式の JSON 文字列（発生順・後方互換）。--issues と排他',
    )
    p_rollup.add_argument(
        "--issues",
        dest="issues",
        default=None,
        help="Issue 番号のカンマ区切りリスト（例: 42,43,44）。各 Issue の合計時間を統一日誌から"
        "自動算出する。entries と排他",
    )

    p_sweep = sub.add_parser(
        "sweep",
        help="人間マージ経路で投稿が漏れたサマリを事後的に投稿する（#3517）",
    )
    p_sweep.add_argument(
        "--days",
        dest="days",
        type=int,
        default=7,
        help="対象とする直近マージ日数（デフォルト 7）",
    )

    parser.set_defaults(func=run_cli)


def _marker_dir() -> Path:
    """merge-summary-emitted マーカーディレクトリのパスを返す（#2391・#3387）.

    `ISSUE_NEXT_STATE_ROOT` 環境変数があればそれを優先する（テスト用）。
    未設定時は `tidd_tools.shared.paths.cache_dir()` 配下の固定パスを使う（#3387）。
    CWD 依存をやめたことで worktree 間で marker / txt が共有される。
    """
    root_override = os.environ.get("ISSUE_NEXT_STATE_ROOT")
    if root_override:
        return Path(root_override) / "cache" / _MARKER_SUBDIR
    return cache_dir() / _MARKER_SUBDIR


def _extract_issue_num(key: str) -> str | None:
    """key から Issue 番号文字列を抽出する（#2543）.

    ``issue-<N>`` 形式と bare な数字列（``<N>``）の両方を受理する。
    どちらにも一致しない場合は None を返す。

    Examples::

        _extract_issue_num("issue-2527") -> "2527"
        _extract_issue_num("2527")       -> "2527"
        _extract_issue_num("not-valid")  -> None
    """
    issue_num = _match_issue_key(key)
    if issue_num is not None:
        return str(issue_num)
    m = _BARE_NUMBER_RE.match(key)
    if m:
        return m.group(1)
    return None


def _normalize_issue_key(key: str) -> str | None:
    """key を ``issue-<N>`` 形式に正規化する（#2717）.

    ``issue-<N>`` 形式はそのまま返す。bare な数字列（``<N>``）は ``issue-<N>`` に変換する。
    どちらにも一致しない場合は None を返す。

    統一日誌（``timing_log``）のキーは常に ``issue-<N>`` 形式のため、この関数で
    正規化することで、``tidd merge-summary report 2717`` と ``tidd merge-summary report
    issue-2717`` が同じ ``issue-2717`` キーを参照するようになる（#2717・#3322）。

    Examples::

        _normalize_issue_key("issue-2717") -> "issue-2717"
        _normalize_issue_key("2717")       -> "issue-2717"
        _normalize_issue_key("not-valid")  -> None
    """
    issue_num = _extract_issue_num(key)
    if issue_num is None:
        return None
    return f"issue-{issue_num}"


def write_merge_summary_txt(key: str, table_text: str) -> None:
    """生成した所要時間サマリ表テキストをファイルに保存する（#2466 Stop hook 厳密一致用）.

    ``cache/merge-summary-emitted/<N>.txt`` として保存する。
    ``key`` が ``issue-<N>`` 形式または bare な Issue 番号のときに書き込む（#2543）。
    """
    issue_num = _extract_issue_num(key)
    if issue_num is None:
        return
    marker_dir = _marker_dir()
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / f"{issue_num}.txt").write_text(table_text, encoding="utf-8")


def post_pr_comment(pr_num: str, body: str) -> bool:
    """PR にコメントを投稿し、成功したかを返す（#2466）.

    ``gh pr comment <pr_num> --body <body>`` を実行する。
    失敗（returncode != 0）の場合は stderr に理由を出力して False を返す。
    """
    import sys

    try:
        proc = subprocess.run(
            ["gh", "pr", "comment", pr_num, "--body", body],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"merge-summary: PR コメント投稿失敗（{exc}）\n")
        return False
    if proc.returncode != 0:
        sys.stderr.write(
            f"merge-summary: PR #{pr_num} へのコメント投稿が失敗しました"
            f"（exit {proc.returncode}）: {proc.stderr.strip()}\n"
        )
        return False
    return True


def find_summary_comment_id(pr_num: str) -> str | None:
    """PR の `## 所要時間サマリ` コメントがあればそのコメント ID を返す（Issue #2790・#3556・#3571）.

    ``summary_comment_exists`` を拡張し、bool ではなくサマリコメントの GitHub
    コメント ID（文字列）を返す。コメントが無い場合は None を返す。
    ID は `cleanup_merged_branch` が後処理完了後に既存コメントを PATCH 上書きする
    ときに使う（#3556）。

    **データソースは REST（`gh api repos/{owner}/{repo}/issues/<PR>/comments`）を使う（#3571）。**
    `gh pr view --json comments` の `id` は GraphQL の node_id（`IC_...` 形式）を返すため、
    `_patch_pr_comment` の PATCH エンドポイント（REST 数値 ID 要求）と形式が食い違って
    HTTP 404 になる。REST エンドポイントの `id` は数値 ID を返し、PATCH と整合する。

    marker ファイルは CWD 相対（``_marker_dir()``）のため、ai-review（worktree）と
    ``/issue-next`` STEP6（本体リポジトリ）では別ディレクトリを見ることになる。
    二重投稿を実際に防ぐには PR 側の実体を確認する必要がある。

    gh の失敗・タイムアウト・JSON 解析失敗時は None（fail-open）を返し、
    投稿そのものは止めない（未投稿のまま終わるより二重投稿の方が害が小さい）。
    リポジトリ名が解決できない場合も警告なしで None を返す（#3583）。
    """
    repo = gh_client.repo_name_with_owner()
    if not repo:
        # Issue #3583: リポジトリ名を解決できない場合は警告を出さず fail-open で None を返す
        # （#3581 で追加した stderr 警告が test_issue_3386.py の stderr 空アサーションを壊した）。
        return None
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{pr_num}/comments"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"merge-summary: PR #{pr_num} のコメント取得に失敗しました（{exc}）\n")
        return None
    if proc.returncode != 0:
        sys.stderr.write(f"merge-summary: PR #{pr_num} のコメント取得に失敗しました（exit {proc.returncode}）\n")
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    for c in payload:
        if _SUMMARY_HEADER in (c.get("body") or ""):
            comment_id = c.get("id")
            if comment_id is not None:
                return str(comment_id)
    return None


def _patch_pr_comment(comment_id: str, body: str) -> bool:
    """既存の PR コメントを PATCH で上書き更新する（#3556）.

    ``gh api --method PATCH /repos/<owner>/<repo>/issues/comments/<id> -F body=@-``
    で body を stdin から渡す。失敗時は stderr に理由を出力して False を返す
    （呼び出し元の exit code は変えない）。リポジトリ名を解決できない場合は
    警告を出さず False を返す（#3583）。

    Issue #3704: ``-f``（form field）ではなく ``-F``（raw field）を使う。gh 2.94.0 では
    ``gh api -f body=@-`` が stdin を読まずリテラル ``@-`` を投稿するため、PATCH が
    サマリコメントを ``@-`` で上書きしてしまう（実害: PR #3699 / #3701 / #3703）。
    ``-F`` は stdin を正しく読む。
    """
    repo = gh_client.repo_name_with_owner()
    if not repo:
        # Issue #3583: リポジトリ名を解決できない場合は stderr に警告を出さず False を返す
        # （#3581 で追加した stderr 警告が test_issue_3386.py の stderr 空アサーションを壊した）。
        return False
    try:
        proc = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/issues/comments/{comment_id}",
                "-F",
                "body=@-",
            ],
            input=body,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"merge-summary: サマリコメント更新失敗（{exc}）\n")
        return False
    if proc.returncode != 0:
        sys.stderr.write(
            f"merge-summary: サマリコメント（id={comment_id}）の更新が失敗しました"
            f"（exit {proc.returncode}）: {proc.stderr.strip()}\n"
        )
        return False
    return True


def refresh_summary_comment(pr_num: str) -> bool:
    """既存サマリコメントを再生成して上書き更新する（#3556）.

    auto-merge のサマリ投稿後、`cleanup_merged_branch` が `step6-cleanup-done` を
    自己記録した直後に再生成・更新される。
    既存サマリコメントが無い場合は何もしない（新規投稿はしない）。PR 本文の
    ``closes #N`` から Issue を解決できない場合も何もしない。失敗時は False を
    返すが例外は投げない（呼び出し元の exit code を変えない）。
    """
    comment_id = find_summary_comment_id(pr_num)
    if comment_id is None:
        return False
    closes = extract_closes_issues(gh_client.pr_body(pr_num))
    if not closes:
        return False
    issue_key = f"issue-{closes[0]}"
    latest_attempt_events = filter_latest_attempt(timing_log.read_events(issue_key))
    if not latest_attempt_events:
        return False
    _, summary_table = _generate_summary_table(issue_key, pr_num, latest_attempt_events)
    return _patch_pr_comment(comment_id, summary_table)


@contextlib.contextmanager
def _temporary_env(overrides: dict[str, str]) -> Iterator[None]:
    """空でない値の環境変数を一時的に設定し、抜けるときに元へ戻す."""
    applied = {k: v for k, v in overrides.items() if v}
    previous = {k: os.environ.get(k) for k in applied}
    os.environ.update(applied)
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def post_merge_summary_from_pr_body(pr_num: str, pr_body: str, token: str = "", repo: str = "") -> None:
    """auto-merge 成功後に所要時間サマリを PR へ投稿する（Issue #2790）.

    ``_finalize_approve`` の auto-merge 成功パスから呼ばれる。
    ``/issue-next`` 経由・直接実行どちらの場合もサマリが投稿されることを保証する。

    設計上の約束:
    - closes #N が PR ボディにない場合はスキップして stderr に警告を出力する
    - marker ファイル、または PR の投稿済みコメントがある場合は二重投稿しない
    - 投稿失敗・例外発生いずれの場合も呼び出し元の exit code を変えない（例外を呑む）

    Args:
        pr_num: PR 番号
        pr_body: PR ボディ文字列（closes #N を含むことが期待される）
        token: gh サブプロセスへ渡す GH_TOKEN。auto-merge が GitHub App token のみで
            成功した経路では ambient な gh 認証がないため呼び出し元から引き継ぐ。
        repo: gh サブプロセスへ渡す GH_REPO（``owner/name``）。auto-merge は --repo で
            対象を解決するため、CWD 側のリポジトリへ誤投稿しないよう引き継ぐ。
    """
    import sys

    try:
        _post_merge_summary_impl(pr_num, pr_body, token=token, repo=repo)
    except Exception as exc:  # noqa: BLE001 — サマリ投稿の失敗は auto-merge の結果を覆さない
        issue_num = ""
        try:
            closes = extract_closes_issues(pr_body)
            if closes:
                issue_num = str(closes[0])
        except Exception:  # noqa: BLE001 — 例外内容の補足失敗は無視してよい
            pass
        sys.stderr.write(
            f"merge-summary: PR #{pr_num} サマリ投稿中に例外が発生しました（{exc}）。"
            " auto-merge の結果には影響しません。\n"
        )
        # Issue #3387: stderr は auto-merge 成功時には誰にも読まれず消えるため、
        # loop-error に永続化して原因追跡を可能にする。
        loop_error_log.record(
            "merge-summary:post-failed",
            f"PR #{pr_num} (Issue #{issue_num}) サマリ投稿中に例外が発生しました（{exc}）。",
            pr_number=pr_num,
        )


def _post_merge_summary_impl(pr_num: str, pr_body: str, token: str = "", repo: str = "") -> None:
    """post_merge_summary_from_pr_body の内部実装（例外伝播あり）."""
    import argparse
    import sys

    closes = extract_closes_issues(pr_body)
    if not closes:
        sys.stderr.write(
            f"merge-summary: PR #{pr_num} に closes #N が見つからないため サマリ投稿をスキップします（skip）。\n"
        )
        # Issue #3387: 投稿漏れの原因を loop-error に永続化する（投稿済みスキップは正常系のため記録しない）
        loop_error_log.record(
            "merge-summary:no-closes",
            f"PR #{pr_num} に closes #N が見つからないため サマリ投稿をスキップしました。",
            pr_number=pr_num,
        )
        return

    issue_num = str(closes[0])
    issue_key = f"issue-{issue_num}"

    with _temporary_env({"GH_TOKEN": token, "GH_REPO": repo}):
        # 投稿済み判定は .txt を正とし、移行期の既存 .marker も読み取り互換として受理する（#3387）。
        # #3995: PR コメントが既にある（が marker が無い＝park→clear で marker だけ消えた）
        # ケースはここで早期 return せず run_cli へ進める。run_cli 内の `_post_report` が
        # 同じコメント存在チェックで再投稿をスキップしつつ marker を書き直すため、
        # marker が永久に再作成されないデッドロックを防げる。
        marker_dir = _marker_dir()
        if (marker_dir / f"{issue_num}.txt").is_file() or (marker_dir / f"{issue_num}.marker").is_file():
            sys.stderr.write(
                f"merge-summary: PR #{pr_num} (Issue #{issue_num}) は既に投稿済みのためスキップします（skip）。\n"
            )
            return

        # run_cli を argparse.Namespace 経由で呼び出す
        args = argparse.Namespace(
            action="report",
            issue=issue_key,
            pr=pr_num,
            branch=None,
            dry_run=False,
            auto_merge=True,
        )
        result = run_cli(args)
        if result != 0:
            sys.stderr.write(
                f"merge-summary: PR #{pr_num} サマリ投稿が失敗しました（exit {result}）。"
                " auto-merge の結果には影響しません。\n"
            )
            # Issue #3387: 失敗を loop-error に永続化して原因追跡を可能にする
            loop_error_log.record(
                "merge-summary:report-failed",
                f"PR #{pr_num} (Issue #{issue_num}) サマリ投稿が失敗しました（exit {result}）。",
                pr_number=pr_num,
            )


def _apply_pr_created_fallback(events: list[PhaseEvent], pr_created_at: datetime) -> None:
    """「プルリクエスト」行を GitHub API 由来の createdAt で補完する（Issue #3516）.

    ``build_events_from_log`` は ``step4-pr-created`` の実測が欠落していると
    「プルリクエスト」行を ``end_at=None``（計測不可）のまま返す。開始時刻
    （PR 作成前の最後の ``step3-preflight-end``）が取得できている場合のみ、
    API から補完した createdAt を終了時刻として反映し所要時間を計算し直す。
    開始時刻が欠落している・API の createdAt が開始時刻より前（異常値）の場合は
    何もしない（従来どおり「計測不可」のまま）。
    """
    for event in events:
        if event.name != "プルリクエスト":
            continue
        if event.end_at is not None or event.start_at is None:
            return
        if pr_created_at < event.start_at:
            return
        event.end_at = pr_created_at
        event.duration_seconds = (pr_created_at - event.start_at).total_seconds()
        return


def _build_report_events(
    issue_key: str,
    pr_num: str,
    *,
    latest_attempt_events: list[dict[str, Any]],
) -> tuple[list[PhaseEvent], datetime | None, list[tuple[datetime, PhaseEvent]], int]:
    """Phase 1〜5（プルリクエストまで）と検証・テストのラウンド番号を構築する（#3315・#3322）."""
    # 9 フェーズ構造（Issue #2453）: Phase 1〜5（プルリクエストまで）を
    # 統一イベントログから純関数で構築する（Issue #2935 やること1・6項目目・#3322）。
    events: list[PhaseEvent] = build_events_from_log(latest_attempt_events)

    # Phase 4 の PR 作成前後区分: step4-pr-created mark から PR 作成時刻を取得する（#2733）
    pr_created_event = _first_log_event(latest_attempt_events, "step4-pr-created")
    # #3552: event timestamp（hook 発火時刻）より meta.created_at（PR の実 createdAt）を優先する
    pr_created_boundary: datetime | None = _pr_created_dt(pr_created_event)
    if pr_created_boundary is None and pr_num:
        # Issue #3516: step4-pr-created mark 欠落（--body-file 経由の hook 断絶等）時は
        # GitHub API の createdAt にフォールバックする。
        api_timestamps = _fetch_pr_timestamps_dt(pr_num)
        if api_timestamps is not None:
            pr_created_boundary = api_timestamps[0]
            _apply_pr_created_fallback(events, pr_created_boundary)
    # #2759: PR 作成後の pre-flight 完全レコードを先に取得し、全ラウンド数を算出する
    post_pr_pf_rows: list[tuple[datetime, PhaseEvent]] = (
        _preflight_rows_from_log(latest_attempt_events, pr_created_at=pr_created_boundary)
        if pr_created_boundary is not None
        else []
    )
    # #2759: PR 作成後のレコードがある場合は全ラウンド数（PR 作成前 + PR 作成後）で連番を付け直す
    n_post = len(post_pr_pf_rows)
    # #2830: 「検証・テスト」以外（「実装（修正）」等）は周回数に含めない。
    preflight_rounds = [e for e in events if e.name.startswith("検証・テスト")]
    n_pre = len(preflight_rounds)
    if n_post > 0:
        total_rounds = n_pre + n_post
        for i, evt in enumerate(preflight_rounds):
            # 並行実行フラグは元ラベルに含まれている場合があるため、ベースラベルを保持しつつ連番を付け直す
            evt.name = f"検証・テスト ({i + 1}/{total_rounds})"
    return events, pr_created_boundary, post_pr_pf_rows, n_pre


def _append_review_events(
    events: list[PhaseEvent],
    *,
    pr_num: str,
    issue_key: str,
    pr_created_boundary: datetime | None,
    post_pr_pf_rows: list[tuple[datetime, PhaseEvent]],
    n_pre: int,
    latest_attempt_events: list[dict[str, Any]],
) -> None:
    """Phase 6 & 7（AIレビュー・リトライ時の修正行）を events へ追記する（#3315・#3322）."""
    review_events = load_review_events(pr_num, issue_key=issue_key)
    if pr_created_boundary is not None and len(review_events) > 1:
        # リトライループ: 各レビュー試行後に「実装（修正）」・「検証・テスト」行を挿入（Issue #2733）
        # 各レビュー試行後の「実装（修正）」・「検証・テスト」行を統一日誌から生成（#3322）
        verdict_records = _unified_step_records(_unified_log_records(f"pr-{pr_num}"), "ai-review-verdict")
        fix_entries = _build_fix_rounds_from_log(latest_attempt_events, verdict_records, post_pr_pf_rows)
        fix_map: dict[int, tuple[PhaseEvent, PhaseEvent | None]] = {
            k: (fix_evt, pf_evt) for k, fix_evt, pf_evt in fix_entries
        }
        for i, rev_evt in enumerate(review_events):
            events.append(rev_evt)
            if i in fix_map:
                fix_evt, pf_evt = fix_map[i]
                events.append(fix_evt)
                if pf_evt is not None:
                    events.append(pf_evt)
    else:
        # 通常ケース（レビュー 1 回、または pr_created_boundary 未記録）
        events += review_events
        # #2759: PR 作成後の pre-flight レコードをレビュー行の直後に時系列順で挿入する
        if post_pr_pf_rows:
            total_rounds = n_pre + len(post_pr_pf_rows)
            for j, (_pf_started, pf_evt) in enumerate(post_pr_pf_rows):
                pf_evt.name = f"検証・テスト ({n_pre + j + 1}/{total_rounds})"
                events.append(pf_evt)


def _clamp_merge_events_after_children(
    merge_events: list[PhaseEvent],
    prior_events: list[PhaseEvent],
) -> list[PhaseEvent]:
    """CI・マージの start_at が既存フェーズの最大 end_at より前にならないよう補正する（Issue #4157）.

    `step6-merge-start`/`step6-merged` が統一日誌に記録されていない場合の PR
    リードタイムフォールバック（PR 作成〜マージ完了の全区間・``load_merge_events``）は、
    実際にはその内部で行われる PRテスト計画検証・AIレビュー・Issueやること検証より
    start_at が早くなり、表の行順が「CI・マージが先に完了し、その後レビューが始まった」
    ように誤解を招く（直近30件のマージ済みPR調査で該当8件中7件がこのパターン）。

    子フェーズの最大 end_at より start_at が早い場合は start_at をその end_at に
    切り上げる（表示用の補正であり実測値の書き換えではない）。end_at も同じ値以前
    になってしまう場合は実測不能とみなし「計測不可」にする。

    呼び出し元は `step6-merge-start`/`step6-merged` の正規記録が無くフォールバック
    値（PR リードタイム等）を使った場合にのみ本関数を適用する。正規記録がある
    場合、CI・マージが AIレビュー等より実際に先行するケース（PR #3373 実観測
    パターン・`test_issue_3389.py` Scenario 1）が存在するため、常時適用しては
    ならない。
    """
    end_candidates = [e.end_at for e in prior_events if e.end_at is not None]
    if not end_candidates:
        return merge_events
    floor = max(end_candidates)
    clamped: list[PhaseEvent] = []
    for event in merge_events:
        if event.start_at is None or event.start_at >= floor:
            clamped.append(event)
            continue
        if event.end_at is not None and event.end_at > floor:
            clamped.append(
                PhaseEvent(
                    event.name,
                    duration_seconds=(event.end_at - floor).total_seconds(),
                    result=event.result,
                    owner=event.owner,
                    start_at=floor,
                    end_at=event.end_at,
                )
            )
        else:
            clamped.append(PhaseEvent(event.name, duration_seconds=None, result=event.result, owner=event.owner))
    return clamped


def _append_tail_events(
    events: list[PhaseEvent],
    *,
    pr_num: str,
    issue_key: str,
) -> None:
    """Phase 7.2〜9（PR テスト計画・やること検証・CI マージ・後処理）を追記する（#3315）.

    PR が存在するにもかかわらず該当 step の記録が 1 件もない場合でも、行自体を
    欠落させず「計測不可」のプレースホルダー行を出力する（Issue #3620 Scenario 3）。
    """
    # Phase 7.2: PRテスト計画検証（Issue #2773）
    if pr_num:
        events.extend(
            load_test_plan_phase_event(pr_num, issue_key=issue_key) or [PhaseEvent("PRテスト計画検証")],
        )
    # Phase 7.5: Issueやること検証（Issue #2772）
    if pr_num:
        events.extend(load_yaru_phase_event(pr_num) or [PhaseEvent("Issueやること検証")])
    # Phase 8: CI・マージ
    unified_merge_events = _merge_events_from_unified(issue_key, pr_num)
    if unified_merge_events is not None:
        # step6-merge-start/merged の正規記録がある場合はそのまま使う（実測順を尊重・#3389）
        events.extend(unified_merge_events)
    else:
        # Issue #4157: フォールバック値（PR リードタイム等）は実際のマージ実行区間より
        # 広い区間になり、子フェーズより前に来て順序が崩れるため補正する
        events.extend(_clamp_merge_events_after_children(load_merge_events(pr_num, issue_key=issue_key), events))
    # Phase 9: 後処理
    events.append(load_cleanup_event(issue_key))


def _post_report(
    args: argparse.Namespace,
    issue_key: str,
    pr_num: str,
    summary_table: str,
    *,
    events: list[PhaseEvent],
) -> int:
    """サマリ表の出力・txt 保存・PR コメント投稿・marker 書き込みを行う（#3315）.

    `--full` 指定時は従来どおり Markdown 表全文（+ 自動チェック結果）を stdout に出力する。
    省略時（デフォルト・#3432）は quiet 1〜2 行のサマリのみを stdout に出力し、
    PR コメント投稿・txt 保存は常に表全文で行う（副作用は変わらない）。
    `_post_merge_summary_impl`（auto-merge 経路）は Namespace に `full` を持たないため
    `getattr` で解決し、quiet デフォルトにフォールバックする。

    **#3995:** marker（txt）が表すのは「この Issue のサマリ報告は完了している」という
    事実であり、新規投稿したか既存投稿を再利用（スキップ）したかは関係ない。
    「投稿済みスキップ」経路で marker を書かないと、park → `issue-next-state clear`
    （marker 削除）→ 再開 → 再マージ → report（既存コメントありでスキップ）という経路で
    marker が永久に再作成されず `clear` がデッドロックする。そのためスキップ時も
    通常投稿時と同様に marker を書く。
    """
    if getattr(args, "full", False):
        print(summary_table, end="")
        auto_check = load_auto_check_result(pr_num, issue_key=issue_key)
        print(format_auto_check_section(auto_check), end="")
    else:
        print(format_quiet_summary(events, issue_key, pr_num), end="")

    # --dry-run 指定時は PR コメント投稿・marker 書き込み・txt 書き出しをスキップする（Issue #2791）
    if getattr(args, "dry_run", False):
        return 0

    if pr_num:
        # 既に PR へ投稿済みなら再投稿しない（#2790）
        if find_summary_comment_id(pr_num) is not None:
            # #3995: 投稿済みスキップ経路でも marker を書く（「報告完了」の意味は新規投稿と同じ）
            write_merge_summary_txt(issue_key, summary_table)
            print(
                f"merge-summary: PR #{pr_num} には既にサマリが投稿済みのため再投稿しません（skip）。",
                file=sys.stderr,
            )
            return 0
        posted = post_pr_comment(pr_num, summary_table)
        if posted:
            # 投稿成功をもって「投稿済み」とみなす（#3387: .txt を正とする）
            write_merge_summary_txt(issue_key, summary_table)
        # 投稿失敗時は txt を書かない（#2466: issue-next-state clear を継続ブロック）
    else:
        write_merge_summary_txt(issue_key, summary_table)
    return 0


def _run_report_cli(args: argparse.Namespace) -> int:
    """`merge-summary report` を実行する（#3315 で run_cli から抽出・#3322 で統一日誌専用化）."""
    # #2717: bare な Issue 番号（例: "2717"）を issue-<N> 形式に正規化する。
    # 統一日誌（timing_log）のキーは常に `issue-<N>` 形式のため、
    # 正規化しないと bare 番号指定時に存在しないキーを参照する。
    raw_issue_key = args.issue
    normalized = _normalize_issue_key(raw_issue_key)
    issue_key = normalized if normalized is not None else raw_issue_key
    pr_num = args.pr

    # データソースは統一日誌のみ（#3322: 旧 5 系統フォールバックは撤去済み）。
    _log_records = timing_log.read_events(issue_key)
    _latest_attempt_events = filter_latest_attempt(_log_records)
    if not _latest_attempt_events:
        print(
            "ERROR: merge-summary: "
            f"Issue {issue_key} の統一イベントログに記録がありません。"
            "旧 5 系統ファイルへのフォールバックは撤去済みのため、"
            "過去 Issue は旧 JSONL を統一日誌へ手動 import（lazy migration）してから再実行してください。",
            file=sys.stderr,
        )
        return 1

    # レポート生成前の完全性チェック（やること3項目目・#2902、fail-open化・#3517）。
    # 欠落があっても表の生成・投稿は止めない（マージ済み PR にサマリが 1 件も
    # 残らない方が、欠落フェーズが「計測不可」表示になることより実害が大きいため）。
    # 欠落は loop-error に記録し、既存の異常検知（detect_anomalous_phases）と同じ
    # 書式で stderr に警告を出すに留める。
    required_steps = ("step6-merged",) if getattr(args, "auto_merge", False) else REQUIRED_REPORT_STEPS
    missing_steps = check_report_completeness(_latest_attempt_events, required_steps)
    if missing_steps:
        missing_str = ", ".join(missing_steps)
        loop_error_log.record(
            "merge-summary:incomplete-marks",
            f"Issue {issue_key} の必須 step が欠落しています（{missing_str}）。",
            pr_number=pr_num or "",
        )
        print(
            f"WARNING: merge-summary: Issue {issue_key} の記録が未完了です"
            f"（欠落 step: {missing_str}）。該当フェーズは「計測不可」として表を生成します。",
            file=sys.stderr,
        )

    events, summary_table = _generate_summary_table(issue_key, pr_num, _latest_attempt_events)

    # 異常な計測値（終了<開始・AIレビュー計測欠落）を検知したら stderr へ警告を出す
    # （Issue #3386）。auto-merge フローを止めないため exit code は変えない。
    for phase_name, marker in detect_anomalous_phases(events):
        print(
            f"WARNING: merge-summary: フェーズ「{phase_name}」の所要時間が異常です（{marker}）。"
            "統一日誌のタイムスタンプを確認してください。",
            file=sys.stderr,
        )

    return _post_report(args, issue_key, pr_num, summary_table, events=events)


def _generate_summary_table(
    issue_key: str,
    pr_num: str,
    latest_attempt_events: list[dict[str, Any]],
) -> tuple[list[PhaseEvent], str]:
    """report と同じ手順でサマリ表を生成する（投稿副作用なし・#3556）.

    `_run_report_cli`（投稿あり）と `refresh_summary_comment`（既存コメント上書き）
    の両方から使う共通の表生成ロジック。

    Returns:
        (events, summary_table) のタプル。events は異常検知など後段の処理に使う。
    """
    events, pr_created_boundary, post_pr_pf_rows, n_pre = _build_report_events(
        issue_key,
        pr_num,
        latest_attempt_events=latest_attempt_events,
    )
    if pr_num:
        _append_review_events(
            events,
            pr_num=pr_num,
            issue_key=issue_key,
            pr_created_boundary=pr_created_boundary,
            post_pr_pf_rows=post_pr_pf_rows,
            n_pre=n_pre,
            latest_attempt_events=latest_attempt_events,
        )
    _append_tail_events(events, pr_num=pr_num, issue_key=issue_key)

    # Phase 6 以降を連結して初めて全体の時系列が確定するため、ここで最終ソートする
    # （Issue #3389: 実測順と食い違う行順の解消）。
    events = _sort_events_by_start_at(events)
    return events, format_summary_table(events)


def _compute_issue_total_display(issue_key: str) -> str | None:
    """統一日誌の最新 attempt から Issue の合計時間表示を算出する（#3433）.

    report と同じ既存ロジック（``filter_latest_attempt`` → ``build_events_from_log`` →
    ``total_duration_seconds``）を再利用する。過去 attempt のイベントは最新 attempt のみに
    絞り込んで除外する。記録が欠落して合計を算出できない場合は None を返す
    （呼び出し元が「計測不可」にする）。
    """
    records = filter_latest_attempt(timing_log.read_events(issue_key))
    if not records:
        return None
    events = build_events_from_log(records)
    events.extend(load_merge_events(None, issue_key=issue_key))
    events.append(load_cleanup_event(issue_key))
    return _format_duration(total_duration_seconds(events))


def _rollup_entries_from_issues(issues: str) -> list[tuple[str, str]]:
    """``--issues`` のカンマ区切り番号から (issue番号, 合計時間表示) エントリ列を組み立てる（#3433）.

    各 Issue の合計時間は統一日誌（``issue-<N>.jsonl``）から自動算出する。
    記録が欠落して合計を算出できない Issue は行を「計測不可」として表に含め、
    stderr に警告 1 行を出力する（バッチ全体の報告を止めない・exit code は 0 のまま）。
    """
    entries: list[tuple[str, str]] = []
    for token in issues.split(","):
        issue_num = _extract_issue_num(token.strip())
        if issue_num is None:
            print(
                f"WARNING: merge-summary: 指定された {token.strip()!r} は Issue 番号として解釈できないため"
                "スキップします。",
                file=sys.stderr,
            )
            continue
        total_display = _compute_issue_total_display(f"issue-{issue_num}")
        if total_display is None:
            print(
                f"WARNING: merge-summary: Issue #{issue_num} の記録が欠落しているため計測不可です。",
                file=sys.stderr,
            )
            total_display = "計測不可"
        entries.append((f"#{issue_num}", total_display))
    return entries


def _resolve_rollup_entries(args: argparse.Namespace) -> list[tuple[str, str]] | None:
    """rollup のエントリ列を JSON 引数 / ``--issues`` のどちらかから解決する（#3433）.

    - JSON 引数（後方互換）が指定された場合は従来どおりそれをそのまま使う
    - ``--issues`` が指定された場合は各 Issue の合計時間を統一日誌から自動算出する
    - 両方指定・両方未指定はエラー（stderr にメッセージを出して None を返し、呼び出し元が exit 1）
    """
    entries_json = getattr(args, "entries", None)
    issues = getattr(args, "issues", None)
    if entries_json is not None and issues is not None:
        print("ERROR: merge-summary: rollup の JSON 引数と --issues は同時に指定できません", file=sys.stderr)
        return None
    if entries_json is not None:
        try:
            return [(str(a), str(b)) for a, b in json.loads(entries_json)]
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            print(f"ERROR: merge-summary: rollup の JSON 引数を解析できません（{exc}）", file=sys.stderr)
            return None
    if issues is not None:
        return _rollup_entries_from_issues(issues)
    print("ERROR: merge-summary: rollup には JSON 引数または --issues のいずれかが必要です", file=sys.stderr)
    return None


def _run_rollup_cli(args: argparse.Namespace) -> int:
    """`merge-summary rollup` を実行する（#3315 で run_cli から抽出・#3433 で --issues 対応）."""
    entries = _resolve_rollup_entries(args)
    if entries is None:
        return 1
    print(format_rollup_table(entries), end="")
    return 0


def _run_sweep_cli(args: argparse.Namespace) -> int:
    """`merge-summary sweep` を実行する（人間マージ経路の投稿漏れ補完・#3517）.

    直近 ``--days`` 日以内にマージされた PR を列挙し、``find_summary_comment_id``
    が None（未投稿）のものだけ ``closes #N`` から Issue を解決して report 相当を
    実行する。1 件の PR で例外が出ても残りの PR の処理を継続し、コマンド全体は
    exit code 0 で終わらせる（個々の失敗は stderr 警告に留める）。
    """
    days = getattr(args, "days", 7) or 7
    cutoff = datetime.now(UTC) - timedelta(days=days)

    try:
        prs = gh_client.pr_list(state="merged", limit=50, fields=("number", "mergedAt", "body"))
    except GhCommandError as exc:
        print(
            f"WARNING: merge-summary: sweep 対象 PR 一覧の取得に失敗しました（{exc}）。",
            file=sys.stderr,
        )
        return 0

    for pr in prs:
        pr_num = str(pr.get("number") or "")
        if not pr_num:
            continue
        try:
            merged_at = _parse_dt(pr.get("mergedAt"))
            if merged_at is None or merged_at < cutoff:
                continue
            closes = extract_closes_issues(pr.get("body") or "")
            if not closes:
                continue
            issue_key = f"issue-{closes[0]}"
            if find_summary_comment_id(pr_num) is not None:
                continue
            report_args = argparse.Namespace(action="report", issue=issue_key, pr=pr_num, branch=None, dry_run=False)
            result = _run_report_cli(report_args)
            if result != 0:
                print(
                    f"WARNING: merge-summary: sweep: PR #{pr_num} の report 実行が失敗しました（exit {result}）。",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001 — 1 件の失敗で sweep 全体を止めない
            print(
                f"WARNING: merge-summary: sweep: PR #{pr_num} の処理中に例外が発生しました（{exc}）。",
                file=sys.stderr,
            )
            continue

    return 0


def run_cli(args: argparse.Namespace) -> int:
    if args.action == "report":
        return _run_report_cli(args)
    if args.action == "rollup":
        return _run_rollup_cli(args)
    if args.action == "sweep":
        return _run_sweep_cli(args)
    return 1
