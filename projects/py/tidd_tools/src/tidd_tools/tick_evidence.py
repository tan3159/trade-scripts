"""`tidd tick-evidence` サブコマンド（Issue #1921）.

Issue の `## やること` checkbox を手動 tick するとき、evidence コメントの投稿が
メモリ依存（任意）だと省略されうる。tick と evidence 投稿を 1 コマンドに束ねて
常に根拠付きで完了を記録する。

- evidence が空（空白のみ含む）→ exit 1 でブロック
- 未チェックの対象項目が本文に見つからない → exit 1（コメント投稿・本文更新なし）
- 正常系 → evidence 表コメントを投稿してから checkbox を `- [x]` に更新
"""

from __future__ import annotations

import argparse
import re
import sys

from tidd_tools.shared import gh_client as gh

_UNCHECKED_RE = re.compile(r"^\s*-\s*\[ \]\s")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "tick-evidence",
        help="Issue やること checkbox を evidence コメント付きで tick する（#1921）",
        description=(
            "Issue 本文の未チェック項目を `- [x]` に更新し、同時に evidence 表コメントを"
            "投稿する。evidence なしの tick を防ぐため evidence 引数は必須（空文字は exit 1）。"
        ),
    )
    parser.add_argument("issue", help="Issue 番号")
    parser.add_argument("item", help="tick 対象のやること項目テキスト（未チェック行への部分一致）")
    parser.add_argument("evidence", help="完了根拠（必須・空文字不可）")
    parser.add_argument("--repo", default=None, help="owner/repo（省略時はカレントリポジトリ）")
    parser.set_defaults(func=run_cli)


def _table_cell(text: str) -> str:
    """Markdown 表のセル用に pipe と改行をエスケープする."""
    return text.replace("|", "\\|").replace("\n", "<br>")


def format_evidence_comment(rows: list[tuple[str, str]]) -> str:
    """(やること, evidence) の組から evidence 表コメント本文を組み立てる."""
    lines = [
        "## やること消化 Evidence",
        "",
        "| やること | Evidence |",
        "|---------|----------|",
    ]
    lines.extend(f"| {_table_cell(item)} | {_table_cell(evidence)} |" for item, evidence in rows)
    return "\n".join(lines) + "\n"


def run_cli(args: argparse.Namespace) -> int:
    issue = str(args.issue)
    item: str = args.item
    evidence: str = args.evidence
    repo: str | None = args.repo

    if not evidence.strip():
        print("ERROR: evidence は必須です（空文字では tick できません）", file=sys.stderr)
        return 1

    if not item.strip():
        print("ERROR: item は必須です（空文字では tick できません）", file=sys.stderr)
        return 1

    body = str(gh.issue_view(issue, repo=repo, fields=("body",)).get("body") or "")
    lines = body.splitlines()
    target_idx: int | None = None
    for i, line in enumerate(lines):
        if _UNCHECKED_RE.match(line) and item in line:
            target_idx = i
            break
    if target_idx is None:
        print(f"ERROR: 項目が見つかりません（未チェックの checkbox 行が対象）: {item}", file=sys.stderr)
        return 1

    comment = format_evidence_comment([(item, evidence)])
    gh.issue_comment(issue, repo, comment)

    lines[target_idx] = lines[target_idx].replace("- [ ]", "- [x]", 1)
    new_body = "\n".join(lines)
    if body.endswith("\n"):
        new_body += "\n"
    gh.issue_edit_body(issue, repo, new_body)

    print(f"==> Issue #{issue} の項目を evidence 付きで tick しました: {item}")
    return 0
