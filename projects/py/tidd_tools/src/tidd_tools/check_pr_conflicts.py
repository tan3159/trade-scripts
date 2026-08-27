"""`tidd check-pr-conflicts` サブコマンド（旧 `scripts/check-pr-conflicts.sh` の Python 移植）.

オープン PR との競合チェック（Issue #204）。

終了コード:
- 0: 競合なし・並行 PR 上限未満
- 1: 変更ファイルがオープン PR のファイルと競合する
- 2: 並行 PR 数が上限（`MAX_PARALLEL_PRS`、既定 5）に達している
- 3: チェック自体が失敗（gh API エラー・JSON パースエラー等）
- 4: 指定 Issue に "closes #N" を含む OPEN PR が存在（--issue N + --explicit-target・単一番号/バッチモード）
- 5: 指定 Issue に "closes #N" を含む OPEN PR が存在（--issue N・--explicit-target 未指定・自動ループ）

環境変数:
- `MAX_PARALLEL_PRS` 並行 PR 数上限（デフォルト 5）
- `REPO` gh に渡すリポジトリ名（省略時は自動検出）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Iterator
from typing import Any

from tidd_tools import timing_log
from tidd_tools.shared import gh_client
from tidd_tools.shared.cli import add_common_flags, check_dry_run_not_implemented
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.issue_body import extract_closes_issues
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "check-pr-conflicts",
        help="OPEN PR との並行上限・ファイル競合をチェックする（旧 scripts/check-pr-conflicts.sh）",
        description=__doc__,
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="ファイル競合チェックをスキップして並行 PR 数の上限チェックのみ行う",
    )
    parser.add_argument(
        "--issue",
        type=int,
        default=None,
        metavar="N",
        help=(
            "指定した Issue 番号に対して 'closes #N' を含む OPEN PR が存在するか確認する（競合ありは exit 4/5 で終了）"
        ),
    )
    parser.add_argument(
        "--explicit-target",
        action="store_true",
        help=(
            "着手対象が /issue-next の引数で明示指定された（単一番号・バッチモード）。"
            "競合 PR あり時は exit 4 を返す（未指定＝自動ループは exit 5・Issue #3634）"
        ),
    )
    parser.add_argument(
        "changed_files",
        nargs="*",
        help="競合チェック対象のファイルパス",
    )
    # check-pr-conflicts は OPEN PR の読み取りのみで副作用がないため dry_run_supported=False（Issue #2791）
    add_common_flags(parser, dry_run_supported=False)
    parser.set_defaults(func=run_cli)


def _record_timing_boundary(args: argparse.Namespace) -> None:
    """計測境界を統一日誌へ自己記録する（Issue #3557）.

    `--count-only` は STEP 0 の並行 PR 上限チェック（`issue-next-session` キー）、
    `--issue N` は STEP 1.7 の競合チェック（`issue-<N>` キー）に対応する。
    `record_event_once_safe` は fail-open だが、想定外の例外でも本コマンドの
    exit code を変えないよう追加の try/except で握りつぶす（#3557 やること 1）。
    """
    try:
        if getattr(args, "count_only", False):
            timing_log.record_event_once_safe(
                "issue-next-session", "step0-pr-limit-check", "point", "check-pr-conflicts"
            )
        elif getattr(args, "issue", None) is not None:
            timing_log.record_event_once_safe(
                f"issue-{args.issue}", "step1.7-conflict-check", "point", "check-pr-conflicts"
            )
    except Exception:
        logger.debug("計測境界の自己記録に失敗しました（無視）", exc_info=True)


def run_cli(args: argparse.Namespace) -> int:
    if check_dry_run_not_implemented(args, subcmd="check-pr-conflicts"):
        return 2

    _record_timing_boundary(args)

    max_parallel = int(os.environ.get("MAX_PARALLEL_PRS", "5"))
    repo = os.environ.get("REPO") or None

    # --issue N: 指定 Issue に対して closes #N を含む OPEN PR を検索する
    issue_number: int | None = getattr(args, "issue", None)
    if issue_number is not None:
        try:
            prs_with_body = _fetch_open_prs_with_body(repo)
        except (GhCommandError, json.JSONDecodeError) as exc:
            print(f"エラー: gh pr list の取得に失敗しました: {exc}", file=sys.stderr)
            return 3
        conflict_pr = _find_issue_conflict(prs_with_body, issue_number)
        if conflict_pr is not None:
            print(
                f"競合: PR #{conflict_pr} の body に 'closes #{issue_number}' が含まれています。",
                file=sys.stdout,
            )
            if getattr(args, "explicit_target", False):
                # 単一番号・バッチモード: 明示指定された Issue は着手できないため報告して終了
                print(
                    f"PR #{conflict_pr} に closes #{issue_number} が含まれるため着手できません",
                    file=sys.stderr,
                )
                return 4
            # 自動ループ: この Issue をスキップして次候補へ進む
            print(
                f"PR #{conflict_pr} に closes #{issue_number} が含まれるため Issue #{issue_number} をスキップします",
                file=sys.stderr,
            )
            return 5
        return 0

    try:
        prs = _fetch_open_prs(repo)
    except GhCommandError as exc:
        print(f"エラー: gh pr list の取得に失敗しました: {exc}", file=sys.stderr)
        return 3
    except json.JSONDecodeError as exc:
        print(f"エラー: JSON の解析に失敗しました: {exc}", file=sys.stderr)
        return 3

    current_branch = _current_branch()
    filtered = [pr for pr in prs if pr.get("headRefName") != current_branch]
    pr_count = len(filtered)

    if pr_count >= max_parallel:
        print(
            f"並行 PR が上限（{max_parallel}件）に達しています。マージ後に再実行してください。",
            file=sys.stdout,
        )
        return 2

    if args.count_only:
        return 0
    if not args.changed_files:
        return 0

    conflict = _find_real_conflict(filtered, list(args.changed_files), repo)
    if conflict is not None:
        pr_num, path = conflict
        print(
            f"競合: PR #{pr_num} が変更しているファイル '{path}' と重複します。",
            file=sys.stdout,
        )
        return 1
    return 0


# ── 内部 ────────────────────────────────────────────────────────────────────


def _fetch_open_prs_with_body(repo: str | None) -> list[dict[str, Any]]:
    """gh pr list で OPEN PR を取得し、number・body・headRefName の dict 一覧を返す."""
    return gh_client.pr_list(repo=repo, state="open", limit=50, fields=("number", "headRefName", "body"))


def _find_issue_conflict(prs: list[dict[str, Any]], issue_number: int) -> int | None:
    """OPEN PR の body に `issue_number` への closes/fixes/resolves 参照が含まれる PR 番号を返す（なければ None）.

    抽出は `shared.issue_body.extract_closes_issues()` に委譲する（closes/fixes/fix/resolves を
    大文字小文字無視で受理・#3550）。
    """
    for pr in prs:
        body = pr.get("body") or ""
        if issue_number in extract_closes_issues(body):
            number = pr.get("number")
            if isinstance(number, int):
                return number
    return None


def _fetch_open_prs(repo: str | None) -> list[dict[str, Any]]:
    """gh pr list で OPEN PR を取得し、files 付きの dict 一覧を返す."""
    return gh_client.pr_list(repo=repo, state="open", limit=50, fields=("number", "headRefName", "files"))


def _current_branch() -> str:
    """`git branch --show-current` の結果を返す（取得失敗時は空文字）."""
    try:
        result = run_subprocess(["git", "branch", "--show-current"])
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _iter_file_conflicts(prs: list[dict[str, Any]], changed_files: list[str]) -> Iterator[tuple[int, str]]:
    """OPEN PR の files と変更ファイル一覧が一致する (pr_num, path) を順にイテレートする（Issue #3925）."""
    targets = set(changed_files)
    for pr in prs:
        number = pr.get("number")
        for f in pr.get("files") or []:
            path = f.get("path", "")
            if path and path in targets and isinstance(number, int):
                yield number, path


def _find_conflict(prs: list[dict[str, Any]], changed_files: list[str]) -> tuple[int, str] | None:
    """OPEN PR の files と変更ファイル一覧を突合する（ファイルパス単位・最初の一致を返す）."""
    for match in _iter_file_conflicts(prs, changed_files):
        return match
    return None


def _find_real_conflict(
    prs: list[dict[str, Any]], changed_files: list[str], repo: str | None
) -> tuple[int, str] | None:
    """`_find_conflict` に加え、意味的に非衝突な純追記を除外した実質的な競合を返す（Issue #3925）.

    ファイルパスが一致しても、ローカル変更と競合候補 PR の両方が同一ファイルへの
    純粋な追記（削除行なし）かつ追加内容が重複しない場合は「意味的に衝突しない」と
    判定してスキップする（`_is_addition_only_disjoint`）。それ以外（削除を伴う変更・
    追加内容の重複・patch 取得失敗）は保守的に競合として扱う。
    """
    for number, path in _iter_file_conflicts(prs, changed_files):
        if _is_addition_only_disjoint(number, path, repo):
            continue
        return number, path
    return None


def _is_addition_only_disjoint(pr_num: int, path: str, repo: str | None) -> bool:
    """PR とローカル変更が同一ファイルへの非重複な純追記であれば True（意味的に非衝突）を返す（Issue #3925）.

    いずれかの patch が削除行を含む、取得に失敗した、追加行が空、または追加内容が
    重複する場合は保守的に False（競合扱い）を返す。
    """
    pr_patch = gh_client.pr_file_patch(pr_num, path, repo)
    local_patch = _local_file_patch(path)
    if pr_patch is None or local_patch is None:
        return False
    pr_added, pr_has_deletions = _parse_patch_additions(pr_patch)
    local_added, local_has_deletions = _parse_patch_additions(local_patch)
    if pr_has_deletions or local_has_deletions:
        return False
    if not pr_added or not local_added:
        return False
    return not (pr_added & local_added)


def _parse_patch_additions(patch: str) -> tuple[set[str], bool]:
    """unified diff テキストから追加行の内容集合と削除行の有無を返す（Issue #3925）.

    `+++`/`---` のファイルヘッダ行は除外する。削除行（`-` 始まり、ヘッダ除く）が
    1行でもあれば `has_deletions=True` を返す。
    """
    added: set[str] = set()
    has_deletions = False
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.add(line[1:])
        elif line.startswith("-"):
            has_deletions = True
    return added, has_deletions


def _local_file_patch(path: str) -> str | None:
    """`git diff origin/main -- <path>` でローカルブランチの unified diff patch を取得する（Issue #3925）.

    取得失敗（非 0 終了・`OSError`）時は None を返す（呼び出し側は保守的に競合として扱う）。
    """
    try:
        result = run_subprocess(["git", "diff", "origin/main", "--", path])
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout
