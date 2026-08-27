"""`tidd ensure-agents-skills` サブコマンド.

Codex 用の `.agents/skills` → `.claude/skills` の symlink 生成をスクリプト化する
（Issue #3197・docs/reference/codex-interop.md §3-2 / §5 Phase 2）。

- `.agents/skills` が存在しない → `.claude/skills` への相対 symlink を作成して exit 0
- symlink が既に存在する → 何もせず exit 0（冪等）
- symlink でない実体が存在する → 上書きせずエラーで exit 1
- `--check` 指定時 → 検証のみ（副作用なし）。正しい symlink なら exit 0、欠落・不正なら exit 1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tidd_tools.shared import git_client
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GitCommandError

AGENTS_SKILLS_RELPATH = Path(".agents") / "skills"
# `.agents/` から見た `.claude/skills` への相対パス（codex-interop.md §3-2 の
# `ln -s ../.claude/skills .agents/skills` と同じ）
SYMLINK_TARGET = Path("../.claude/skills")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "ensure-agents-skills",
        help=".agents/skills → .claude/skills の symlink を生成する（Codex interop・Issue #3197）",
        description=__doc__,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="検証のみ実行し symlink を作成しない（生存確認・Issue #3235）",
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def ensure(repo_root: Path) -> int:
    """リポジトリルートで `.agents/skills` symlink の状態を整える.

    Returns:
        0: 作成成功 or 既存 symlink で変更不要
        1: symlink でない実体が存在するため上書きせず終了
        2: symlink 作成に失敗（OS エラー）
    """
    target = repo_root / AGENTS_SKILLS_RELPATH
    if target.is_symlink():
        print("OK: .agents/skills は既に .claude/skills への symlink です（変更なし）")
        return 0
    if target.exists():
        print(
            "ERROR: .agents/skills が symlink でない実体として存在するため上書きしません",
            file=sys.stderr,
        )
        return 1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(SYMLINK_TARGET, target_is_directory=True)
    except OSError as exc:
        print(f"ERROR: .agents/skills の symlink 作成に失敗しました: {exc}", file=sys.stderr)
        return 2
    print("OK: .agents/skills -> .claude/skills の symlink を作成しました")
    return 0


def ensure_check(repo_root: Path) -> int:
    """検証専用（副作用なし）で `.agents/skills` symlink の生存を確認する.

    Returns:
        0: `.agents/skills` が `.claude/skills` への symlink として存在する
        1: 存在しない・symlink でない実体・ターゲット不一致
    """
    target = repo_root / AGENTS_SKILLS_RELPATH
    if target.is_symlink():
        if target.readlink() == SYMLINK_TARGET:
            print("OK: .agents/skills は .claude/skills への symlink です")
            return 0
        print(
            "ERROR: .agents/skills が .claude/skills 以外を指しています",
            file=sys.stderr,
        )
        return 1
    if target.exists():
        print(
            "ERROR: .agents/skills が symlink でない実体として存在します",
            file=sys.stderr,
        )
        return 1
    print("ERROR: .agents/skills symlink が存在しません", file=sys.stderr)
    return 1


def run_cli(args: argparse.Namespace) -> int:
    try:
        repo_root = git_client.rev_parse_show_toplevel()
    except GitCommandError as exc:
        print(f"ERROR: git rev-parse に失敗しました: {exc}", file=sys.stderr)
        return 2
    if args.check:
        return ensure_check(Path(repo_root))
    return ensure(Path(repo_root))
