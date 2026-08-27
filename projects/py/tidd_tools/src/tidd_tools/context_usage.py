"""`tidd context-usage` サブコマンド (Issue #1884・親 #1872・AND ゲート #3075).

`~/.claude/projects/<プロジェクトdir>/*.jsonl` の `message.usage` フィールドを
週別（月曜起点・UTC）に集計し、input / cache_creation / cache_read / assistant
メッセージ数を表形式で stdout に出力する。`--check` で直近週の非キャッシュ input
（input_tokens + cache_creation_input_tokens）が直近 4 週移動平均の 1.3 倍を超え、
かつ 1 メッセージあたり非キャッシュ input も直近 4 週平均の 1.3 倍を超えたときのみ
alert Issue を自動起票する（AND ゲート・fingerprint 重複防止・
watch-circleci-failures と同方式）。稼働量の増加のみ（1 メッセージあたりは横ばい）
では発火せず、キャッシュミス増・compaction 頻発などの効率退行のみを検知する
（実例・分析: #3071）。

静的予算（#1882 WARN / #1883 HARD）は rules の肥大化しか捕捉できないため、
subagent 起動増・キャッシュミス増・ループ長期化による実消費の退行を本コマンドで
週次監視する。運用手順: docs/reference/context-usage.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from tidd_tools.shared import git_client
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GitCommandError

# 起票・重複検索・コメントは watch-circleci-failures と同方式（Issue #1884 設計判断）
from tidd_tools.watch_circleci_failures import (
    EXIT_GH_AUTH_FAIL,
    EXIT_GH_RATE_LIMIT,
    _create_issue,
    _find_existing_by_fingerprint,
    _post_comment,
)

DEFAULT_WEEKS = 4
MOVING_AVERAGE_WEEKS = 4
ALERT_RATIO = 1.3


@dataclass
class WeekUsage:
    input_tokens: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    assistant_messages: int = 0

    @property
    def noncache(self) -> int:
        return self.input_tokens + self.cache_creation

    @property
    def noncache_per_message(self) -> float:
        """1 assistant メッセージあたりの非キャッシュ input（作業量増との切り分け用・Issue #3075）."""
        if self.assistant_messages == 0:
            return 0.0
        return self.noncache / self.assistant_messages


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "context-usage",
        help="実測トークン消費の週次レポートと消費増アラート Issue 起票（Issue #1884）",
        description=(
            "~/.claude/projects/<proj>/*.jsonl の usage を週別集計して表示する。"
            "--check で直近週の非キャッシュ input・1 メッセージあたり非キャッシュ input の"
            "両方が 4 週移動平均の 1.3 倍を超えたら alert Issue を自動起票する"
            "（AND ゲート・fingerprint 重複防止）。"
        ),
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=DEFAULT_WEEKS,
        help=f"レポート対象の週数（デフォルト {DEFAULT_WEEKS}）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="直近週の非キャッシュ input・1 メッセージあたり非キャッシュ input の両方が"
        " 4 週移動平均の 1.3 倍超過なら alert Issue を起票（AND ゲート）",
    )
    parser.add_argument(
        "--projects-dir",
        type=Path,
        default=None,
        help="JSONL ディレクトリ（省略時は ~/.claude/projects/<リポジトリパス変換名>）",
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def default_projects_dir(repo_root: Path) -> Path:
    """リポジトリパスから Claude Code の projects ディレクトリ名を導出する（非英数字→ `-`）."""
    name = re.sub(r"[^A-Za-z0-9]", "-", str(repo_root))
    return Path.home() / ".claude" / "projects" / name


def aggregate_weekly(projects_dir: Path) -> dict[dt.date, WeekUsage]:
    """*.jsonl の assistant 行 usage を週別（月曜起点・UTC）に集計する.

    同一 API 応答がストリーミングで複数行に現れるため requestId（なければ
    message.id / uuid）で重複排除する。パース不能行・usage なし行は無視する。
    """
    buckets: dict[dt.date, WeekUsage] = {}
    if not projects_dir.is_dir():
        return buckets
    seen: set[str] = set()
    for jsonl_path in sorted(projects_dir.glob("*.jsonl")):
        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
                if not isinstance(usage, dict):
                    continue
                ts_raw = entry.get("timestamp")
                if not isinstance(ts_raw, str):
                    continue
                try:
                    ts = dt.datetime.fromisoformat(ts_raw)
                except ValueError:
                    continue
                msg_id = message.get("id") if isinstance(message, dict) else None
                dedupe_key = str(entry.get("requestId") or msg_id or entry.get("uuid") or "")
                if dedupe_key:
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                day = ts.astimezone(dt.UTC).date()
                week = day - dt.timedelta(days=day.weekday())
                bucket = buckets.setdefault(week, WeekUsage())
                bucket.assistant_messages += 1
                bucket.input_tokens += int(usage.get("input_tokens") or 0)
                bucket.cache_creation += int(usage.get("cache_creation_input_tokens") or 0)
                bucket.cache_read += int(usage.get("cache_read_input_tokens") or 0)
    return buckets


def _ratio_detail(buckets: dict[dt.date, WeekUsage]) -> tuple[dt.date, float, float | None] | None:
    """ゲート判定前の絶対量 ratio・per-message ratio を算出する（Issue #3075）.

    per-message 平均が 0（防御的ガード対象）の場合は per-message ratio を None
    として返す。バケット 4 週未満・絶対量平均 0 の場合は None。
    """
    if len(buckets) < MOVING_AVERAGE_WEEKS:
        return None
    recent = sorted(buckets)[-MOVING_AVERAGE_WEEKS:]
    values = [buckets[week].noncache for week in recent]
    average = sum(values) / MOVING_AVERAGE_WEEKS
    if average <= 0:
        return None
    latest_week = recent[-1]
    ratio = buckets[latest_week].noncache / average

    per_message_values = [buckets[week].noncache_per_message for week in recent]
    per_message_average = sum(per_message_values) / MOVING_AVERAGE_WEEKS
    if per_message_average <= 0:
        return latest_week, ratio, None
    per_message_ratio = buckets[latest_week].noncache_per_message / per_message_average
    return latest_week, ratio, per_message_ratio


def compute_alert(buckets: dict[dt.date, WeekUsage]) -> tuple[dt.date, float, float] | None:
    """直近 4 週バケットで移動平均超過を AND ゲートで判定する（Issue #3075）.

    絶対量 ratio（直近週の noncache ÷ 直近 4 週平均）と per-message ratio
    （直近週の noncache_per_message ÷ 直近 4 週平均）の両方が ALERT_RATIO を
    超えたときのみ (直近週, 絶対量 ratio, per-message ratio) を返す。これにより
    稼働量の増加のみ（per-message は横ばい）では発火せず、キャッシュミス増・
    compaction 頻発などの効率退行（per-message も増加）のみ検知する。

    per-message 平均が 0（防御的ガード。usage データに assistant_messages が
    無い旧形式等）の場合は絶対量のみで判定する。バケット 4 週未満・絶対量平均 0
    の場合は None。
    """
    detail = _ratio_detail(buckets)
    if detail is None:
        return None
    week, ratio, per_message_ratio = detail
    if per_message_ratio is None:
        # 防御的ガード: per-message データが無い場合は絶対量のみで判定する
        if ratio > ALERT_RATIO:
            return week, ratio, 0.0
        return None
    if ratio > ALERT_RATIO and per_message_ratio > ALERT_RATIO:
        return week, ratio, per_message_ratio
    return None


def make_fingerprint(week: dt.date, ratio: float) -> str:
    """週と超過率から SHA-256 先頭 12 文字の重複検知キーを算出する."""
    raw = f"context-usage|{week.isoformat()}|{ratio:.2f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _print_report(buckets: dict[dt.date, WeekUsage], weeks: int, *, json_output: bool) -> None:
    recent = sorted(buckets)[-weeks:] if weeks > 0 else sorted(buckets)
    if json_output:
        print(
            json.dumps(
                {
                    "weeks": [
                        {
                            "week": week.isoformat(),
                            "input_tokens": buckets[week].input_tokens,
                            "cache_creation_input_tokens": buckets[week].cache_creation,
                            "cache_read_input_tokens": buckets[week].cache_read,
                            "assistant_messages": buckets[week].assistant_messages,
                        }
                        for week in recent
                    ]
                },
                ensure_ascii=False,
            )
        )
        return
    print(f"{'週開始':<12}  {'input':>12}  {'cache_creation':>14}  {'cache_read':>12}  {'assistant_messages':>18}")
    for week in recent:
        usage = buckets[week]
        print(
            f"{week.isoformat():<12}  {usage.input_tokens:>12}  {usage.cache_creation:>14}  "
            f"{usage.cache_read:>12}  {usage.assistant_messages:>18}"
        )
    if not recent:
        print("(usage データなし)", file=sys.stderr)


def _alert_title(week: dt.date, ratio: float, fingerprint: str) -> str:
    return f"🤖 fix: context-usage 消費増を検知 {week.isoformat()} 週 非キャッシュ input {ratio:.2f} 倍 [{fingerprint}]"


def _alert_issue_body(
    buckets: dict[dt.date, WeekUsage],
    week: dt.date,
    ratio: float,
    per_message_ratio: float,
    fingerprint: str,
) -> str:
    recent = sorted(buckets)[-MOVING_AVERAGE_WEEKS:]
    rows = "\n".join(
        f"| {w.isoformat()} | {buckets[w].input_tokens} | {buckets[w].cache_creation} "
        f"| {buckets[w].cache_read} | {buckets[w].noncache} | {buckets[w].assistant_messages} "
        f"| {buckets[w].noncache_per_message:.1f} |"
        for w in recent
    )
    latest = buckets[week]
    return f"""## 背景

`tidd context-usage --check` が実測トークン消費の週次監視で消費増を検知しました。
直近週（{week.isoformat()} 週開始）の非キャッシュ input（input_tokens + cache_creation_input_tokens）が
直近 4 週移動平均の {ratio:.2f} 倍（閾値 {ALERT_RATIO} 倍）を超えています。

**1 メッセージあたり非キャッシュ input** も直近週実測値 {latest.noncache_per_message:.1f} で
直近 4 週平均の {per_message_ratio:.2f} 倍（閾値 {ALERT_RATIO} 倍）を超えており、単純な稼働量増ではなく
効率の退行（キャッシュミス増・compaction 頻発・ループ長期化等）が疑われます
（絶対量・per-message の AND ゲート判定・Issue #3075）。

静的予算（#1882 WARN / #1883 HARD）内でも subagent 起動増・キャッシュミス増・ループ長期化で
実消費は退行しうるため、原因の特定と削減が必要です（親 Issue #1872）。

**重複検知キー（fingerprint）:** `{fingerprint}` — 週と超過率から算出。同一週・同一超過率の
再検知時のみこの Issue にコメントが追記され、別の週の超過は新規 Issue になります。

### 週次実測値（直近 4 週）

| 週開始 | input | cache_creation | cache_read | 非キャッシュ | assistant msgs | 非キャッシュ/msg |
|---|---|---|---|---|---|---|
{rows}

## やること

- [ ] 消費増の原因を特定する（subagent 起動数・キャッシュミス・compaction 頻度・長時間ループ等）
- [ ] 原因に応じた削減策を実施する（大きい場合は個別 Issue に分割して起票する）

## 振る舞い

```gherkin
Feature: 実測トークン消費の回復

  Scenario: 削減策適用後は alert が発生しない
    Given 削減策を適用した後の週次データ
    When tidd context-usage --check を実行する
    Then exit code 0 で終了し alert Issue が新規作成されない

  Scenario: 対策前は同一 fingerprint で再検知される
    Given 対策前と同じ週次データ
    When tidd context-usage --check を実行する
    Then 本 Issue にコメントが 1 件追記され新規 Issue は作成されない
```

---
*この Issue は `tidd context-usage --check` によって自動作成されました。*"""


def _alert_comment_body(week: dt.date, ratio: float, per_message_ratio: float, fingerprint: str) -> str:
    return f"""### 再検知（自動）

`tidd context-usage --check` が同一の消費増を再検知しました（fingerprint: `{fingerprint}`）。

- 週開始: {week.isoformat()}
- 非キャッシュ input / 4 週移動平均: {ratio:.2f} 倍（閾値 {ALERT_RATIO} 倍）
- 1 メッセージあたり非キャッシュ input / 4 週移動平均: {per_message_ratio:.2f} 倍（閾値 {ALERT_RATIO} 倍）

---
*このコメントは `tidd context-usage` によって自動投稿されました。*"""


def _run_check(buckets: dict[dt.date, WeekUsage], *, dry_run: bool) -> int:
    if len(buckets) < MOVING_AVERAGE_WEEKS:
        print(
            f"==> skip: insufficient data（usage データ {len(buckets)} 週分 < {MOVING_AVERAGE_WEEKS} 週）。"
            "alert 判定をスキップします",
            file=sys.stderr,
        )
        return 0
    alert = compute_alert(buckets)
    if alert is None:
        detail = _ratio_detail(buckets)
        if detail is not None and detail[2] is not None and detail[1] > ALERT_RATIO and detail[2] <= ALERT_RATIO:
            _, ratio, per_message_ratio = detail
            print(
                f"==> context-usage: 絶対量 {ratio:.2f} 倍だが 1 メッセージあたり {per_message_ratio:.2f} 倍"
                f"（閾値 {ALERT_RATIO} 倍以内）のため alert なし（作業量増と判定）",
                file=sys.stderr,
            )
            return 0
        print(
            f"==> context-usage: 直近週の非キャッシュ input は 4 週移動平均の {ALERT_RATIO} 倍以内（alert なし）",
            file=sys.stderr,
        )
        return 0
    week, ratio, per_message_ratio = alert
    fingerprint = make_fingerprint(week, ratio)
    title = _alert_title(week, ratio, fingerprint)
    print(
        f"==> 消費増を検知: week={week.isoformat()}, ratio={ratio:.2f}, "
        f"per_message_ratio={per_message_ratio:.2f}, fp={fingerprint}",
        file=sys.stderr,
    )
    if dry_run:
        print(f"==> [dry-run] Issue 操作をスキップします: {title}", file=sys.stderr)
        return 0

    existing_number = _find_existing_by_fingerprint(fingerprint, repo=None)
    if existing_number is not None:
        _post_comment(
            existing_number,
            _alert_comment_body(week, ratio, per_message_ratio, fingerprint),
            repo=None,
        )
        print(f"==> 既存 Issue #{existing_number} に再検知コメントを追記しました", file=sys.stderr)
        return 0

    body = _alert_issue_body(buckets, week, ratio, per_message_ratio, fingerprint)
    result = _create_issue(title, body, repo=None)
    if result == "ok":
        print(f"==> alert Issue を作成しました: {title}", file=sys.stderr)
        return 0
    # Issue #1918: 認証失敗・rate limit は watch-circleci-failures の契約（#1456）に
    # 揃えて fail-loud にする（silent skip だと cron 週次実行で翌週まで気づけない）
    if result == "auth_fail":
        print(
            "==> GitHub API authentication failed（認証失敗）. Exit 10 (fail-loud, Issue #1918).",
            file=sys.stderr,
        )
        return EXIT_GH_AUTH_FAIL
    if result == "rate_limit":
        print(
            "==> GitHub API rate limit exceeded. Exit 11 (fail-loud, Issue #1918).",
            file=sys.stderr,
        )
        return EXIT_GH_RATE_LIMIT
    print("==> alert Issue の作成に失敗しました（skip 扱いで終了します）", file=sys.stderr)
    return 0


def run_cli(args: argparse.Namespace) -> int:
    if args.projects_dir is not None:
        projects_dir = args.projects_dir
    else:
        try:
            repo_root = git_client.rev_parse_show_toplevel()
        except GitCommandError as exc:
            print(f"ERROR: git rev-parse に失敗しました: {exc}", file=sys.stderr)
            return 2
        projects_dir = default_projects_dir(repo_root)

    buckets = aggregate_weekly(projects_dir)
    _print_report(buckets, args.weeks, json_output=args.json_output)

    if not args.check:
        return 0
    return _run_check(buckets, dry_run=args.dry_run)
