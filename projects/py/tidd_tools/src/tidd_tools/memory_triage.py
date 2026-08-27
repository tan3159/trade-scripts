"""`tidd memory-triage` サブコマンド (Issue #3659).

AutoMem のメモリディレクトリ `~/.claude/projects/<repo 変換名>/memory/` にある
`type: project` エントリを対象に、① 最終更新が `--stale-days` 日超 ② 本文内の
Issue/PR 参照（`#N` / `PR #N`）が gh で CLOSED / MERGED であるものを自動照合し、
削除候補リストを stdout に出力する。**削除は行わず候補提示のみ**（削除は人間または
AI が確認して行う）。

メモリディレクトリの解決は `context_usage.default_projects_dir()` の変換則
（repo root の非英数字 → `-`）を再利用する。MEMORY.md は AutoMem が自動生成する
インデックスのため PR 差分に現れず、既存 context-budget の HARD gate（PR gate）の
管轄外。本コマンドは WARN 層（SessionStart 警告 `session-start-memory-warn`）と
併用する棚卸し候補層（詳細: docs/research/memory-index-lifecycle.md）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from tidd_tools.context_usage import default_projects_dir
from tidd_tools.shared import gh_client, git_client
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError, GitCommandError

DEFAULT_STALE_DAYS = 30

# frontmatter の type が project のエントリのみ対象（AutoMem の type は自由値）
ENTRY_TYPE_PROJECT = "project"

# 本文中の Issue/PR 参照（#N / PR #N）。PR 参照は "#" 単体参照とは分けて抽出する
# （merged PR を "PR #N" の形で表示するため・propose-step GREEN 提案の改善を反映）。
_ISSUE_REF_RE = re.compile(r"(?<![A-Za-z0-9])#(\d+)")
_PR_REF_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:PR|pull request)\s*#(\d+)")


@dataclass
class Candidate:
    """削除候補（name / 最終更新日付 / 候補理由のリスト）."""

    name: str
    date: str
    reasons: list[str]


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "memory-triage",
        help="AutoMem の project 型エントリの削除候補を提示する（Issue #3659・削除はしない）",
        description=(
            "~/.claude/projects/<repo 変換名>/memory/ の type: project エントリについて、"
            "最終更新が --stale-days 日超、または本文内の Issue/PR 参照（#N / PR #N）が"
            " gh で CLOSED / MERGED であるものを削除候補として stdout に出力する。"
            "削除は行わず候補提示のみ（削除は人間または AI が確認して行う）。"
        ),
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=DEFAULT_STALE_DAYS,
        help=f"最終更新がこの日数超の project 型エントリを候補にする（デフォルト {DEFAULT_STALE_DAYS}）",
    )
    parser.add_argument(
        "--memory-dir",
        type=Path,
        default=None,
        help="メモリディレクトリ（省略時は ~/.claude/projects/<repo 変換名>/memory）",
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def default_memory_dir(repo_root: Path) -> Path:
    """メモリディレクトリを repo root から導出する（context_usage の変換則を再利用）."""
    return default_projects_dir(repo_root) / "memory"


def parse_entry_type(text: str) -> str | None:
    """frontmatter（--- で挟まれた先頭ブロック）の type フィールドを返す.

    frontmatter が無い・type キーが無い場合は None を返す。
    """
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("type:"):
            return stripped[len("type:") :].strip()
    return None


def find_issue_refs(body: str) -> set[int]:
    """本文中の `#N` 形式の参照を抽出する（Issue/PR 共通番号空間）.

    `PR #N` / `pull request #N` は PR 参照として扱う。`#` の直前に英数字が
    ある参照（CSS 等）は対象外（負の後読みで除外）。
    """
    pr_numbers = {int(n) for n in _PR_REF_RE.findall(body)}
    issue_numbers = {int(n) for n in _ISSUE_REF_RE.findall(body)}
    return issue_numbers | pr_numbers


def reference_status(number: int) -> str | None:
    """#N が完了済み（MERGED PR / CLOSED Issue）なら説明文字列、未完了なら None を返す.

    gh は PR と Issue で同一番号空間を持つため、まず PR として MERGED を確認し、
    PR でない（`GhCommandError`）場合は Issue として CLOSED を確認する。
    gh 呼び出し失敗は silent skip（参照解決不能は候補理由にしない）。
    """
    try:
        data = gh_client.pr_view(number, fields=("state",))
        if data.get("state") == "MERGED":
            return f"PR #{number} MERGED"
        return None
    except GhCommandError:
        pass
    try:
        data = gh_client.issue_view(number, fields=("state",))
        if data.get("state") == "CLOSED":
            return f"#{number} CLOSED"
    except GhCommandError:
        pass
    return None


def collect_candidates(memory_dir: Path, stale_days: int) -> list[Candidate]:
    """project 型エントリの削除候補を収集する（削除はしない）.

    ① 最終更新が stale_days 日超 ② 本文内参照が CLOSED / MERGED の OR で候補化する。
    """
    now = dt.datetime.now(dt.UTC)
    candidates: list[Candidate] = []
    if not memory_dir.is_dir():
        return candidates
    for path in sorted(memory_dir.glob("*.md")):
        if path.name.lower() == "memory.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if parse_entry_type(text) != ENTRY_TYPE_PROJECT:
            continue
        try:
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
        except OSError:
            continue
        age_days = (now - mtime).days
        reasons: list[str] = []
        if age_days > stale_days:
            reasons.append(f"最終更新 {stale_days} 日超")
        for n in sorted(find_issue_refs(text)):
            status = reference_status(n)
            if status is not None:
                reasons.append(status)
        if reasons:
            candidates.append(Candidate(name=path.name, date=mtime.date().isoformat(), reasons=reasons))
    return candidates


def _print_candidates(candidates: list[Candidate], stale_days: int) -> None:
    """削除候補リストを stdout に出力する（人間が確認して削除する・候補提示のみ）."""
    if not candidates:
        print("削除候補なし")
        return
    print(f"削除候補（project 型・最終更新 {stale_days} 日超 or 参照 Issue/PR 完了）:")
    for c in candidates:
        print(f"  {c.name:<40} ({c.date}・{' / '.join(c.reasons)})")


def run_cli(args: argparse.Namespace) -> int:
    if args.memory_dir is not None:
        memory_dir = args.memory_dir
    else:
        try:
            repo_root = git_client.rev_parse_show_toplevel()
        except GitCommandError as exc:
            print(f"ERROR: git rev-parse に失敗しました: {exc}", file=sys.stderr)
            return 2
        memory_dir = default_memory_dir(repo_root)

    candidates = collect_candidates(memory_dir, args.stale_days)

    if args.json_output:
        print(
            json.dumps(
                [{"name": c.name, "date": c.date, "reasons": c.reasons} for c in candidates],
                ensure_ascii=False,
            )
        )
        return 0

    _print_candidates(candidates, args.stale_days)
    return 0
