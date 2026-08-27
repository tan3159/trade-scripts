"""`tidd lead-time` サブコマンド（Issue #2006）.

merged PR のリードタイムを 4 フェーズに分解して集計する:
  (a) 実装    = ブランチ最初のコミット → PR 作成
  (b) ai-review = PR 作成 → 初回 verdict（レビューコメント投稿時刻）
  (c) 往復    = REQUEST_CHANGES 回数 + 再 push → 再 verdict の累計時間
  (d) 人間待ち = needs-human-merge ラベル付与または APPROVE → マージまでの時間
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, date, datetime
from statistics import median
from typing import Any

from tidd_tools.shared import gh_client
from tidd_tools.shared.errors import GhCommandError

# ── gh ラッパー（テストでモック対象） ────────────────────────────────────


def _run_gh_json(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """gh コマンドを実行して CompletedProcess を返す（モック差し込み用ラッパー）."""
    return subprocess.run(  # noqa: S603
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        **kwargs,
    )


# ── 日付パーサー（argparse type=） ───────────────────────────────────────


def _parse_date(value: str) -> date:
    """YYYY-MM-DD 形式を date に変換する。不正値は ArgumentTypeError を上げる（argparse が exit 2 に変換する）.

    ValueError だと argparse が汎用文言（invalid value）に差し替えるため、
    メッセージを stderr にそのまま出せる ArgumentTypeError を使う。
    """
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"日付形式が不正です（YYYY-MM-DD 形式で指定してください）: {value!r}"
        ) from None


# ── 時刻パーサー ─────────────────────────────────────────────────────────


def _parse_dt(s: str | None) -> datetime | None:
    """ISO 8601 文字列を UTC datetime に変換する。失敗時は None を返す."""
    if not s:
        return None
    try:
        # GitHub API は "2026-06-15T12:00:00Z" 形式
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _hours(a: datetime | None, b: datetime | None) -> float | None:
    """2 つの datetime の差を時間（float）で返す。どちらかが None なら None."""
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 3600.0


# ── percentile（numpy なし） ──────────────────────────────────────────────


def _percentile(values: list[float], p: float) -> float:
    """p パーセンタイルを線形補間なしで返す（numpy 非依存）."""
    if not values:
        raise ValueError("空リスト")
    sorted_v = sorted(values)
    idx = (len(sorted_v) - 1) * p / 100.0
    lo = int(idx)
    # 小数部がある場合は切り上げ側の値を使う（保守的な見積もり）
    return sorted_v[min(lo + 1, len(sorted_v) - 1)] if idx != lo else sorted_v[lo]


# ── gh データ取得 ────────────────────────────────────────────────────────


def _fetch_pr_list(since: date, limit: int, repo: str | None) -> list[dict[str, Any]]:
    """merged PR 一覧を取得する."""
    cmd = [
        "gh",
        "pr",
        "list",
        "--state",
        "merged",
        "--limit",
        str(limit),
        "--json",
        "number,title,createdAt,mergedAt,labels",
    ]
    if repo:
        cmd = [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--limit",
            str(limit),
            "--json",
            "number,title,createdAt,mergedAt,labels",
        ]
    result = _run_gh_json(cmd)
    if result.returncode != 0:
        return []
    try:
        prs: Any = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(prs, list):
        return []

    # since でフィルタ（mergedAt >= since）
    since_dt = datetime(since.year, since.month, since.day, tzinfo=UTC)
    filtered = []
    for pr in prs:
        merged_at = _parse_dt(pr.get("mergedAt"))
        if merged_at and merged_at >= since_dt:
            filtered.append(pr)
    return filtered


def _fetch_pr_detail(pr_num: int, repo: str | None) -> dict[str, Any]:
    """PR の詳細（commits・reviews）を取得する.

    timelineItems は gh pr view の有効フィールドではない（Unknown JSON field）。
    """
    fields = ("number", "createdAt", "mergedAt", "labels", "commits", "reviews")
    try:
        return gh_client.pr_view(pr_num, repo=repo, fields=fields)
    except GhCommandError:
        return {}


# ── フェーズ計測 ─────────────────────────────────────────────────────────


def _measure_phases(pr: dict[str, Any], detail: dict[str, Any]) -> dict[str, float | None]:
    """1 PR のフェーズ別リードタイム（時間）を計算する."""
    created_at = _parse_dt(pr.get("createdAt") or detail.get("createdAt"))
    merged_at = _parse_dt(pr.get("mergedAt") or detail.get("mergedAt"))

    # (a) 実装: 最初のコミット → PR 作成
    commit_dates = []
    for commit in detail.get("commits") or []:
        dt = _parse_dt(commit.get("committedDate"))
        if dt:
            commit_dates.append(dt)
    first_commit_dt = min(commit_dates) if commit_dates else None
    impl_hours = _hours(first_commit_dt, created_at)

    first_verdict_dt: datetime | None = None
    request_changes_count = 0
    roundtrip_hours_total = 0.0
    last_changes_requested_dt: datetime | None = None
    last_approve_dt: datetime | None = None

    for review in detail.get("reviews") or []:
        state = (review.get("state") or "").upper()
        submitted = _parse_dt(review.get("submittedAt"))
        body = review.get("body") or ""

        is_verdict = "VERDICT:" in body or state in ("APPROVED", "CHANGES_REQUESTED")
        if is_verdict and first_verdict_dt is None:
            first_verdict_dt = submitted

        if state == "CHANGES_REQUESTED":
            request_changes_count += 1
            last_changes_requested_dt = submitted
        elif state == "APPROVED":
            if submitted:
                last_approve_dt = submitted
            if last_changes_requested_dt is not None:
                # 往復: REQUEST_CHANGES → 次の APPROVE まで
                h = _hours(last_changes_requested_dt, submitted)
                if h is not None:
                    roundtrip_hours_total += h
                last_changes_requested_dt = None

    # (b) ai-review: PR 作成 → 初回 verdict
    ai_review_hours = _hours(created_at, first_verdict_dt)

    # (c) 往復: REQUEST_CHANGES の累計
    roundtrip_hours: float | None = roundtrip_hours_total if request_changes_count > 0 else None

    # (d) 人間待ち: 最終 APPROVE → マージまでの時間
    human_wait_hours = _hours(last_approve_dt, merged_at)

    return {
        "実装": impl_hours,
        "ai-review": ai_review_hours,
        "往復": roundtrip_hours,
        "人間待ち": human_wait_hours,
    }


# ── 集計 ────────────────────────────────────────────────────────────────


def _aggregate(phase_data: list[dict[str, float | None]]) -> dict[str, dict[str, Any]]:
    """フェーズ別に件数・median・p90 を集計する."""
    phase_names = ("実装", "ai-review", "往復", "人間待ち")
    result: dict[str, dict[str, Any]] = {}
    for name in phase_names:
        values = [v for row in phase_data if (v := row.get(name)) is not None]
        count = len(values)
        med = round(median(values), 2) if values else None
        p90 = round(_percentile(values, 90), 2) if values else None
        result[name] = {"count": count, "median": med, "p90": p90}
    return result


# ── 出力フォーマット ─────────────────────────────────────────────────────


def _format_markdown(agg: dict[str, dict[str, Any]], total_prs: int) -> str:
    """markdown 表を組み立てる."""
    lines = [
        f"## tidd lead-time レポート（対象 PR: {total_prs} 件）",
        "",
        "| フェーズ | 件数 | median (h) | p90 (h) |",
        "|---------|-----|-----------|--------|",
    ]
    for phase_name, stats in agg.items():
        med = stats["median"] if stats["median"] is not None else "-"
        p90 = stats["p90"] if stats["p90"] is not None else "-"
        lines.append(f"| {phase_name} | {stats['count']} | {med} | {p90} |")
    return "\n".join(lines) + "\n"


def _format_json(agg: dict[str, dict[str, Any]], total_prs: int) -> str:
    """JSON 形式で出力する."""
    return json.dumps({"total_prs": total_prs, "phases": agg}, ensure_ascii=False) + "\n"


# ── CLI エントリポイント ──────────────────────────────────────────────────


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "lead-time",
        help="merged PR のリードタイムを 4 フェーズで集計する（#2006）",
        description=(
            "merged PR のブランチ最初のコミットからマージまでのリードタイムを"
            "実装・ai-review・往復・人間待ちの 4 フェーズに分解して集計します。"
        ),
    )
    parser.add_argument(
        "--since",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="集計開始日（YYYY-MM-DD）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="取得する PR の上限数（デフォルト: 100）",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="owner/repo（省略時はカレントリポジトリ）",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="JSON 形式で出力する",
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    since: date | None = getattr(args, "since", None)
    limit: int = getattr(args, "limit", 100)
    repo: str | None = getattr(args, "repo", None)
    output_json: bool = getattr(args, "output_json", False)

    # since 省略時は全件（gh のデフォルトに委ねる）
    since_date = since or date(2000, 1, 1)

    prs = _fetch_pr_list(since_date, limit, repo)

    if not prs:
        print("対象 PR がありません")
        return 0

    phase_data: list[dict[str, float | None]] = []
    for pr in prs:
        pr_num = pr.get("number")
        if pr_num is None:
            continue
        detail = _fetch_pr_detail(int(pr_num), repo)
        phases = _measure_phases(pr, detail)
        phase_data.append(phases)

    agg = _aggregate(phase_data)

    if output_json:
        print(_format_json(agg, len(prs)), end="")
    else:
        print(_format_markdown(agg, len(prs)), end="")

    return 0


if __name__ == "__main__":
    import argparse as _ap

    _parser = _ap.ArgumentParser()
    _sub = _parser.add_subparsers(dest="command")
    register(_sub)
    _args = _parser.parse_args()
    sys.exit(run_cli(_args))
