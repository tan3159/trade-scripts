"""verify-post-merge サブコマンド (Issue #1402).

マージ済み PR の ``[AI確認-post-merge]`` 項目を Agent tool 経由で自律検証するための
ラッパー実装。実際の検証（CircleCI API を叩く・cron routine 起動時の集約処理）は
Claude Code セッション内の Agent tool + subagent (`.claude/agents/post-merge-verifier.md`)
が担うため、本モジュールは以下の責務のみを持つ:

- PR ボディから未消化 ``[AI確認-post-merge]`` 項目を抽出する
- 検証済み項目を ``- [x]`` に更新した本文を生成する（副作用なし）
- CLI エントリポイントとして PR 番号と項目一覧を表示し、Claude Code 側の Agent tool
  呼び出しを促す（本モジュールから直接 Agent tool を起動することはできない）

実際の post-merge 検証手順は ``.claude/skills/verify-post-merge/SKILL.md`` を参照。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta

from tidd_tools.shared.cli import add_common_flags, check_dry_run_not_implemented

# `- [ ] [AI確認-post-merge] ...` を抽出する（先頭のインデント許容）。
POST_MERGE_LINE_RE = re.compile(r"^([ \t]*- \[)( )(\][ \t]*\[AI確認-post-merge\][^\n]*)$")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`tidd verify-post-merge <PR>` サブコマンドを登録する."""
    parser = subparsers.add_parser(
        "verify-post-merge",
        help="マージ済み PR の [AI確認-post-merge] 項目を Agent tool 経由で検証する (#1402)",
        description=(
            "指定した PR の ``[AI確認-post-merge]`` 項目一覧を表示し、Claude Code の "
            "Agent tool (post-merge-verifier subagent) 経由で検証するための情報を出力する。"
            "検証結果の PR ボディ更新・PR コメント投稿・失敗時の Issue 起票は SKILL 側で行う。"
        ),
    )
    parser.add_argument(
        "pr_num",
        nargs="?",
        help="検証対象の PR 番号（--list-candidates 指定時は省略可）",
    )
    parser.add_argument(
        "--list-candidates",
        action="store_true",
        help=(
            "マージ済みかつ 24h 以内（--within-hours で変更可）にマージされ、"
            "未消化 [AI確認-post-merge] 項目がある PR 番号を 1 行 1 件で出力する (#3638)"
        ),
    )
    parser.add_argument(
        "--within-hours",
        type=int,
        default=24,
        metavar="N",
        help="--list-candidates で対象とするマージからの経過時間（時間・デフォルト 24）",
    )
    # verify-post-merge は PR ボディの読み取り専用で副作用がないため dry_run_supported=False（Issue #2791）
    add_common_flags(parser, dry_run_supported=False)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    """`tidd verify-post-merge` の入り口.

    - `--list-candidates` 指定時は、マージ済みかつ 24h 以内で未消化
      `[AI確認-post-merge]` 項目がある PR 番号を列挙する（PR 番号の位置引数は不要）
    - PR 番号指定時は、PR ボディを取得して未消化 `[AI確認-post-merge]` 項目を stdout に列挙する
    - 実際の検証は SKILL に委ねる（stderr に案内を出力する）
    """
    if check_dry_run_not_implemented(args, subcmd="verify-post-merge"):
        return 2

    # 既存の step_defs 等が pr_num のみの Namespace を渡すため getattr で後方互換を保つ
    if getattr(args, "list_candidates", False):
        return run_list_candidates(within_hours=getattr(args, "within_hours", 24))

    if not args.pr_num:
        print(
            "ERROR: PR 番号を指定するか --list-candidates を指定してください",
            file=sys.stderr,
        )
        return 2

    pr_num = args.pr_num
    # gh は import 循環を避けるため関数内で遅延 import する（Anthropic SDK 直接呼び出し禁止 rule と同様）
    from tidd_tools.shared import gh_client as gh

    try:
        body = gh.pr_body(pr_num, None)
    except Exception as exc:  # pragma: no cover - gh CLI 未インストール環境等
        print(f"ERROR: PR #{pr_num} の本文取得に失敗しました: {exc}", file=sys.stderr)
        return 1

    pending = extract_pending_post_merge_items(body)
    if not pending:
        print(
            f"==> PR #{pr_num}: skip: no unchecked post-merge items"
            "（未消化の [AI確認-post-merge] 項目なし。正常終了します）",
            file=sys.stderr,
        )
        return 0

    print(
        f"==> PR #{pr_num} に {len(pending)} 件の [AI確認-post-merge] 項目が残っています:",
        file=sys.stderr,
    )
    for item in pending:
        print(f"  - {item}", file=sys.stderr)
    print(
        "==> 実際の検証は .claude/skills/verify-post-merge/SKILL.md の手順に従って "
        "Claude Code の Agent tool (post-merge-verifier subagent) で行ってください。",
        file=sys.stderr,
    )

    # 各項目を stdout に 1 行 1 項目で出力し、SKILL 側から parse しやすくする。
    for item in pending:
        print(item)
    return 0


def run_list_candidates(*, within_hours: int) -> int:
    """マージ済みかつ `within_hours` 以内で未消化 `[AI確認-post-merge]` 項目がある PR 番号を列挙する.

    closed PR を取得し、以下の 3 条件をすべて満たす PR 番号のみを stdout に 1 行 1 件で出力する:

    - ``mergedAt`` が非 null（マージ済み。未マージ close は除外）
    - ``mergedAt`` が現在時刻から ``within_hours`` 時間以内
    - body に ``- [ ] [AI確認-post-merge]`` 行が 1 件以上ある（全項目 tick 済みは除外）

    候補 0 件のときは何も出力せず exit 0 を返す。API 取得失敗時は stderr にエラーを出して
    exit 1 を返す（Issue #3638）。
    """
    # gh は import 循環を避けるため関数内で遅延 import する
    from tidd_tools.shared import gh_client as gh

    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=within_hours)

    try:
        prs = gh.pr_list(state="closed", limit=100, fields=("number", "mergedAt", "body"))
    except Exception as exc:
        print(f"ERROR: PR 一覧の取得に失敗しました: {exc}", file=sys.stderr)
        return 1

    for pr in prs:
        number = pr.get("number")
        if number is None:
            continue
        merged_at = _parse_iso_datetime(pr.get("mergedAt"))
        if merged_at is None or merged_at < cutoff:
            continue
        body = pr.get("body") or ""
        if not extract_pending_post_merge_items(body):
            continue
        print(number)
    return 0


def _parse_iso_datetime(value: str | None) -> datetime | None:
    """GitHub API の ISO 8601 タイムスタンプ（``...Z`` 形式）を aware datetime に変換する.

    空・null・パース不能な値は None を返す（呼び出し側で「非マージ」として除外する）。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def extract_pending_post_merge_items(body: str) -> list[str]:
    """PR ボディから ``- [ ] [AI確認-post-merge] ...`` 行のプレフィックス除去済みテキストを返す.

    - ``- [x]`` (完了) は除外する
    - `## Test plan` 以外のセクションに書かれていても検出する（cron 検証では
      セクション位置を問わない・SKILL 側で誤検出があれば手動で除外する運用）
    """
    result: list[str] = []
    for line in body.splitlines():
        match = POST_MERGE_LINE_RE.match(line)
        if match:
            # match.group(3) は "] [AI確認-post-merge] ..." で、先頭の "] " を剥がす
            text = match.group(3).lstrip("]").strip()
            result.append(text)
    return result


def mark_post_merge_item_done(body: str, item_text: str, tracking_issue: int | str | None = None) -> str:
    """PR ボディ内の ``- [ ] [AI確認-post-merge] <item_text>`` を ``- [x]`` に置換する.

    - `item_text` は ``extract_pending_post_merge_items`` が返した stripped 済みテキスト
      （例: ``[AI確認-post-merge] nightly が GREEN``）
    - 該当行が見つからなければ body をそのまま返す（idempotent）
    - `tracking_issue` を指定すると ``（→ Issue #<N> で追跡中）`` の注記を末尾に付与する。
      検証失敗で Issue を起票した項目を ``- [x]`` に更新して cron の再検出
      （同内容 Issue の量産）を防ぐために使う (Issue #1967)
    """
    out: list[str] = []
    replaced = False
    for line in body.splitlines():
        match = POST_MERGE_LINE_RE.match(line)
        if match and not replaced:
            text = match.group(3).lstrip("]").strip()
            if text == item_text:
                note = f"（→ Issue #{tracking_issue} で追跡中）" if tracking_issue is not None else ""
                out.append(f"{match.group(1)}x{match.group(3)}{note}")
                replaced = True
                continue
        out.append(line)
    result = "\n".join(out)
    if body.endswith("\n"):
        result += "\n"
    return result
