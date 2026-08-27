"""`tidd check-template-drift` サブコマンド (Issue #1476).

`.claude/**` の変更が同一パスの `templates/workflow/.claude/**` にも
反映されているかを検証する。同期漏れを CI (nightly) で fail-loud にする。

`<!-- template-drift-exempt: <理由> -->` marker で override 可能。

stdlib のみ使用。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from tidd_tools import sync_template
from tidd_tools.shared import gh_client

DRIFT_EXEMPT_MARKER_RE = re.compile(
    r"<!--\s*template-drift-exempt\s*:\s*(.+?)\s*-->",
    re.DOTALL,
)

#: `templates/workflow` はテンプレート提供リポジトリ（本リポジトリ）にしか存在しない。
#: consumer 文脈ではこのメッセージを stderr に出力し exit 0 で no-op する（Issue #3982）。
_MAINTAINER_ONLY_MESSAGE = (
    "==> tidd check-template-drift: 本サブコマンドはテンプレート提供リポジトリ専用です"
    "（templates/workflow/ が見つからないため対象外・consumer では no-op）。"
)


def _find_repo_root(start: Path) -> Path | None:
    for c in [start, *start.parents]:
        if (c / ".claude").is_dir() and (c / "templates" / "workflow").is_dir():
            return c
    return None


def _get_pr_body(pr_num: str | int, repo: str | None) -> str:
    return gh_client.pr_body(pr_num, repo=repo)


def _get_pr_diff_files(pr_num: str | int, repo: str | None) -> list[str]:
    return gh_client.pr_diff_files(pr_num, repo=repo)


def find_drifted_files(changed_files: list[str], repo_root: Path) -> list[tuple[str, str]]:
    """`.claude/**` の変更ファイルについて対応する templates/workflow/.claude/** を検証する.

    Returns:
        drift しているファイルのリスト: (claude_path, expected_template_path) のタプル
    """
    drifts: list[tuple[str, str]] = []
    claude_changed = {f for f in changed_files if f.startswith(".claude/")}
    template_changed = {f for f in changed_files if f.startswith("templates/workflow/.claude/")}

    for claude_path in sorted(claude_changed):
        expected = "templates/workflow/" + claude_path
        # templates/workflow 側に対応 file が実存する場合のみ drift 判定
        # (対応 file がない = template distribution 対象外 → 除外)
        target = repo_root / expected
        if not target.is_file():
            continue
        if expected not in template_changed:
            drifts.append((claude_path, expected))
    return drifts


def find_static_drift(repo_root: Path) -> list[str]:
    """`templates/workflow/.claude/**` の全ファイルを対応する `.claude/**` と内容比較する (Issue #3437).

    PR diff ベースの `find_drifted_files` と異なり、PR diff に依存せず
    現在の main 上の実ファイル内容を直接比較するため、過去の同期漏れ
    （PR diff チェックの網から漏れた既存 drift）も継続的に検知できる。

    `sync_template.find_sync_targets`（Issue #3417）に委譲することで、
    以下の除外ロジックを重複実装せず再利用する（Issue #3598）:

    - `__pycache__/` 配下・`.pyc`/`.pyo`（gitignore 対象の Python バイトコード
      キャッシュ。CI 実行順序上 pytest ステップの後に本チェックが走るため、
      import 内容次第で非決定的に生成され false positive の原因になっていた）
    - `templates/workflow/_copier/sync-exempt.yaml` 登録済みパス（consumer 向け
      配布物として意図的に書き換え済みの `.claude/rules/*.md` 等）

    Returns:
        drift しているファイルのリスト（`.claude/**` からの相対パス文字列、ソート済み）。
    """
    return sync_template.find_sync_targets(repo_root)


def run_static(*, repo_root: Path | None = None) -> int:
    """`--static`: 現在の main 上での実ファイル内容差分を全件スキャンする (Issue #3437)."""
    root = repo_root if repo_root is not None else _find_repo_root(Path.cwd())
    if root is None:
        print(_MAINTAINER_ONLY_MESSAGE, file=sys.stderr)
        return 0
    template_claude_root = root / "templates" / "workflow" / ".claude"
    if not template_claude_root.is_dir():
        print(
            f"==> ERROR: templates directory not found: {template_claude_root}",
            file=sys.stderr,
        )
        return 2

    drifts = find_static_drift(root)
    if not drifts:
        return 0

    print("==> ERROR: template drift detected (static scan)", file=sys.stderr)
    for path in drifts:
        print(f"    template drift: {path} 同期漏れ", file=sys.stderr)
    return 1


def has_exempt_marker(pr_body: str) -> bool:
    """PR body に `<!-- template-drift-exempt: <理由> -->` marker があるか判定."""
    if not pr_body:
        return False
    m = DRIFT_EXEMPT_MARKER_RE.search(pr_body)
    if m is None:
        return False
    reason = m.group(1).strip()
    return bool(reason)


def run(*, pr_num: str, repo: str | None) -> int:
    repo_root = _find_repo_root(Path.cwd())
    if repo_root is None:
        print(_MAINTAINER_ONLY_MESSAGE, file=sys.stderr)
        return 0
    changed_files = _get_pr_diff_files(pr_num, repo)
    if not changed_files:
        print(f"==> no diff files for PR #{pr_num}", file=sys.stderr)
        return 0

    drifts = find_drifted_files(changed_files, repo_root)
    if not drifts:
        print(f"==> PR #{pr_num}: no template drift detected", file=sys.stderr)
        return 0

    pr_body = _get_pr_body(pr_num, repo)
    if has_exempt_marker(pr_body):
        print(
            f"==> PR #{pr_num}: template-drift-exempt marker detected, allowing drift",
            file=sys.stderr,
        )
        return 0

    print("==> ERROR: template drift detected", file=sys.stderr)
    for claude_path, expected in drifts:
        print(
            f"    template drift: {expected} 同期漏れ (change in {claude_path} not mirrored)",
            file=sys.stderr,
        )
    print(
        "\n    Fix: templates/workflow 側にも同じ変更を適用するか、"
        "PR body に `<!-- template-drift-exempt: <理由> -->` を追加してください。",
        file=sys.stderr,
    )
    return 1


def _handle(args: argparse.Namespace) -> int:
    if args.static:
        return run_static()
    if args.pr_num is None:
        print("error: pr_num is required unless --static is given", file=sys.stderr)
        return 2
    return run(pr_num=str(args.pr_num), repo=args.repo or None)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "check-template-drift",
        help=(
            ".claude/** と templates/workflow/.claude/** の同期漏れを CI で検知する (Issue #1476)。"
            "テンプレート提供リポジトリ専用。templates/workflow が無いリポジトリでは no-op で exit 0 (Issue #3982)"
        ),
    )
    parser.add_argument("pr_num", nargs="?", default=None, help="対象 PR 番号 (--static 指定時は不要)")
    parser.add_argument("--repo", default=None, help="リポジトリ (owner/repo)")
    parser.add_argument(
        "--static",
        action="store_true",
        help="PR diff ではなく現在の main 上の実ファイル内容差分を全件スキャンする (Issue #3437)",
    )
    parser.set_defaults(func=_handle)
