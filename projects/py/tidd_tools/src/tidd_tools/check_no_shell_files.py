"""`tidd check-no-shell-files` サブコマンド.

親 #1090 の最終ゲート（#1091）: `git ls-files "*.sh" "*.bats"` の出力が空であることを assert する。
CircleCI から呼ばれて Phase 4 完了後に sh / bats の残存をブロックする想定。

- 出力が空 → exit 0
- 出力が非空 → exit 1（ファイル一覧を stderr に出力）
- 環境変数 `ALLOW_SHELL_REMAIN=1` で結果を warning に格下げ（移行期間中の CI 用）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from tidd_tools.shared import git_client
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GitCommandError
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)

BYPASS_ENV = "ALLOW_SHELL_REMAIN"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "check-no-shell-files",
        help="git ls-files に *.sh / *.bats が残っていないことを確認する（親 #1090 最終ゲート）",
        description=(
            '`git ls-files "*.sh" "*.bats"` の出力が空であることを assert する。'
            "親 #1090（sh / bats 完全廃止プロジェクト）の最終ゲートとして CircleCI から呼ばれる。"
        ),
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    try:
        repo_root = git_client.rev_parse_show_toplevel()
    except GitCommandError as exc:
        print(f"ERROR: git rev-parse に失敗しました: {exc}", file=sys.stderr)
        return 2

    try:
        # NOTE: `*.sh` / `*.bats` のワイルドカードはシェル展開ではなく
        # `git ls-files` 側のパス pattern として解釈される（shell=False で渡しているため
        # シェル展開は走らない）。`git ls-files` は内部で `wildmatch` を使って自身で
        # 展開するので、引数を文字列のままサブプロセスに渡してよい。
        result = run_subprocess(
            ["git", "ls-files", "*.sh", "*.bats"],
            cwd=repo_root,
        )
    except OSError as exc:
        print(f"ERROR: git ls-files に失敗しました: {exc}", file=sys.stderr)
        return 2

    if result.returncode != 0:
        # 権限エラー・git 内部エラー等で git ls-files が非ゼロ終了した場合は
        # stdout の真偽で判定すると誤検知のリスクがある（空 stdout を「sh/bats なし」
        # と読んで誤って exit 0 を返す経路を防ぐ）。
        stderr = (result.stderr or "").strip()
        print(
            f"ERROR: git ls-files が exit={result.returncode} で失敗しました: {stderr}",
            file=sys.stderr,
        )
        return 2

    files = [line for line in result.stdout.splitlines() if line.strip()]
    return _report(files, json_output=bool(args.json_output))


def _report(files: list[str], *, json_output: bool) -> int:
    if not files:
        if json_output:
            import json

            print(json.dumps({"status": "ok", "files": []}))
        else:
            print("OK: *.sh / *.bats は残っていません（sh / bats 完全廃止 達成）", file=sys.stderr)
        return 0

    bypass = os.environ.get(BYPASS_ENV) == "1"
    if json_output:
        import json

        print(
            json.dumps(
                {
                    "status": "warning" if bypass else "fail",
                    "files": files,
                    "count": len(files),
                }
            )
        )
    else:
        print("以下の sh / bats ファイルがまだ残っています:", file=sys.stderr)
        for f in files:
            print(f"  - {f}", file=sys.stderr)
        print(f"合計: {len(files)} 件", file=sys.stderr)

    if bypass:
        print(
            f"WARN: {BYPASS_ENV}=1 が指定されているため warning に格下げします（exit 0）。"
            " Phase 4 完了後はこの環境変数を外してください。",
            file=sys.stderr,
        )
        return 0
    print(
        'FAIL: 親 #1090 の DoD（git ls-files "*.sh" "*.bats" が空）を満たしていません。',
        file=sys.stderr,
    )
    return 1
