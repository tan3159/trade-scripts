"""`tidd analyze-loop-errors` サブコマンド（旧 `scripts/analyze-loop-errors.sh` の Python 移植）.

`loop-error-log` で蓄積された JSONL ログ（`~/.cache/loop-error-log/errors-YYYY-MM-DD.jsonl`）
を集約し、同一パターンが閾値以上発生した場合に GitHub Issue を起票する。

引数:
- `--log-dir <dir>` ログディレクトリ（デフォルト: `$LOOP_ERROR_LOG_DIR` または
  `~/.cache/loop-error-log`）
- `--min-count <N>` Issue 化する最小発生回数（デフォルト 2）
- `--days <N>` 分析対象日数（デフォルト 7）
- `--threshold <N>` `--min-count` のエイリアス（CLAUDE.md の `[--threshold N]` 仕様）
- `--create-issues` Issue を実際に起票する（デフォルトはサマリー表示のみ）
- 共通フラグ `--verbose / --dry-run / --json`

旧 sh の振る舞いをそのまま移植している。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.gh_client import existing_open_issue, issue_create
from tidd_tools.shared.paths import cache_dir as _cache_dir
from tidd_tools.shared.subprocess_runner import run as run_subprocess
from tidd_tools.shared.subprocess_timeouts import QUICK_TIMEOUT_SEC

logger = logging.getLogger(__name__)


DEFAULT_LOG_DIRNAME = "loop-error-log"
DEFAULT_MIN_COUNT = 2
DEFAULT_DAYS = 7
KEY_LEN = 100  # 旧 sh の集約キー長（先頭100文字）

ERROR_PREVIEW_LEN = 80
ISSUE_LABELS = ["type: fix", "priority: medium", "source: ci"]

# Issue #3867: エラーメッセージに埋め込まれる `PR #101` / `Issue #2790` のような
# 可変の番号参照を集約キーへそのまま含めると、同一バグでも番号が異なるだけで
# 別パターンとして分裂集計・分裂起票されてしまう。`#<数字>` 形式を `#N` に
# マスクしてから集約・dedup 判定する。
_NUMBER_REF_RE = re.compile(r"#\d+")
_REMOTE_REPO_RE = re.compile(r"github\.com[/:]([^/ :]+/[^/]+?)(?:\.git)?/?$")
_SSH_ALIAS_REPO_RE = re.compile(r"^[^@/\s]+@[^:/\s]+:([^/]+/[^/]+?)(?:\.git)?/?$")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "analyze-loop-errors",
        help="自律ループ異常系ログを集約して Issue 化する（旧 scripts/analyze-loop-errors.sh）",
        description=__doc__,
    )
    parser.add_argument(
        "--log-dir",
        dest="log_dir",
        default=None,
        help="ログディレクトリ（デフォルト: $LOOP_ERROR_LOG_DIR or ~/.cache/loop-error-log）",
    )
    parser.add_argument(
        "--min-count",
        dest="min_count",
        type=int,
        default=DEFAULT_MIN_COUNT,
        help=f"Issue 化する最小発生回数（デフォルト: {DEFAULT_MIN_COUNT}）",
    )
    parser.add_argument(
        "--threshold",
        dest="threshold",
        type=int,
        default=None,
        help="--min-count のエイリアス（指定時は --min-count を上書き）",
    )
    parser.add_argument(
        "--days",
        dest="days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"分析対象日数（デフォルト: {DEFAULT_DAYS}）",
    )
    parser.add_argument(
        "--create-issues",
        dest="create_issues",
        action="store_true",
        help="Issue を実際に起票する（デフォルトはサマリーのみ）",
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    log_dir = _resolve_log_dir(args.log_dir)
    min_count = args.threshold if args.threshold is not None else args.min_count
    days = args.days
    dry_run = bool(args.dry_run)
    create_issues = bool(args.create_issues)

    if not log_dir.is_dir():
        print(f"ログなし（ディレクトリが存在しません: {log_dir}）")
        return 0

    cutoff_date = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    records = _collect_records(log_dir, cutoff_date)
    current_repo = _current_repo()
    records = [record for record in records if current_repo is not None and record.get("repo") == current_repo]
    if not records:
        print(f"ログなし（{days}日間のエラーログがありません）")
        return 0

    patterns = _aggregate_patterns(records)
    if not patterns:
        print("0件（有効なログエントリがありません）")
        return 0

    if args.json_output:
        print(
            json.dumps(
                {
                    "patterns": [{"step": s, "error": e, "count": c} for (s, e), c in patterns.items()],
                    "min_count": min_count,
                    "days": days,
                },
                ensure_ascii=False,
            )
        )
        return 0

    print("## 自律ループ異常系ログ分析レポート")
    print()
    print(f"対象期間: 過去 {days} 日間")
    print("集約単位: step + エラーメッセージ先頭100文字")
    print()
    print("### 全パターン（発生回数順）")
    print()
    for (step, err), count in patterns.items():
        print(f"  {count}回 [{step}] {err}")
    print()

    candidates = [(step, err, c) for (step, err), c in patterns.items() if c >= min_count]
    if not candidates:
        print(f"Issue化対象: 0件（{min_count}回以上のパターンなし）")
        return 0

    print(f"### Issue化対象: {len(candidates)}件（{min_count}回以上）")
    print()

    for step, err, count in candidates:
        details = _select_details(records, step, err)
        print(f"#### パターン: [{step}] {err[:ERROR_PREVIEW_LEN]}")
        print(f"- 発生回数: {count}回")
        print("- 発生例:")
        for line in details:
            print(line)
        print()

        if create_issues and not dry_run:
            _maybe_create_issue(step=step, err=err, count=count, days=days, details=details)

    if dry_run:
        print("（--dry-run モード: Issue は作成されません）")
    return 0


# ── 公開 API ────────────────────────────────────────────────────────────────


def build_issue_title(step: str, err: str) -> str:
    safe_step = _strip_control(step)
    safe_err = _strip_control(err[:50])
    return f"fix: 自律ループ異常「{safe_step}: {safe_err}」を根本解消する"


def build_issue_body(step: str, err: str, count: int, days: int, details: list[str]) -> str:
    err_50 = err[:50]
    err_80 = err[:ERROR_PREVIEW_LEN]
    err_100 = err[:KEY_LEN]
    details_block = "\n".join(details) if details else ""
    msg = (
        f"「{step}」ステップで「{err_50}」が根本解消できないせいで、"
        f"過去 {days} 日間で {count} 回繰り返し中断・エスカレーションして"
        f"人間が毎回介入しなければならない状況が続いている。"
    )
    return f"""## 背景

{msg}

発生ステップ: `{step}`
エラーパターン: `{err_100}`

## やること

- [ ] エラーパターン「{err_80}」の根本原因を調査する
- [ ] 再発防止策を実装する
- [ ] 必要に応じてログのサニタイズルールを更新する

## 振る舞い

Feature: 自律ループ異常の根本原因解消

  Scenario: 修正後にエラーが再発しない
    Given エラー「{err_50}」の修正が実装されている
    When 自律ループ（ai-review.sh または issue-next）が実行される
    Then 該当パターンのエラーが発生しない
    And ログに新規エラーが記録されない

  Scenario: 異常ケースが適切にハンドリングされる
    Given 修正前と同じ状況が発生する
    When エラーが発生する
    Then エラーが適切にハンドリングされ、ループが継続または適切に終了する

## 設計の選択肢

| 案 | 採用 | 理由 |
|----|------|------|
| エラーを無視する | ❌ | 問題が繰り返し発生し続ける |
| エラーを根本的に修正する | ✅ | 再発防止効果が高い |

## 発生ログ（直近3件）

```
{details_block}
```

---
*自動生成: analyze-loop-errors による週次ログ分析結果（発生回数: {count}回）*"""


# ── 内部 ──────────────────────────────────────────────────────────────────


def _resolve_log_dir(arg_log_dir: str | None) -> Path:
    if arg_log_dir:
        return Path(arg_log_dir).expanduser()
    env = os.environ.get("LOOP_ERROR_LOG_DIR")
    if env:
        return Path(env).expanduser()
    return _cache_dir() / DEFAULT_LOG_DIRNAME


def _current_repo() -> str | None:
    """origin の GitHub URL から現在のリポジトリ名を解決する（失敗時は None）。"""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except Exception:  # noqa: BLE001 - 分析対象を安全側（空）にする
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    match = _REMOTE_REPO_RE.search(url) or _SSH_ALIAS_REPO_RE.match(url)
    return match.group(1) if match else None


def _collect_records(log_dir: Path, cutoff_date: str) -> list[dict[str, Any]]:
    """`*.jsonl` ファイルを集約してレコード一覧を返す.

    ファイル名が `errors-YYYY-MM-DD.jsonl` の場合は cutoff_date 以降のみ採用。
    日付なしファイルは timestamp フィールドで個別フィルタリングする。
    """
    out: list[dict[str, Any]] = []
    for jsonl in sorted(log_dir.glob("*.jsonl")):
        file_date = _parse_file_date(jsonl.name)
        try:
            content = jsonl.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("ログファイル読み込み失敗: %s (%s)", jsonl, exc)
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if file_date is not None:
                if file_date < cutoff_date:
                    continue
                out.append(rec)
            else:
                ts = rec.get("timestamp", "")
                if not isinstance(ts, str):
                    continue
                if ts[:10] >= cutoff_date:
                    out.append(rec)
    return out


def _parse_file_date(name: str) -> str | None:
    """`errors-YYYY-MM-DD.jsonl` から日付文字列を抽出する."""
    if not name.startswith("errors-") or not name.endswith(".jsonl"):
        return None
    candidate = name[len("errors-") : -len(".jsonl")]
    if len(candidate) != len("YYYY-MM-DD"):
        return None
    parts = candidate.split("-")
    if len(parts) != 3:
        return None
    if not all(p.isdigit() for p in parts):
        return None
    return candidate


def _normalize_error_for_grouping(err: str) -> str:
    """集約・dedup key 用にエラーメッセージを正規化する（Issue #3867）.

    改行除去 → `#<数字>`（PR番号・Issue番号等の可変部分）を `#N` へマスク →
    先頭 `KEY_LEN` 文字へ切り詰め、の順に適用する。同一バグでも PR 番号が異なる
    だけで別パターンとして分裂集計されるのを防ぐ（実例: Issue #3852〜#3865 の分裂起票）。
    """
    flat = err.replace("\n", " ").replace("\r", " ")
    masked = _NUMBER_REF_RE.sub("#N", flat)
    return masked[:KEY_LEN]


def _aggregate_patterns(records: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    """(step, 正規化後 error[:100]) 単位で発生回数を集計し、件数降順で返す."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for r in records:
        step = r.get("step")
        err = r.get("error")
        if not isinstance(step, str) or not isinstance(err, str):
            continue
        normalized = _normalize_error_for_grouping(err)
        counts[(step, normalized)] += 1
    # 件数降順で並べ替え（同数は step / err 昇順で安定化）
    sorted_items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
    return dict(sorted_items)


def _select_details(records: list[dict[str, Any]], step: str, err_key: str) -> list[str]:
    """発生例を最大3件、`  - timestamp PR#... [step] error[:80]` 形式で返す."""
    out: list[str] = []
    for r in records:
        if r.get("step") != step:
            continue
        err = r.get("error", "")
        if not isinstance(err, str):
            continue
        normalized = _normalize_error_for_grouping(err)
        if normalized != err_key:
            continue
        ts = r.get("timestamp", "")
        pr = r.get("pr_number") or "-"
        err_short = err[:ERROR_PREVIEW_LEN]
        out.append(f"  - {ts} PR#{pr} [{step}] {err_short}")
        if len(out) >= 3:
            break
    return out


def _strip_control(text: str) -> str:
    """旧 sh の `tr -d '\\`$\\\\'` 相当（コマンドインジェクション防止）."""
    return text.replace("`", "").replace("$", "").replace("\\", "")


def _maybe_create_issue(*, step: str, err: str, count: int, days: int, details: list[str]) -> None:
    title = build_issue_title(step, err)
    # 旧 sh の dedup_key は "自律ループ異常「<step>: <err[:40]>" の部分一致
    safe_step = _strip_control(step)
    safe_err40 = _strip_control(err[:50])[:40]
    dedup_key = f"自律ループ異常「{safe_step}: {safe_err40}"
    try:
        if existing_open_issue(dedup_key, repo=None):
            print(
                f"SKIP: 同一パターンのIssueが既に存在します（{safe_step}）。作成をスキップします。",
                file=sys.stderr,
            )
            return
    except Exception as exc:
        logger.warning("既存 Issue チェック失敗: %s", exc)

    template_body = build_issue_body(step=step, err=err, count=count, days=days, details=details)
    body = _enhance_body_with_llm(step=step, err=err, details=details, template_body=template_body)
    try:
        issue_create(title=title, body=body, labels=list(ISSUE_LABELS), repo=None)
        print("==> Issue を作成しました", file=sys.stderr)
    except Exception as exc:
        print(f"WARN: Issue の作成に失敗しました: {exc}", file=sys.stderr)


def _enhance_body_with_llm(*, step: str, err: str, details: list[str], template_body: str) -> str:
    """LLM で Issue 本文を強化する。フォールバック時は template_body をそのまま返す（Issue #1247）."""
    try:
        from tidd_tools.shared.llm_issue_body import enhance_issue_body
    except ImportError:
        return template_body
    repo_root = _resolve_repo_root()
    context = (
        f"# 自律ループで検出されたエラー\n\n"
        f"発生ステップ: {step}\n"
        f"エラー先頭 100 文字: {err[:100]}\n\n"
        f"## 直近発生ログ（最大 3 件）\n\n" + ("\n---\n".join(details) if details else "（ログ情報なし）")
    )
    result = enhance_issue_body(
        context=context,
        template_body=template_body,
        repo_root=repo_root,
    )
    if result.enhanced:
        print("==> LLM で Issue 本文を強化しました", file=sys.stderr)
    else:
        if result.reason == "no-key":
            print(
                "ANTHROPIC_API_KEY 未設定のためテンプレート生成にフォールバック",
                file=sys.stderr,
            )
        else:
            print(
                f"==> LLM 強化はスキップ（{result.reason}）。テンプレートを使用します。",
                file=sys.stderr,
            )
    return result.body


def _resolve_repo_root() -> Path:
    """git rev-parse --show-toplevel で repo root を返す（失敗時は CWD）."""
    proc = run_subprocess(
        ["git", "rev-parse", "--show-toplevel"],
        timeout=QUICK_TIMEOUT_SEC,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return Path.cwd()


if __name__ == "__main__":
    sys.exit(0)
