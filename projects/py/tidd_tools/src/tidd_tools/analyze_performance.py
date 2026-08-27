"""`tidd analyze-performance` サブコマンド（旧 `scripts/analyze-performance.sh` の Python 移植）.

統一イベントログ（`timing_log`・#2933）の `ai-review-verdict` レコードを集計し、
SLO 判定・速度回帰検知・backend 別集計・REQUEST_CHANGES 率表示を行う
（Issue #268・#727・#3324）。

#3324 で集計元を旧 `~/.cache/ai-reviewer/pr-*/timing.json` から統一日誌へ移行した:

- 集計対象は `ai-review-verdict` イベント（key: `pr-<N>`、meta に verdict /
  backend / pr_number / started_at / ended_at を持つ）
- 所要時間は `meta.ended_at - meta.started_at`（レビュー所要時間・秒）
- 中央値・最大値・最小値・p90 を `statistics` モジュールベースで計算
- SLO 設定ファイル（`--slo-json` / 既定はパッケージ同梱の `data/slo.json`）と突き合わせ

引数:
- `[slo_json]` 位置引数（省略時はパッケージ同梱 `slo.json`）

`--verbose / --dry-run / --json` は `add_common_flags` で追加（旧 sh 仕様にない `--dry-run`
は何もせず終了するための共通フラグ）。
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sqlite3
import statistics
import subprocess
import sys
from collections import defaultdict
from importlib import resources
from pathlib import Path
from typing import Any

from tidd_tools.shared.cli import add_common_flags

logger = logging.getLogger(__name__)


# source ラベルのプレフィックス
_SOURCE_PREFIX = "source: "
# 既知の source 値
_KNOWN_SOURCES = ("ci", "agent", "human")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "analyze-performance",
        help="統一日誌の ai-review-verdict を集計して SLO 判定する（旧 scripts/analyze-performance.sh）",
        description=__doc__,
    )
    parser.add_argument(
        "slo_json",
        nargs="?",
        default=None,
        help="SLO 設定ファイルパス（デフォルト: パッケージ同梱 data/slo.json）",
    )
    # Issue #1621: not-planned 率・バッチ別集計
    parser.add_argument(
        "--not-planned-breakdown",
        action="store_true",
        default=False,
        help="起票歩留まり KPI（not-planned 率・source ラベル別内訳）を集計して出力する",
    )
    parser.add_argument(
        "--since",
        dest="not_planned_since",
        default=None,
        metavar="DATE",
        help="集計開始日（ISO 8601: 2026-01-01）。未指定時は過去 90 日分",
    )
    parser.add_argument(
        "--repo",
        dest="not_planned_repo",
        default=None,
        metavar="OWNER/REPO",
        help="集計対象リポジトリ（例: being-gaia-plan/ai-dev-handbook）。未指定時は gh コマンドのデフォルト",
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    # Issue #1621: --not-planned-breakdown モードは別ハンドラに委譲
    if getattr(args, "not_planned_breakdown", False):
        return run_not_planned_breakdown(args)

    slo_path_str = args.slo_json
    records = _load_unified_records()
    if not records:
        print("計測データが見つかりません。ai-review を実行してから再試行してください。")
        return 0

    slo = _load_slo(slo_path_str)
    summary = analyze(records, slo)

    if args.json_output:
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    _print_report(summary, records)
    return 0


# ── 公開 API ────────────────────────────────────────────────────────────────


def analyze(records: list[dict[str, Any]], slo: dict[str, int]) -> dict[str, Any]:
    """統一日誌ベースの集計と SLO 判定の結果を dict で返す.

    旧 sh の出力フォーマットは `_print_report` 側で組み立てるため、ここでは
    数値だけを返すことに徹する（テスト容易性のため）。
    """
    review_durations = [_to_int(r["review_duration"]) for r in records if _is_int(r.get("review_duration"))]

    review_stats = _calc_stats(review_durations)

    approve_count = sum(1 for r in records if r.get("verdict") == "APPROVE")
    request_changes_count = sum(1 for r in records if r.get("verdict") == "REQUEST_CHANGES")
    verdict_total = approve_count + request_changes_count
    rc_rate = (request_changes_count * 100 // verdict_total) if verdict_total > 0 else 0

    # PR ごとのレビュー回数（pr_number は str / int 両対応）
    pr_counts: dict[str, int] = defaultdict(int)
    for r in records:
        pr = r.get("pr_number")
        if pr is None:
            continue
        pr_str = str(pr)
        if pr_str.isdigit():
            pr_counts[pr_str] += 1
    multi_review_prs = [(pr, cnt) for pr, cnt in pr_counts.items() if cnt >= 3]
    multi_review_prs.sort(key=lambda x: int(x[0]))

    # backend 別集計
    backend_data: dict[str, list[int]] = defaultdict(list)
    for r in records:
        backend = r.get("backend")
        duration = r.get("review_duration")
        if isinstance(backend, str) and backend and _is_safe_backend(backend) and _is_int(duration):
            backend_data[backend].append(_to_int(duration))
    backend_summary = {backend: _calc_stats(vals) for backend, vals in sorted(backend_data.items())}

    # 速度回帰検知（直近10件の中央値が全期間中央値の1.2倍を超えるか）
    regression: dict[str, Any] = {}
    if len(review_durations) >= 2:
        all_median = int(statistics.median(review_durations))
        recent_count = min(10, len(review_durations))
        recent = review_durations[-recent_count:]
        recent_median = int(statistics.median(recent))
        # 旧 sh と同じ整数演算で閾値を出す（all_median * 1.2 を 12/10 で代用）
        threshold = all_median * 12 // 10
        is_degraded = recent_median > threshold
        causes: list[dict[str, Any]] = []
        if is_degraded:
            recent_records = records[-recent_count:]
            for rec in recent_records:
                v = rec.get("review_duration")
                if _is_int(v) and _to_int(v) > threshold:
                    causes.append(
                        {
                            "pr_number": rec.get("pr_number", "N/A"),
                            "review_duration": _to_int(v),
                            "backend": rec.get("backend", "N/A"),
                        }
                    )
        regression = {
            "all_median": all_median,
            "recent_count": recent_count,
            "recent_median": recent_median,
            "threshold": threshold,
            "is_degraded": is_degraded,
            "causes": causes,
        }

    # SLO 判定
    slo_violations: list[str] = []
    review_p90 = _percentile(review_durations, 90)
    slo_review = int(slo.get("review_duration_p90_sec", 0) or 0)
    slo_rc_rate = int(slo.get("request_changes_rate_max", 0) or 0)
    if review_p90 is not None and slo_review > 0 and review_p90 > slo_review:
        slo_violations.append(f"レビュー時間p90がSLO超過 (実績: {review_p90}秒 > SLO: {slo_review}秒)")
    rc_rate_violation: list[str] = []
    if verdict_total > 0 and slo_rc_rate > 0 and rc_rate > slo_rc_rate:
        rc_pr_list = [f"#{r.get('pr_number', 'N/A')}" for r in records if r.get("verdict") == "REQUEST_CHANGES"]
        recent_rc = rc_pr_list[-5:] if rc_pr_list else []
        slo_violations.append(f"REQUEST_CHANGES率がSLO超過 (実績: {rc_rate}% > SLO: {slo_rc_rate}%)")
        rc_rate_violation = recent_rc

    return {
        "total": len(records),
        "review": review_stats,
        "verdict": {
            "approve": approve_count,
            "request_changes": request_changes_count,
            "total": verdict_total,
            "request_changes_rate": rc_rate,
        },
        "multi_review_prs": multi_review_prs,
        "backend": backend_summary,
        "regression": regression,
        "slo": {
            "violations": slo_violations,
            "review_p90": review_p90,
            "recent_request_changes_prs": rc_rate_violation,
            "thresholds": {
                "review_duration_p90_sec": slo_review,
                "request_changes_rate_max": slo_rc_rate,
            },
        },
    }


# ── 内部ユーティリティ ─────────────────────────────────────────────────────


def _is_int(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    return bool(isinstance(v, str) and v.isdigit())


def _to_int(v: Any) -> int:
    """`_is_int(v)` を満たす Any を int に安全に変換する."""
    if isinstance(v, bool):
        # bool は int のサブクラスだが、`_is_int` が False を返すケースのみ通る
        raise TypeError("bool is not a valid int duration")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        return int(v)
    raise TypeError(f"unexpected duration type: {type(v).__name__}")


def _is_safe_backend(name: str) -> bool:
    """旧 sh の `^[a-zA-Z0-9_-]+$` バリデーションを移植."""
    return bool(name) and all(c.isalnum() or c in "_-" for c in name)


def _calc_stats(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"count": 0, "median": None, "max": None, "min": None}
    s = sorted(values)
    # 旧 sh は count/2 の位置を中央値として採用していた（偶数件数でも下側）。
    # statistics.median は補間するため、ここでも旧 sh と同じインデックス選択を行う。
    median = s[len(s) // 2]
    return {
        "count": len(s),
        "median": median,
        "max": s[-1],
        "min": s[0],
    }


def _percentile(values: list[int], pct: int) -> int | None:
    """旧 sh の `count * pct / 100` インデックス選択方式の p 値を返す."""
    if not values:
        return None
    s = sorted(values)
    idx = len(s) * pct // 100
    if idx >= len(s):
        idx = len(s) - 1
    return s[idx]


def _parse_dt(value: str) -> datetime.datetime | None:
    """ISO 8601（末尾 ``Z`` 許容）の日時文字列をパースする."""
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _load_unified_records() -> list[dict[str, Any]]:
    """統一日誌（timing_log）の ai-review-verdict レコードを分析用レコードへ変換する.

    - 所要時間は meta.started_at〜ended_at の差（秒・整数切り捨て）
    - 日時が揃わないレコードは review_duration なしとして扱い、統計集計からは
      除外する（best-effort・旧 timing.json の不正行スキップと同等）
    - 時系列順は meta.ended_at（なければイベント時刻）の文字列ソート

    DB 読み込み失敗（破損・権限等）は空リストで fail-open する。
    """
    from tidd_tools import timing_log

    try:
        events = timing_log.read_all_events()
    except (OSError, sqlite3.Error):
        return []

    records: list[dict[str, Any]] = []
    for event in events:
        if event.get("step") != "ai-review-verdict":
            continue
        meta = event.get("meta") or {}
        started = _parse_dt(str(meta.get("started_at", "")))
        ended = _parse_dt(str(meta.get("ended_at", "")))
        record: dict[str, Any] = {
            "timestamp": str(meta.get("ended_at") or event.get("timestamp", "")),
            "pr_number": meta.get("pr_number"),
            "verdict": meta.get("verdict"),
            "backend": meta.get("backend"),
        }
        if started is not None and ended is not None:
            duration = (ended - started).total_seconds()
            record["review_duration"] = int(max(0, duration))
        records.append(record)

    records.sort(key=lambda r: str(r.get("timestamp", "")))
    return records


def _load_slo(slo_path_str: str | None) -> dict[str, int]:
    """SLO 設定ファイルを読み込む.

    引数指定があればそれ、なければパッケージ同梱の `data/slo.json` を使う。
    """
    if slo_path_str:
        path = Path(slo_path_str).expanduser()
        try:
            return _read_slo_file(path)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("SLO ファイル読み込み失敗: %s (%s)", path, exc)
            return {}

    # パッケージ同梱
    try:
        with resources.as_file(resources.files("tidd_tools.data").joinpath("slo.json")) as p:
            return _read_slo_file(p)
    except (OSError, FileNotFoundError, json.JSONDecodeError, ModuleNotFoundError) as exc:
        logger.warning("パッケージ同梱 SLO 読み込み失敗: %s", exc)
        return {}


def _read_slo_file(path: Path) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in data.items():
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _print_coverage_row() -> None:
    """Issue #1287: coverage.xml があれば line/branch coverage を 1 行表示する.

    coverage.xml は CI 実行後にリポジトリルートに生成される想定。
    存在しない場合は「(no data)」を出力（フェイルオープン）。
    """
    from tidd_tools.coverage_summary import format_summary_row, parse_coverage_xml

    candidates = [
        Path.cwd() / "coverage.xml",
        Path.cwd() / "projects" / "py" / "tidd_tools" / "coverage.xml",
    ]
    summary = None
    for candidate in candidates:
        summary = parse_coverage_xml(candidate)
        if summary is not None:
            break
    print("--- テスト coverage ---")
    print(f"  {format_summary_row(summary)}")
    print()


def _print_mutation_row() -> None:
    """Issue #1286: mutmut-report.xml があれば mutation score を 1 行表示する.

    mutmut 週次 job の artifact をローカルに配置している場合に表示される。
    存在しない場合は「(no data)」を出力（フェイルオープン）。
    """
    from tidd_tools.mutation_summary import (
        format_summary_row as _fmt_mut,
    )
    from tidd_tools.mutation_summary import (
        parse_mutation_xml,
    )

    candidates = [
        Path.cwd() / "mutmut-report.xml",
        Path.cwd() / "projects" / "py" / "tidd_tools" / "mutmut-report.xml",
        Path("/tmp/mutmut-report.xml"),
    ]
    summary = None
    for candidate in candidates:
        summary = parse_mutation_xml(candidate)
        if summary is not None:
            break
    print("--- mutation testing ---")
    print(f"  {_fmt_mut(summary)}")
    print()


def _print_report(summary: dict[str, Any], records: list[dict[str, Any]]) -> None:
    total = summary["total"]
    print("=== AI レビュー パフォーマンス分析 ===")
    print(f"計測回数: {total} 件")
    print()

    # Issue #1287: coverage 集計を報告に含める（coverage.xml 有無で条件付き）
    _print_coverage_row()
    # Issue #1286: mutation score 集計（mutmut-report.xml 有無で条件付き）
    _print_mutation_row()

    review = summary["review"]
    if review["count"] > 0:
        print("--- レビュー時間 (review_duration) ---")
        print(f"  件数  : {review['count']} 件")
        print(f"  中央値: {review['median']} 秒")
        print(f"  最大値: {review['max']} 秒")
        print(f"  最小値: {review['min']} 秒")
        print()

    verdict = summary["verdict"]
    print("--- VERDICT 分布 ---")
    print(f"  APPROVE        : {verdict['approve']} 件")
    print(f"  REQUEST_CHANGES: {verdict['request_changes']} 件")
    if verdict["total"] > 0:
        print(f"  REQUEST_CHANGES率: {verdict['request_changes_rate']}%")
    print()

    print("--- PRごとのレビュー回数 ---")
    if summary["multi_review_prs"]:
        print("  3回以上レビューされたPR:")
        for pr, cnt in summary["multi_review_prs"]:
            print(f"    PR #{pr}: {cnt}回")
    else:
        print("  3回以上レビューされたPRはありません")
    print()

    print("--- backend別 レビュー時間 ---")
    backend = summary["backend"]
    if backend:
        for name, stats in backend.items():
            print(f"  {name}: 件数={stats['count']}, 中央値={stats['median']}秒, 最大値={stats['max']}秒")
    else:
        print("  backendデータなし")
    print()

    regression = summary["regression"]
    if regression:
        print("--- 速度回帰検知 ---")
        print(f"  全期間中央値 : {regression['all_median']} 秒")
        print(f"  直近{regression['recent_count']}件中央値: {regression['recent_median']} 秒")
        if regression["is_degraded"]:
            print(
                f"  ⚠ 速度劣化中 (直近中央値{regression['recent_median']}s > "
                f"全期間中央値{regression['all_median']}sの1.2倍={regression['threshold']}s)"
            )
            print("  原因候補:")
            for c in regression["causes"]:
                print(f"    PR #{c['pr_number']}: {c['review_duration']}秒 ({c['backend']})")
        else:
            print("  速度劣化なし")
        print()

    print("--- SLO判定 ---")
    slo = summary["slo"]
    if not slo["violations"]:
        print("  SLO違反なし")
    else:
        for msg in slo["violations"]:
            print(f"  ⚠ {msg}")
        if slo["recent_request_changes_prs"]:
            joined = " ".join(slo["recent_request_changes_prs"])
            print(f"  直近のREQUEST_CHANGES PR: {joined}")
    print()

    print("--- 最近5件の計測結果 ---")
    start = max(0, total - 5)
    for r in records[start:]:
        ts = r.get("timestamp", "N/A")
        pr = r.get("pr_number", "N/A")
        duration_v = r.get("review_duration", "N/A")
        verdict_v = r.get("verdict", "N/A")
        backend_v = r.get("backend", "N/A")
        print(f"  [{ts}] PR #{pr}: {duration_v}s, {verdict_v} ({backend_v})")


# ── Issue #1621: not-planned 率・バッチ別集計 ─────────────────────────────


def _check_token() -> None:
    """GITHUB_TOKEN または GH_TOKEN が設定されているか確認する.

    未設定の場合は ValueError を raise する。
    """
    if not os.environ.get("GITHUB_TOKEN") and not os.environ.get("GH_TOKEN"):
        raise ValueError("GITHUB_TOKEN が未設定です")


def _extract_source(labels: list[dict[str, Any]]) -> str:
    """ラベルリストから source ラベルの値を抽出する.

    見つからなければ "unknown" を返す。
    """
    for label in labels:
        name: str = label.get("name", "")
        if name.startswith(_SOURCE_PREFIX):
            return name[len(_SOURCE_PREFIX) :]
    return "unknown"


def fetch_not_planned_issues(
    repo: str | None = None,
    since: str | None = None,
    _token_checked: bool = False,
) -> list[dict[str, Any]]:
    """not-planned で閉じられた Issue 一覧を gh API で取得する.

    Args:
        repo: OWNER/REPO 形式のリポジトリ識別子。None の場合は gh のデフォルト
        since: ISO 8601 日付文字列。None の場合は過去 90 日
        _token_checked: True のとき token チェックをスキップ（内部テスト用）

    Returns:
        Issue オブジェクトのリスト（number, createdAt, labels フィールドを含む）

    Raises:
        ValueError: GITHUB_TOKEN / GH_TOKEN が未設定
        RuntimeError: gh コマンドが非ゼロ終了
    """
    if not _token_checked:
        _check_token()

    query = "state:closed state_reason:not_planned"
    if since:
        query += f" created:>={since}"

    cmd = [
        "gh",
        "issue",
        "list",
        "--json",
        "number,createdAt,labels",
        "--search",
        query,
        "--limit",
        "1000",
        "--state",
        "all",
    ]
    if repo:
        cmd += ["--repo", repo]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"gh issue list 失敗: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    if not isinstance(data, list):
        return []
    return data


def fetch_all_created_issues(
    repo: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """指定期間に作成された全 Issue を gh API で取得する.

    Args:
        repo: OWNER/REPO 形式のリポジトリ識別子
        since: ISO 8601 日付文字列

    Returns:
        Issue オブジェクトのリスト
    """
    query = "is:issue"
    if since:
        query += f" created:>={since}"

    cmd = [
        "gh",
        "issue",
        "list",
        "--json",
        "number,createdAt,labels",
        "--search",
        query,
        "--limit",
        "1000",
        "--state",
        "all",
    ]
    if repo:
        cmd += ["--repo", repo]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        logger.warning("gh issue list (all) 失敗: %s", result.stderr.strip())
        return []

    data = json.loads(result.stdout)
    if not isinstance(data, list):
        return []
    return data


def _issue_week_key(created_at: str) -> str:
    """ISO 8601 日付文字列を ISO year-week キー（例: '2026-W03'）に変換する."""
    dt = datetime.datetime.fromisoformat(created_at.rstrip("Z").replace("Z", "+00:00"))
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _week_keys_in_range(since: str, until: str | None = None) -> list[str]:
    """since から until（省略時は今日）までの ISO year-week キー一覧を返す."""
    start = datetime.datetime.fromisoformat(since.rstrip("Z"))
    end = (
        datetime.datetime.fromisoformat(until.rstrip("Z"))
        if until
        else datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    )
    keys: list[str] = []
    current = start
    while current <= end:
        iso_year, iso_week, _ = current.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
        if not keys or keys[-1] != key:
            keys.append(key)
        current += datetime.timedelta(days=7)
    # end の週も含める
    iso_year, iso_week, _ = end.isocalendar()
    end_key = f"{iso_year}-W{iso_week:02d}"
    if not keys or keys[-1] != end_key:
        keys.append(end_key)
    return keys


def aggregate_not_planned(
    all_issues: list[dict[str, Any]],
    not_planned_issues: list[dict[str, Any]],
    since: str | None = None,
) -> dict[str, Any]:
    """全起票数と not-planned 数から KPI を集計する.

    Args:
        all_issues: 対象期間に作成された全 Issue リスト
        not_planned_issues: not-planned で閉じられた Issue リスト
        since: 集計開始日（ISO 8601 形式）。指定すると期間内の 0 件週も weekly_breakdown に含める

    Returns:
        集計結果 dict:
            - total_created: int
            - total_not_planned: int
            - not_planned_rate: float | None  (0 件なら None = N/A)
            - source_breakdown: dict[str, dict]  source ラベル別内訳
            - weekly_breakdown: dict[str, dict]  週次内訳（ISO year-week キー）
    """
    total_created = len(all_issues)
    total_not_planned = len(not_planned_issues)

    rate: float | None = None
    if total_created > 0:
        rate = total_not_planned / total_created * 100

    # not-planned の Issue 番号セット（source 内訳・週次内訳の集計に使う）
    np_numbers: set[int] = {issue["number"] for issue in not_planned_issues}

    # source ラベル別に集計
    source_created: dict[str, int] = defaultdict(int)
    source_np: dict[str, int] = defaultdict(int)

    # 週次集計（createdAt の ISO year-week をキーに）
    weekly_created: dict[str, int] = defaultdict(int)
    weekly_np: dict[str, int] = defaultdict(int)

    for issue in all_issues:
        src = _extract_source(issue.get("labels", []))
        source_created[src] += 1
        if issue["number"] in np_numbers:
            source_np[src] += 1

        # 週次集計
        created_at = issue.get("createdAt", "")
        if created_at:
            week_key = _issue_week_key(created_at)
            weekly_created[week_key] += 1
            if issue["number"] in np_numbers:
                weekly_np[week_key] += 1

    source_breakdown: dict[str, dict[str, Any]] = {}
    for src, created in sorted(source_created.items()):
        np_count = source_np.get(src, 0)
        src_rate: float | None = np_count / created * 100 if created > 0 else None
        source_breakdown[src] = {
            "created": created,
            "not_planned": np_count,
            "rate": src_rate,
        }

    # 週次内訳（時系列順にソート）
    # since が指定されている場合、期間内の全週（0 件週を含む）を生成する
    all_week_keys: set[str] = set(weekly_created.keys())
    if since:
        for key in _week_keys_in_range(since):
            all_week_keys.add(key)

    weekly_breakdown: dict[str, dict[str, Any]] = {}
    for week_key in sorted(all_week_keys):
        wc = weekly_created.get(week_key, 0)
        wnp = weekly_np.get(week_key, 0)
        w_rate: float | None = wnp / wc * 100 if wc > 0 else None
        weekly_breakdown[week_key] = {
            "created": wc,
            "not_planned": wnp,
            "rate": w_rate,
        }

    return {
        "total_created": total_created,
        "total_not_planned": total_not_planned,
        "not_planned_rate": rate,
        "source_breakdown": source_breakdown,
        "weekly_breakdown": weekly_breakdown,
    }


def print_not_planned_report(summary: dict[str, Any]) -> None:
    """not-planned 率レポートを stdout に出力する."""
    total_created = summary["total_created"]
    total_not_planned = summary["total_not_planned"]
    rate = summary["not_planned_rate"]
    rate_str = f"{rate:.1f}%" if rate is not None else "N/A"

    print("=== 起票歩留まり KPI（not-planned 率） ===")
    print(f"起票数: {total_created} 件、not-planned: {total_not_planned} 件 ({rate_str})")
    print()

    # 週次内訳
    weekly_breakdown = summary.get("weekly_breakdown", {})
    if weekly_breakdown:
        print("--- 週次内訳 ---")
        print(f"  {'週':>8} {'起票数':>6} {'not-planned':>12} {'率':>8}")
        print(f"  {'--------':>8} {'-' * 6} {'-' * 12} {'-' * 8}")
        for week_key, row in weekly_breakdown.items():
            row_rate = f"{row['rate']:.1f}%" if row["rate"] is not None else "N/A"
            print(f"  {week_key:>8} {row['created']:>6} {row['not_planned']:>12} {row_rate:>8}")
        print()

    breakdown = summary.get("source_breakdown", {})
    if breakdown:
        print("--- source ラベル別内訳 ---")
        print(f"  {'source':<12} {'起票数':>6} {'not-planned':>12} {'率':>8}")
        print(f"  {'-' * 12} {'-' * 6} {'-' * 12} {'-' * 8}")
        for src in ("ci", "agent", "human"):
            if src not in breakdown:
                continue
            row = breakdown[src]
            row_rate = f"{row['rate']:.1f}%" if row["rate"] is not None else "N/A"
            print(f"  {src:<12} {row['created']:>6} {row['not_planned']:>12} {row_rate:>8}")
        # 既知以外の source も出力
        for src, row in breakdown.items():
            if src in _KNOWN_SOURCES:
                continue
            row_rate = f"{row['rate']:.1f}%" if row["rate"] is not None else "N/A"
            print(f"  {src:<12} {row['created']:>6} {row['not_planned']:>12} {row_rate:>8}")
        print()


def run_not_planned_breakdown(args: argparse.Namespace) -> int:
    """--not-planned-breakdown モードのエントリポイント.

    Returns:
        0 = 成功、1 = GITHUB_TOKEN 未設定エラー
    """
    repo: str | None = getattr(args, "not_planned_repo", None)
    since: str | None = getattr(args, "not_planned_since", None)

    try:
        _check_token()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        not_planned = fetch_not_planned_issues(repo=repo, since=since, _token_checked=True)
        all_created = fetch_all_created_issues(repo=repo, since=since)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    summary = aggregate_not_planned(all_created, not_planned, since=since)
    print_not_planned_report(summary)
    return 0


if __name__ == "__main__":
    sys.exit(0)
