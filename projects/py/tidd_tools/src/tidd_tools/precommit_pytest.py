"""`tidd pre-commit-pytest <project_dir>` サブコマンド（Issue #4170）.

pre-commit の `entry:` から呼べる pytest 実行の入口。責務は「指定 project の pytest を、
TMPDIR ガード（#4143）+ xdist ワーカー数（#2894）+ flock 直列化（#2787）+
再帰ガード（`shared/recursion.py`）付きで実行する」に限定する。

コマンド組み立て（`--extra dev` 付与判定・pytest-xdist 依存判定・coverage 付与判定）は
`run_project_tests._pytest_cmd()` をそのまま再利用し、二重実装を作らない
（`_has_xdist_dependency` 相当の共有・Issue #4170 やること）。TMPDIR ガード付き環境変数の
組み立て・タイムアウト付きプロセス起動・失敗時ログ tail 表示は `test_plan._run_pytest_process()`
（`_build_pytest_subprocess_env()` 経由）を再利用する。

`tidd run-project-tests` は PR 番号起点で変更ファイルから対象プロジェクトを決める設計で、
コミット前の pre-commit hook からはそのまま使えない（PR がまだ存在しない）。本サブコマンドは
PR 番号を必要とせず、呼び出し元（pre-commit の `entry:`）が指定した `project_dir` 単体を
対象にする点が異なる。

終了コード:
- 0 → pytest 成功 / pyproject.toml 未検出でスキップ
- 1 → pytest 失敗 / uv 未検出 / project_dir 自体が存在しない（設定不備・#4170 レビュー指摘）
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
from pathlib import Path

from tidd_tools import pytest_tmpdir, run_project_tests, test_plan
from tidd_tools.pytest_flock import PytestFlockContext
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.recursion import is_recursive_call


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "pre-commit-pytest",
        help=(
            "pre-commit の entry から呼べる pytest 実行入口"
            "（TMPDIR ガード + xdist + flock 直列化 + 再帰ガード付き・Issue #4170）"
        ),
        description=__doc__,
    )
    add_common_flags(parser)
    parser.add_argument(
        "project_dir",
        help="pytest を実行する対象プロジェクトのディレクトリ（例: projects/py/<python_package_name>）",
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    if not project_dir.is_dir():
        # `.pre-commit-config.yaml` の entry は copier render 時に固定された project_dir を
        # 渡す。既存 consumer で `detect_python_projects.py` が雛形を削除した場合等、
        # project_dir 自体が存在しないケースは設定不備であり、pyproject.toml 未検出の
        # スキップ（正常系）と同列に exit 0 で通すとテストが実行されないままコミットが
        # 通ってしまう（レビュー指摘・#4170）。ここは exit 1 で失敗させ、consumer に
        # `.pre-commit-config.yaml` の project_dir 修正を促す。
        print(
            f"==> エラー: {project_dir} が見つかりません。"
            ".pre-commit-config.yaml の pytest hook に指定した project_dir を確認してください。",
            file=sys.stderr,
        )
        return 1

    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        print(f"==> スキップ: {project_dir} に pyproject.toml がありません", file=sys.stderr)
        return 0

    if shutil.which("uv") is None:
        print(
            "==> エラー: uv が見つかりません。https://docs.astral.sh/uv/ を参考にインストールしてください。",
            file=sys.stderr,
        )
        return 1

    _cleanup_pytest_tmpdir()

    print(f"==> pre-commit pytest 実行中: {project_dir}", file=sys.stderr)
    cmd = run_project_tests._pytest_cmd(project_dir)
    timeout_secs = test_plan._pytest_timeout_secs()
    # Issue #2787/#2835 と同じ判断: 入れ子（親が既にロックを保持したまま起動した子 pytest）
    # では取り直さない。祖先が保持済みなら直列化の目的は満たされている。
    #
    # Issue #4205: 本サブコマンドは `git commit` 単一プロセスの pre-commit フックであり、
    # `PytestFlockContext` が守ろうとしている「複数プロセスでのフルスイート同時実行防止」
    # は元々不要。#4205 導入当時は Windows ネイティブ（`sys.platform == "win32"`）で
    # `PytestFlockContext` が常に fail-closed する設計（#3877/#3880）だったため、
    # Windows ネイティブに限りロック取得自体をスキップしていた。
    #
    # Issue #4207: `PytestFlockContext` 自体が Windows ネイティブでも msvcrt.locking() に
    # よる実ロックを取得できるようになり、上記の「常に fail-closed する」設計は解消済み。
    # 本サブコマンドが Windows ネイティブでロック取得をスキップし続けているのは
    # fail-closed バグの回避目的ではなく、直上のコメント（単一プロセスの pre-commit
    # フックでは直列化自体が元々不要）という理由のみによる。
    skip_lock = is_recursive_call() or sys.platform == "win32"
    lock_ctx = contextlib.nullcontext() if skip_lock else PytestFlockContext()
    with lock_ctx:
        returncode = test_plan._run_pytest_process(cmd, project_dir, timeout_secs)

    if returncode != 0:
        print(f"==> pre-commit pytest 失敗: {project_dir}", file=sys.stderr)
        return 1
    return 0


def _cleanup_pytest_tmpdir() -> None:
    """pytest 起動前に一時ディレクトリの総容量ガードを走らせる（Issue #2834・#4143 と同型）.

    掃除は best-effort。失敗しても本体を止めないよう例外を握りつぶす。
    """
    try:
        pytest_tmpdir.cleanup(stderr=sys.stderr)
    except OSError as exc:
        print(f"WARN: pytest 一時ディレクトリの掃除をスキップしました: {exc}", file=sys.stderr)
