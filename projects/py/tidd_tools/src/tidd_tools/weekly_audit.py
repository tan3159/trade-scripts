"""`tidd weekly-audit` サブコマンド (Issue #1282).

`/weekly-audit` slash command 用のテスト可能な helper。

- `pick-random-pr`: stdin の PR JSON リストから seed 決定的に 1 件抽出
- `format-mismatch-issue`: 元 verdict と opus 判定が不一致のとき Issue title / body を組み立てる
- `bypass-summary`: `shared/paths.cache_dir() / "bypass-audit.jsonl"` を集計してバイパス使用回数を出力
  (#1625・#2950 でパスを `.claude/hooks/_lib/bypass_audit.py`（writer）と揃えて cache_dir() 経由に統一)

skill 側（`.claude/skills/weekly-audit/SKILL.md`）はこれらの subcommand を呼び出しつつ、
GitHub 操作は MCP tool（`mcp__github__list_pull_requests` 等）で行う。
"""

from __future__ import annotations

import argparse
import datetime
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from tidd_tools.shared import paths


def pick_random_merged_pr(prs: list[dict[str, Any]], seed: int | None = None) -> dict[str, Any] | None:
    """PR リストから決定的に 1 件抽出する.

    - 空リスト → None
    - 1 件 → その 1 件
    - 複数 → `random.Random(seed).choice(prs)`

    `seed` を渡せばテスト再現性が保てる。skill 側は `int(time.time())` を渡すことで
    実行時刻に応じて変動する分布を得る。
    """
    if not prs:
        return None
    rng = random.Random(seed)
    return rng.choice(prs)


def format_mismatch_issue_title(pr_num: int) -> str:
    """Issue タイトルを組み立てる.

    validate-issue.py の `🤖 <type>: <説明>` 形式に準拠。
    """
    return f"🤖 fix: 週次 opus クロスチェック: PR #{pr_num} の verdict 再検証"


def format_mismatch_issue_body(
    *,
    pr_num: int,
    pr_title: str,
    pr_url: str,
    original_verdict: str,
    secondary_verdict: str,
    rationale: str,
) -> str:
    """不一致 Issue の本文を組み立てる.

    validate-issue.py の必須セクション（背景・やること・振る舞い）を含む。
    """
    return f"""## 背景

`/weekly-audit` が過去 1 週間の merged PR から抽出したサンプルで、元 verdict と opus 判定が不一致だった。

| 項目 | 値 |
|---|---|
| PR | [#{pr_num} {pr_title}]({pr_url}) |
| 元 verdict（agy/codex/sonnet） | {original_verdict} |
| opus 判定 | {secondary_verdict} |

**解消すべき Pain:** 元レビューが誤 APPROVE している可能性があり、post-merge で修正しないと本番運用に drift が残る。

## opus からの指摘（rationale）

{rationale}

## やること

- [ ] PR #{pr_num} の変更を再確認し、opus の指摘が妥当かを判断する
- [ ] 妥当なら follow-up PR を作成して修正する
- [ ] 誤検知なら本 Issue を `not_planned` で close する

## 振る舞い

Feature: 元レビューと opus 判定の不一致解消

  Scenario: 妥当な指摘の場合
    Given opus が指摘した箇所が実運用で問題を引き起こす
    When follow-up PR で修正する
    Then follow-up PR がマージされ本 Issue が close される

  Scenario: 誤検知の場合
    Given opus の指摘が実運用で問題を引き起こさない
    When 本 Issue を `not_planned` で close する
    Then Issue が `not_planned` 状態になる

## 参照

- 起票元 skill: `.claude/skills/weekly-audit/SKILL.md`
- 週次クロスチェック運用: `docs/setup/weekly-opus-cross-check.md`
- 元 Issue: #1282
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_pick_random_pr(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    try:
        prs = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        print(f"pick-random-pr: 不正な JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(prs, list):
        print("pick-random-pr: JSON はリストである必要があります", file=sys.stderr)
        return 1
    picked = pick_random_merged_pr(prs, seed=args.seed)
    print(json.dumps(picked, ensure_ascii=False))
    return 0


def _cmd_format_mismatch_issue(args: argparse.Namespace) -> int:
    if args.from_stdin:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            print(f"format-mismatch-issue: 不正な JSON: {exc}", file=sys.stderr)
            return 1
        required = ("pr_num", "pr_title", "pr_url", "original", "secondary", "rationale")
        missing = [k for k in required if k not in payload]
        if missing:
            print(f"format-mismatch-issue: 必須キー不足: {missing}", file=sys.stderr)
            return 1
        pr_num = int(payload["pr_num"])
        pr_title = payload["pr_title"]
        pr_url = payload["pr_url"]
        original = payload["original"]
        secondary = payload["secondary"]
        rationale = payload["rationale"]
    else:
        pr_num = args.pr_num
        pr_title = args.pr_title
        pr_url = args.pr_url
        original = args.original
        secondary = args.secondary
        rationale = args.rationale
    title = format_mismatch_issue_title(pr_num=pr_num)
    body = format_mismatch_issue_body(
        pr_num=pr_num,
        pr_title=pr_title,
        pr_url=pr_url,
        original_verdict=original,
        secondary_verdict=secondary,
        rationale=rationale,
    )
    print(json.dumps({"title": title, "body": body}, ensure_ascii=False))
    return 0


# Issue #2950: Path.home() / ".cache" / "tidd" ハードコードを shared/paths.cache_dir() へ統一
# （writer 側 .claude/hooks/_lib/bypass_audit.py とパスを揃える必要があるため同時変更）
_DEFAULT_BYPASS_LOG = paths.cache_dir() / "bypass-audit.jsonl"

# Issue #1625: 集計対象のバイパスマーカー種別
_KNOWN_EVENTS = [
    "allow-test-update",
    "allow-single-commit",
    "allow-ai-confirm-keyword",
    "split-not-possible",
]


def summarize_bypass_events(
    log_path: Path | None = None,
    *,
    since_days: int = 7,
) -> str:
    """バイパス audit log を集計してサマリー文字列を返す.

    Args:
        log_path: 読み込む JSONL ファイルのパス。None のときはデフォルトパスを使う。
        since_days: 集計対象期間（日数）。

    Returns:
        バイパス使用サマリーの文字列（stdout 出力用）。
        ログが存在しないか空のとき "バイパス使用: 0 件（audit log なし）" を返す。
    """
    path = log_path if log_path is not None else _DEFAULT_BYPASS_LOG
    if not path.is_file():
        return "バイパス使用: 0 件（audit log なし）"

    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=since_days)
    counts: Counter[str] = Counter()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "バイパス使用: 0 件（audit log なし）"

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_str = entry.get("timestamp", "")
        try:
            ts = datetime.datetime.fromisoformat(ts_str)
            # naive な datetime は UTC とみなす
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.UTC)
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        event = entry.get("event", "unknown")
        counts[event] += 1

    if not counts:
        return "バイパス使用: 0 件（audit log なし）"

    parts = []
    for event in _KNOWN_EVENTS:
        if event in counts:
            parts.append(f"{event} {counts[event]} 件")
    # 未知イベントも集計
    for event, count in sorted(counts.items()):
        if event not in _KNOWN_EVENTS:
            parts.append(f"{event} {count} 件")

    return "バイパス使用: " + "、".join(parts)


def _cmd_bypass_summary(args: argparse.Namespace) -> int:
    log_path = Path(args.audit_log) if args.audit_log else None
    summary = summarize_bypass_events(log_path=log_path, since_days=args.since_days)
    print(summary)
    return 0


def _cmd_bypass_summary_default(args: argparse.Namespace) -> int:
    """Issue #1782: サブコマンドなしで weekly-audit を実行した場合のデフォルト動作.

    bypass-summary を since_days=7 で実行する。
    BYPASS_AUDIT_LOG 環境変数でログパスを上書きできる（テスト用途）。
    """
    import os

    audit_log_env = os.environ.get("BYPASS_AUDIT_LOG")
    log_path = Path(audit_log_env) if audit_log_env else None
    summary = summarize_bypass_events(log_path=log_path, since_days=7)
    print(summary)
    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "weekly-audit",
        help="/weekly-audit skill 用の helper (#1282)",
        description=(
            "/weekly-audit slash command のテスト可能な helper.\n"
            "サブコマンドなしで実行するとバイパス使用回数サマリーを出力する（Issue #1782）。"
        ),
    )
    # Issue #1782: サブコマンドなしで実行した場合にデフォルト動作（bypass-summary）を実行する。
    # required=False にしてデフォルト func を設定する。
    sub = parser.add_subparsers(dest="subcommand", required=False)
    parser.set_defaults(func=_cmd_bypass_summary_default)

    p_pick = sub.add_parser(
        "pick-random-pr",
        help="stdin の PR JSON リストから 1 件を決定的に抽出",
    )
    p_pick.add_argument("--seed", type=int, default=None, help="乱数 seed（省略時は非決定的）")
    p_pick.set_defaults(func=_cmd_pick_random_pr)

    p_fmt = sub.add_parser(
        "format-mismatch-issue",
        help="不一致 Issue の title / body を JSON で出力",
    )
    p_fmt.add_argument(
        "--from-stdin",
        action="store_true",
        help="stdin から JSON payload を読む（シェル引数への埋め込みを避けた安全な入力経路）",
    )
    p_fmt.add_argument("--pr-num", type=int, help="PR 番号（--from-stdin 未指定時に必須）")
    p_fmt.add_argument("--pr-title", help="PR タイトル（--from-stdin 未指定時に必須）")
    p_fmt.add_argument("--pr-url", help="PR URL（--from-stdin 未指定時に必須）")
    p_fmt.add_argument("--original", help="元 verdict（APPROVE / REQUEST_CHANGES）")
    p_fmt.add_argument("--secondary", help="opus 判定")
    p_fmt.add_argument("--rationale", help="opus からの指摘・判定根拠")
    p_fmt.set_defaults(func=_cmd_format_mismatch_issue)

    # Issue #1625: bypass-summary サブコマンド
    p_bypass = sub.add_parser(
        "bypass-summary",
        help="バイパス audit log を集計してバイパス使用回数を出力 (#1625)",
    )
    p_bypass.add_argument(
        "--audit-log",
        default=None,
        help=f"読み込む JSONL ファイルパス（省略時: {_DEFAULT_BYPASS_LOG}）",
    )
    p_bypass.add_argument(
        "--since-days",
        type=int,
        default=7,
        help="集計対象期間（日数・デフォルト 7）",
    )
    p_bypass.set_defaults(func=_cmd_bypass_summary)
