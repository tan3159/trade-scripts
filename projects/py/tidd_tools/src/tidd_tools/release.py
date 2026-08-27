"""`tidd release` サブコマンド（Issue #3416）.

リリース作業（CalVer タグ発行・GitHub Release 作成）が完全手動だったため、直近では
最新タグから 629 コミットが溜まり、consumer への配布が 3 週間止まっていた（#3414）。
本サブコマンドは既存 CLI（`check_template_drift.py` 等）の流儀を踏襲し、完全ローカル
実行でタグ発行〜GitHub Release 作成を自動化する（追加費用ゼロ・GitHub Actions は
本リポジトリで撤去済み・#1224 の方針に整合）。

実行フロー:
1. `git status --porcelain` で working tree がクリーンか確認（差分あれば exit 1）
2. 現在ブランチが main か確認（違えば exit 1）
3. `git fetch origin --tags` 後、HEAD が origin/main と一致するか確認（違えば exit 1）
4. `priority: critical` / `priority: high` の OPEN Issue 件数を stderr に警告表示
   （ブロックはしない・`--strict` 指定時のみ 1 件以上で exit 1）
5. タグ名 `v<YYYY.MM.DD>` を生成（同日タグが既存なら `v<YYYY.MM.DD>.1` の連番サフィックス）
6. `git tag <tag> && git push origin <tag>`
7. `gh release create <tag> --generate-notes` で GitHub Release を作成

`--dry-run` はタグ名と実行予定コマンドを stdout に表示し、push・Release 作成は行わない。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
import sys

from tidd_tools.shared import gh_client
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.subprocess_runner import run as run_subprocess

# priority 警告対象ラベル（#3416）。
_PRIORITY_LABELS = ("priority: critical", "priority: high")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "release",
        help="CalVer タグ発行と GitHub Release 作成を自動化する（#3416）",
        description=__doc__,
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="priority: critical/high の OPEN Issue が 1 件以上ある場合に exit 1 でブロックする",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="対象リポジトリ (owner/repo)。省略時は gh がカレントディレクトリから自動検出する",
    )
    add_common_flags(parser)
    parser.set_defaults(func=_handle)


def _handle(args: argparse.Namespace) -> int:
    return run(dry_run=args.dry_run, strict=args.strict, repo=args.repo)


# ── git ラッパー ──────────────────────────────────────────────────────────


def _git(args: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return run_subprocess(["git", *args], capture=True, timeout=timeout)


def is_working_tree_clean() -> bool:
    """`git status --porcelain` の出力が空かどうかを返す."""
    result = _git(["status", "--porcelain"])
    return result.returncode == 0 and result.stdout.strip() == ""


def current_branch() -> str:
    """現在チェックアウト中のブランチ名を返す（detached HEAD なら "HEAD"）."""
    result = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    return result.stdout.strip()


def fetch_tags() -> bool:
    """`git fetch origin --tags` を実行する."""
    result = _git(["fetch", "origin", "--tags"], timeout=60)
    return result.returncode == 0


def head_matches_origin_main() -> bool:
    """ローカル HEAD が `origin/main` と一致するかどうかを返す."""
    head = _git(["rev-parse", "HEAD"]).stdout.strip()
    origin_main = _git(["rev-parse", "origin/main"]).stdout.strip()
    return bool(head) and head == origin_main


def existing_tags() -> list[str]:
    """既存タグ一覧（`git tag -l`）を返す."""
    result = _git(["tag", "-l"])
    return [t for t in result.stdout.splitlines() if t.strip()]


def create_and_push_tag(tag: str) -> bool:
    """`git tag <tag> && git push origin <tag>` を実行する."""
    result = _git(["tag", tag])
    if result.returncode != 0:
        return False
    result = _git(["push", "origin", tag], timeout=60)
    return result.returncode == 0


# ── priority Issue 警告（#3416） ────────────────────────────────────────────


def count_open_priority_issues(repo: str | None) -> int | None:
    """`priority: critical` / `priority: high` の OPEN Issue 件数の合計を返す.

    `gh issue list --label a --label b` は複数 `--label` を AND 条件（両方の
    ラベルを持つ Issue のみ）として扱う。priority ラベルは 1 Issue につき 1 つの
    排他運用のため、単純に AND で問い合わせると常に 0 件になってしまう。
    critical / high をそれぞれ個別に問い合わせて合算することで、リリース前に
    確認したい「高優先度の残 Issue 件数」という意図（#3414）を実現する。

    取得失敗時は None を返す（fail-soft・呼び出し元が警告して続行する）。
    """
    total = 0
    for label in _PRIORITY_LABELS:
        args = ["issue", "list", "--label", label, "--state", "open", "--json", "number"]
        if repo:
            args += ["--repo", repo]
        try:
            issues = gh_client.gh_json(args)
        except GhCommandError as exc:
            print(f"WARNING: priority Issue 件数の取得に失敗しました: {exc}", file=sys.stderr)
            return None
        total += len(issues) if isinstance(issues, list) else 0
    return total


# ── タグ名生成（#3416） ──────────────────────────────────────────────────


def _today() -> _dt.date:
    return _dt.date.today()


def generate_tag_name(today: _dt.date, tags: list[str]) -> str:
    """`v<YYYY.MM.DD>` 形式のタグ名を生成する。同日タグ既存時は `.1` から連番を振る."""
    tagset = set(tags)
    base = f"v{today.year:04d}.{today.month:02d}.{today.day:02d}"
    if base not in tagset:
        return base
    n = 1
    while f"{base}.{n}" in tagset:
        n += 1
    return f"{base}.{n}"


# ── GitHub Release 作成 ──────────────────────────────────────────────────


def create_github_release(tag: str, repo: str | None) -> bool:
    """`gh release create <tag> --generate-notes` を実行する."""
    args = ["release", "create", tag, "--generate-notes"]
    if repo:
        args += ["--repo", repo]
    result = run_subprocess(["gh", *args], capture=True, timeout=60)
    return result.returncode == 0


# ── メインフロー ──────────────────────────────────────────────────────────


def run(*, dry_run: bool, strict: bool, repo: str | None) -> int:
    if not is_working_tree_clean():
        print(
            "ERROR: working tree に未コミットの変更があります。コミットまたは stash してください。",
            file=sys.stderr,
        )
        return 1

    branch = current_branch()
    if branch != "main":
        print(f"ERROR: main ブランチではありません（現在のブランチ: {branch}）。", file=sys.stderr)
        return 1

    if not fetch_tags():
        print("ERROR: git fetch origin --tags に失敗しました。", file=sys.stderr)
        return 1

    if not head_matches_origin_main():
        print(
            "ERROR: HEAD が origin/main と一致しません。git pull で最新化してください。",
            file=sys.stderr,
        )
        return 1

    priority_count = count_open_priority_issues(repo)
    if priority_count is None:
        print("WARNING: priority Issue 件数を確認できませんでした（続行します）。", file=sys.stderr)
    elif priority_count > 0:
        print(
            f"WARNING: priority: critical/high の OPEN Issue が {priority_count} 件あります。",
            file=sys.stderr,
        )
        if strict:
            print("ERROR: --strict 指定のため中断します。", file=sys.stderr)
            return 1

    tag = generate_tag_name(_today(), existing_tags())

    if dry_run:
        print(f"[dry-run] tag: {tag}")
        print(f"[dry-run] git tag {tag} && git push origin {tag}")
        release_cmd = f"gh release create {tag} --generate-notes"
        if repo:
            release_cmd += f" --repo {repo}"
        print(f"[dry-run] {release_cmd}")
        return 0

    if not create_and_push_tag(tag):
        print(f"ERROR: タグ {tag} の作成・push に失敗しました。", file=sys.stderr)
        return 1

    if not create_github_release(tag, repo):
        print(f"ERROR: GitHub Release {tag} の作成に失敗しました。", file=sys.stderr)
        return 1

    print(f"OK: {tag} のタグと GitHub Release を作成しました。")
    return 0
