"""`tidd ai-review-timing --report` サブコマンド（Issue #2102・#2326）.

``~/.cache/ai-review-timing/*.jsonl``（Issue #2100・#2101 が記録する ai-review /
pre-flight ステップ別 timing）を全件集計し、ステップ別の median / p90（秒）を
標準出力に表示する。#2071（親 Issue）の「10 PR 以上の実測から支配ステップを
特定する」ための集計コマンド。

**mark 漏れ検出（Issue #2326）:** 計測境界は hook / ツール自己記録（record-timing-boundaries /
pre-flight / ai-review 等）が開いたレコードを、次の境界レコードで close する方式のため、
正常な運用では各 jsonl ファイルにつき最大 1 件（現在進行中のステップ）だけが
`ended_at: null` のまま残る。自己記録が欠落した場合（セッション `/clear` 等）、その open
レコードは永久に close されず「実装フェーズ全体の所要時間が算出できない」という
#2314 で実際に観測された事象につながる。`_find_stale_open_boundaries()` は
「一定時間（既定 1 時間）以上 open のまま」の境界を mark 漏れ疑いとして検出し、
`--report` の出力末尾に警告表示する。#3559 で `tidd issue-next-timing mark` サブコマンドを
廃止したため、LLM が手打ちで mark を打つ運用は存在しない。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

# ── データ収集 ────────────────────────────────────────────────────────────


def _timing_dir() -> Path:
    home = Path(os.environ.get("HOME", str(Path.home())))
    return home / ".cache" / "ai-review-timing"


def _find_jsonl_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.jsonl"))


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _load_step_durations(files: list[Path]) -> dict[str, list[float]]:
    """jsonl 群からステップ名 → 所要秒数リストを組み立てる.

    壊れた行（不正 JSON・必須キー欠落・日時パース失敗）は無視する（best-effort）。
    """
    durations: dict[str, list[float]] = {}
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            step = record.get("step")
            started_at = _parse_dt(record.get("started_at", ""))
            ended_at = _parse_dt(record.get("ended_at", ""))
            if not step or started_at is None or ended_at is None:
                continue
            duration = (ended_at - started_at).total_seconds()
            durations.setdefault(step, []).append(duration)
    return durations


def _load_step_durations_from_events(events: list[dict[str, Any]]) -> dict[str, list[float]]:
    """統一イベントログ（timing_log）の span レコードからステップ別秒数を組み立てる（#3295）.

    `measure_step()` の二重書き込み（kind="span"・source="ai-review-timing"）が
    meta.started_at / meta.ended_at を持つ。不正レコードは無視する（best-effort）。
    """
    durations: dict[str, list[float]] = {}
    for event in events:
        if event.get("kind") != "span" or event.get("source") != "ai-review-timing":
            continue
        step = event.get("step")
        meta = event.get("meta") or {}
        started_at = _parse_dt(str(meta.get("started_at", "")))
        ended_at = _parse_dt(str(meta.get("ended_at", "")))
        if not step or started_at is None or ended_at is None:
            continue
        duration = (ended_at - started_at).total_seconds()
        durations.setdefault(str(step), []).append(duration)
    return durations


# ── 集計 ────────────────────────────────────────────────────────────────


def _percentile(values: list[float], p: float) -> float:
    """p パーセンタイルを線形補間なしで返す（numpy 非依存）."""
    sorted_v = sorted(values)
    idx = (len(sorted_v) - 1) * p / 100.0
    lo = int(idx)
    return sorted_v[min(lo + 1, len(sorted_v) - 1)] if idx != lo else sorted_v[lo]


def _aggregate(step_durations: dict[str, list[float]]) -> dict[str, dict[str, Any]]:
    """ステップ別に件数・median・p90 を集計し median 降順で返す（支配ステップを先頭に）."""
    result: dict[str, dict[str, Any]] = {}
    for step, values in step_durations.items():
        result[step] = {
            "count": len(values),
            "median": round(median(values), 2),
            "p90": round(_percentile(values, 90), 2),
        }
    return dict(sorted(result.items(), key=lambda item: item[1]["median"], reverse=True))


# ── mark 漏れ検出（Issue #2326） ─────────────────────────────────────────

_STALE_OPEN_BOUNDARY_THRESHOLD_SECONDS = 3600  # 1時間: 現在進行中のステップは除外する


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _find_stale_open_boundaries(
    files: list[Path],
    *,
    now: datetime | None = None,
    stale_after_seconds: float = _STALE_OPEN_BOUNDARY_THRESHOLD_SECONDS,
) -> list[dict[str, Any]]:
    """各ファイルの最終行が `ended_at: null` のまま長時間経過している境界を検出する.

    `mark_boundary()`（#2312）は新規境界を開く際に直前の open 中レコードを close
    するため、正常運用では各ファイルにつき最大 1 件（現在進行中のステップ）だけが
    open のまま残る。`stale_after_seconds` 以上経過してもなお open な最終行は、
    後続の mark 呼び出しが行われなかった（mark漏れ）疑いがあるとみなす。
    """
    now_dt = now if now is not None else _now_utc()
    stale: list[dict[str, Any]] = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        try:
            last: Any = json.loads(lines[-1])
        except json.JSONDecodeError:
            continue
        if not isinstance(last, dict) or last.get("ended_at") is not None:
            continue
        started_at = _parse_dt(last.get("started_at", ""))
        if started_at is None:
            continue
        age_seconds = (now_dt - started_at).total_seconds()
        if age_seconds >= stale_after_seconds:
            stale.append(
                {
                    "key": path.stem,
                    "step": last.get("step"),
                    "started_at": last.get("started_at"),
                    "age_seconds": age_seconds,
                }
            )
    return stale


# ── 出力フォーマット ─────────────────────────────────────────────────────


def _format_report(agg: dict[str, dict[str, Any]], total_records: int) -> str:
    lines = [
        f"## tidd ai-review-timing レポート（対象レコード: {total_records} 件）",
        "",
        "| ステップ | 件数 | median (s) | p90 (s) |",
        "|---------|-----|-----------|--------|",
    ]
    for step, stats in agg.items():
        lines.append(f"| {step} | {stats['count']} | {stats['median']} | {stats['p90']} |")
    return "\n".join(lines) + "\n"


def _format_age(age_seconds: float) -> str:
    total = round(age_seconds)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}時間{minutes:02d}分"
    return f"{minutes}分"


def _format_stale_warnings(stale: list[dict[str, Any]]) -> str:
    if not stale:
        return ""
    lines = [
        "",
        "## mark 漏れ疑い（境界が open のまま長時間経過）",
        "",
        "| キー | ステップ | started_at | 経過時間 |",
        "|-----|---------|-----------|--------|",
    ]
    for item in stale:
        lines.append(f"| {item['key']} | {item['step']} | {item['started_at']} | {_format_age(item['age_seconds'])} |")
    return "\n".join(lines) + "\n"


# ── CLI エントリポイント ──────────────────────────────────────────────────


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "ai-review-timing",
        help="ai-review / pre-flight のステップ別 timing を集計する（#2102）",
        description=(
            "~/.cache/ai-review-timing/*.jsonl（Issue #2100・#2101）を集計し"
            "ステップ別の median / p90 秒数をレポートします。"
        ),
    )
    parser.add_argument(
        "--report",
        action="store_true",
        required=True,
        help="ステップ別 median/p90 レポートを出力する",
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    del args  # --report のみのため未使用
    from tidd_tools import timing_log

    # #3295: 統一イベントログを第一ソースとし、旧 jsonl は移行前履歴のフォールバック。
    step_durations: dict[str, list[float]] = {}
    try:
        unified_events = timing_log.read_all_events()
    except OSError:
        unified_events = []
    step_durations.update(_load_step_durations_from_events(unified_events))

    files = _find_jsonl_files(_timing_dir())
    if not files and not step_durations:
        print("計測データが見つかりません（~/.cache/ai-review-timing/ にファイルがありません）", file=sys.stderr)
        return 1

    for step, values in _load_step_durations(files).items():
        step_durations.setdefault(step, []).extend(values)
    stale = _find_stale_open_boundaries(files)
    if not step_durations and not stale:
        print("計測データが見つかりません（有効なレコードがありません）", file=sys.stderr)
        return 1

    output = ""
    if step_durations:
        agg = _aggregate(step_durations)
        total_records = sum(len(v) for v in step_durations.values())
        output += _format_report(agg, total_records)
    output += _format_stale_warnings(stale)
    print(output, end="")
    return 0


if __name__ == "__main__":
    _parser = argparse.ArgumentParser()
    _sub = _parser.add_subparsers(dest="command")
    register(_sub)
    _args = _parser.parse_args()
    sys.exit(run_cli(_args))
