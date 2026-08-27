"""`tidd run-project-tests <PR>` サブコマンド（旧 `scripts/run-project-tests.sh` の Python 移植）.

PR の変更ファイルから影響を受けるプロジェクト（`projects/gas/*/`・`projects/py/*/`）を
検出し、Jest または pytest を選択的に実行する。

- GAS プロジェクト: `package.json` の dependencies/devDependencies に jest があれば
  `npm ci --ignore-scripts --silent && ./node_modules/.bin/jest`
- Python プロジェクト: `pyproject.toml` があれば `uv run [--extra dev] pytest`
  （Issue #2294: project venv 経由で起動し、consumer 環境のグローバル python への
  pytest 導入を不要にする。`uv` が PATH 上にない場合はエラーで停止する）
- いずれも `env -i` 相当の最小環境（HOME / PATH のみ）でサブプロセス起動して
  ホスト機密環境変数の漏洩を防ぐ（旧 sh と同じ）

`--all` フラグ（Issue #2878）: PR diff を使わず `projects/py/*/`（`example` を除く）を
全件検出して pytest を実行する。nightly-tests 用。新規 Python プロジェクトを
`projects/py/` に追加しても `.circleci/config.yml` の編集が不要になる。

`--all-gas` フラグ（Issue #2879）: PR diff を使わず `projects/gas/*/` を全件検出して
jest を実行する（jest 依存がないプロジェクトはスキップ）。nightly-gas-tests job 用。
新規 GAS プロジェクトを `projects/gas/` に追加しても `.circleci/config.yml` の編集が
不要になる。

Issue #2894: Python pytest は dev extra に pytest-xdist を持つプロジェクトのみ
`-n <workers>` で並列化する（未導入プロジェクトへの無条件付与は
`unrecognized arguments: -n` を招くため。レビュー指摘対応）。
サブプロセスのタイムアウトはハードコード 600 秒ではなく `TIDD_PYTEST_TIMEOUT_SECS`
（既定 1800 秒）で制御する。タイムアウト時は Traceback を投げず孫プロセス
（`uv run` 越しの実体 pytest）ごと停止して exit 1 する（test_plan.py の
Issue #2832 と同型の障害への対処。projects/py 配下のテストがハードコード
600 秒を超える規模まで増えたため）。

Issue #3427: `npm ci` / jest はデフォルトで capture 化されている。成功時は
npm の進捗・jest の個別テストケースログを出力せず「jest passed」の 1 行サマリ
のみを stderr に出力する（`tidd run-project-tests` は Claude Code セッション内
から実行されるため、素通しの大量ログが LLM コンテキストを浪費していた）。
失敗時のみキャプチャ出力の末尾（npm ci: 20 行・jest: 50 行）を stderr に出力
する。`TIDD_TEST_OUTPUT_VERBOSE=1` で従来の素通し動作に戻せる。

終了コード:
- 0 → 全テスト通過またはスキップ
- 1 → いずれかのスイートが失敗 / gh diff の取得失敗
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from tidd_tools.pytest_workers import calc_pytest_workers_from_system
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.gh_client import pr_diff_files, repo_name_with_owner
from tidd_tools.shared.subprocess_runner import run as run_subprocess
from tidd_tools.shared.subprocess_timeouts import TEST_SUITE_TIMEOUT_SEC

#: pytest サブプロセスのタイムアウト既定値（秒）。`TIDD_PYTEST_TIMEOUT_SECS` で上書き可（Issue #2894）。
DEFAULT_PYTEST_TIMEOUT_SECS = 1800

GAS_DIR_RE = re.compile(r"^projects/gas/([^/]+)")
PY_DIR_RE = re.compile(r"^projects/py/([^/]+)")

# Issue #2878: 全件検出モードで除外するプロジェクト名。
# projects/py/example は docs/reference/tech-stack-map.md に明記された学習用デモ
# パッケージであり実プロダクトの振る舞いを持たないため、nightly 全件検出の対象外とする。
EXCLUDED_ALL_PY_PROJECTS = frozenset({"example"})

#: npm ci 失敗時に stderr へ表示するキャプチャ出力の末尾行数（Issue #3427）。
NPM_CI_FAILURE_TAIL_LINES = 20
#: jest 失敗時に stderr へ表示するキャプチャ出力の末尾行数（Issue #3427）。
JEST_FAILURE_TAIL_LINES = 50


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "run-project-tests",
        help="PR の変更ファイルに応じて GAS/Python プロジェクトのテストを実行する（旧 scripts/run-project-tests.sh）",
        description=__doc__,
    )
    add_common_flags(parser)
    parser.add_argument("pr_num", nargs="?", default=None, help="PR 番号（--all/--all-gas 指定時は不要）")
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "PR diff を使わず projects/py/*/（example を除く）を全件検出して pytest を実行する"
            "（nightly 用・Issue #2878）"
        ),
    )
    parser.add_argument(
        "--all-gas",
        action="store_true",
        help=(
            "PR diff を使わず projects/gas/*/ を全件検出して jest を実行する"
            "（jest 依存がなければスキップ。nightly 用・Issue #2879）"
        ),
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    if getattr(args, "all", False):
        return run_all_py_tests()
    if getattr(args, "all_gas", False):
        return run_all_gas_tests()

    pr_num = args.pr_num
    if pr_num is None:
        print("==> エラー: pr_num を指定するか --all/--all-gas を指定してください。", file=sys.stderr)
        return 1
    repo = os.environ.get("REPO") or repo_name_with_owner() or ""

    # gh pr diff --name-only で変更ファイルを取得
    try:
        changed = pr_diff_files(pr_num, repo=repo or None)
    except GhCommandError as exc:
        print(
            f"==> エラー: gh pr diff が失敗しました（exit {exc.returncode}）。PR番号・認証を確認してください。",
            file=sys.stderr,
        )
        return 1

    if not changed:
        print("==> 変更ファイルなし: プロジェクトテストをスキップします。", file=sys.stderr)
        return 0

    exit_code = 0

    # ── GAS プロジェクト ──────────────────────────────────────────────
    gas_projects = _detect_projects(changed, GAS_DIR_RE, "projects/gas")
    if gas_projects:
        for project_dir in gas_projects:
            rc = _run_gas_tests(Path(project_dir))
            if rc != 0:
                exit_code = 1
    else:
        print("==> GAS プロジェクトの変更なし: npm test をスキップします", file=sys.stderr)

    # ── Python プロジェクト ──────────────────────────────────────────
    py_projects = _detect_projects(changed, PY_DIR_RE, "projects/py")
    if py_projects:
        for project_dir in py_projects:
            rc = _run_py_tests(Path(project_dir))
            if rc != 0:
                exit_code = 1
    else:
        print("==> Python プロジェクトの変更なし: pytest をスキップします", file=sys.stderr)

    return exit_code


def _detect_projects(changed: list[str], regex: re.Pattern[str], prefix: str) -> list[str]:
    """変更ファイル一覧から `projects/<kind>/<project>` の重複なしリストを返す."""
    seen: set[str] = set()
    for f in changed:
        m = regex.match(f)
        if m:
            seen.add(f"{prefix}/{m.group(1)}")
    return sorted(seen)


# ── 全件検出モード（Issue #2878・nightly 用） ────────────────────────────


def discover_all_py_projects(base_dir: Path | None = None) -> list[str]:
    """`projects/py/*/` を全件検出し、`EXCLUDED_ALL_PY_PROJECTS` を除外したソート済みリストを返す.

    PR diff を使わない nightly 用の全件検出モード（Issue #2878）。
    `pyproject.toml` を持つディレクトリのみ対象とする。
    """
    root = base_dir if base_dir is not None else Path.cwd()
    py_root = root / "projects" / "py"
    if not py_root.is_dir():
        return []
    found: list[str] = []
    for entry in sorted(py_root.iterdir()):
        if not entry.is_dir() or entry.name in EXCLUDED_ALL_PY_PROJECTS:
            continue
        if (entry / "pyproject.toml").is_file():
            found.append(f"projects/py/{entry.name}")
    return found


def run_all_py_tests(base_dir: Path | None = None) -> int:
    """nightly 用: PR diff を使わず `projects/py/*/`（example を除く）を全件検出して pytest を実行する.

    Issue #2878。
    """
    root = base_dir if base_dir is not None else Path.cwd()
    projects = discover_all_py_projects(root)
    if not projects:
        print("==> Python プロジェクトが検出されませんでした", file=sys.stderr)
        return 0

    exit_code = 0
    for project_rel in projects:
        print(f"==> 全件検出モード: {project_rel} で pytest を実行します", file=sys.stdout)
        rc = _run_py_tests(root / project_rel)
        if rc != 0:
            exit_code = 1
    return exit_code


# ── GAS 全件検出モード（Issue #2879・nightly 用） ────────────────────────


def discover_all_gas_projects(base_dir: Path | None = None) -> list[str]:
    """`projects/gas/*/` を全件検出したソート済みリストを返す.

    PR diff を使わない nightly 用の全件検出モード（Issue #2879）。
    `package.json` を持つディレクトリのみ対象とする。jest 依存の有無による
    スキップ判定は `_run_gas_tests`/`_has_jest_dependency` が実行時に行う。
    """
    root = base_dir if base_dir is not None else Path.cwd()
    gas_root = root / "projects" / "gas"
    if not gas_root.is_dir():
        return []
    found: list[str] = []
    for entry in sorted(gas_root.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "package.json").is_file():
            found.append(f"projects/gas/{entry.name}")
    return found


def run_all_gas_tests(base_dir: Path | None = None) -> int:
    """nightly 用: PR diff を使わず `projects/gas/*/` を全件検出して jest を実行する.

    jest 依存を持たないプロジェクトは `_run_gas_tests` 内でスキップされ、
    job を失敗させない（Issue #2879）。
    """
    root = base_dir if base_dir is not None else Path.cwd()
    projects = discover_all_gas_projects(root)
    if not projects:
        print("==> GAS プロジェクトが検出されませんでした", file=sys.stderr)
        return 0

    exit_code = 0
    for project_rel in projects:
        print(f"==> 全件検出モード: {project_rel} で npm ci と jest を実行します", file=sys.stdout)
        rc = _run_gas_tests(root / project_rel)
        if rc != 0:
            exit_code = 1
    return exit_code


# ── GAS テスト ────────────────────────────────────────────────────────


def _run_gas_tests(project_dir: Path) -> int:
    pkg_json = project_dir / "package.json"
    if not pkg_json.is_file():
        print(f"==> スキップ: {project_dir} に package.json がありません", file=sys.stderr)
        return 0
    if not _has_jest_dependency(pkg_json):
        print(f"==> スキップ: {project_dir}/package.json に jest の依存関係がありません", file=sys.stderr)
        return 0

    print(f"==> GAS テスト実行中: {project_dir}", file=sys.stderr)

    minimal_env = _minimal_env()
    verbose = _test_output_verbose()

    npm_ci = run_subprocess(
        ["npm", "ci", "--ignore-scripts", "--silent"],
        cwd=project_dir,
        env=minimal_env,
        capture=not verbose,
        timeout=TEST_SUITE_TIMEOUT_SEC,
    )
    if npm_ci.returncode != 0:
        if not verbose:
            _print_captured_tail(npm_ci, NPM_CI_FAILURE_TAIL_LINES)
        print(f"==> GAS テスト失敗: {project_dir}", file=sys.stderr)
        return 1

    jest_bin = project_dir / "node_modules" / ".bin" / "jest"
    jest_started = time.monotonic()
    jest_result = run_subprocess(
        [str(jest_bin)], cwd=project_dir, env=minimal_env, capture=not verbose, timeout=TEST_SUITE_TIMEOUT_SEC
    )
    jest_elapsed = time.monotonic() - jest_started
    if jest_result.returncode != 0:
        if not verbose:
            _print_captured_tail(jest_result, JEST_FAILURE_TAIL_LINES)
        print(f"==> GAS テスト失敗: {project_dir}", file=sys.stderr)
        return 1

    if not verbose:
        print(f"==> jest passed: {project_dir}（{jest_elapsed:.1f}秒）", file=sys.stderr)
    print(f"==> GAS テスト成功: {project_dir}", file=sys.stderr)
    return 0


def _test_output_verbose() -> bool:
    """`TIDD_TEST_OUTPUT_VERBOSE=1` のとき npm ci / jest の出力を従来の素通し（capture なし）に戻す.

    `tidd test-plan` の同名 env var（Issue #3426）と挙動を揃える（Issue #3427）。
    """
    return os.environ.get("TIDD_TEST_OUTPUT_VERBOSE") == "1"


def _print_captured_tail(result: subprocess.CompletedProcess[str], max_lines: int) -> None:
    """キャプチャ済み subprocess 出力（stdout+stderr）の末尾 `max_lines` 行を stderr に出力する（Issue #3427）."""
    combined = (result.stdout or "") + (result.stderr or "")
    for line in combined.splitlines()[-max_lines:]:
        print(line, file=sys.stderr)


def _has_jest_dependency(pkg_json: Path) -> bool:
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    for key in ("dependencies", "devDependencies"):
        deps = data.get(key, {})
        if isinstance(deps, dict):
            for name in deps:
                if "jest" in name:
                    return True
    return False


# ── Python テスト ────────────────────────────────────────────────────


def _has_dev_extra(text: str) -> bool:
    """pyproject.toml の `[project.optional-dependencies]` に `dev` extra が定義されているか判定する（Issue #2294）."""
    match = re.search(r"\[project\.optional-dependencies\](.*?)(?:\n\[|\Z)", text, re.DOTALL)
    if not match:
        return False
    return bool(re.search(r"(?m)^\s*dev\s*=", match.group(1)))


def _has_xdist_dependency(text: str) -> bool:
    """pyproject.toml の `[project.optional-dependencies]` に pytest-xdist が含まれるか判定する（Issue #2894）.

    `projects/py/example` のように dev extra に pytest-xdist を持たないプロジェクトへ
    無条件で `-n` を付与すると `unrecognized arguments: -n` で失敗するため、xdist が
    宣言されている場合のみ `-n` を付与する（レビュー指摘対応）。
    """
    match = re.search(r"\[project\.optional-dependencies\](.*?)(?:\n\[|\Z)", text, re.DOTALL)
    if not match:
        return False
    return "pytest-xdist" in match.group(1)


def _pytest_cmd(project_dir: Path) -> list[str]:
    """pytest 起動コマンドを組み立てる.

    Issue #2294: consumer 環境でグローバル python への pytest 導入を不要にするため
    `uv run` 経由で起動する（CI と同じ project venv を使う）。project_dir の
    pyproject.toml に `[project.optional-dependencies]` の `dev` extra が定義されて
    いれば `--extra dev` を付与する。

    Issue #2894: `-n <workers>` でメモリ量に応じたワーカー数の pytest-xdist 並列実行にする。
    直列実行だと projects/py 配下のテスト全体（4000 件超）が 600 秒を超え nightly が
    タイムアウトしていたため。dev extra に pytest-xdist が宣言されているプロジェクトの
    みに付与する（`projects/py/example` のように未導入のプロジェクトでは付与しない）。

    Issue #1369: `[tool.coverage.report]` セクションと `fail_under` が定義されて
    いれば `--cov --cov-report=term-missing` を追加する。
    """
    pyproject = project_dir / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        text = ""

    cmd = ["uv", "run"]
    if _has_dev_extra(text):
        cmd.extend(["--extra", "dev"])
    cmd.append("pytest")
    if _has_xdist_dependency(text):
        cmd.extend(["-n", str(calc_pytest_workers_from_system())])
    # 単純な header 検出。tomllib を避けて stdlib のみで判定。
    if "[tool.coverage.report]" in text and "fail_under" in text:
        cmd.extend(["--cov", "--cov-report=term-missing"])
    return cmd


def _pytest_timeout_secs() -> int:
    """pytest サブプロセスのタイムアウト秒数を返す（``TIDD_PYTEST_TIMEOUT_SECS`` で上書き可・Issue #2894）.

    test_plan.py の `_pytest_timeout_secs`（Issue #2832）と同じパターンだが、Rule of
    Three（3 回出現するまで共有抽象化しない）に従い共有モジュール化はせずこのモジュール内で完結させる。
    """
    raw = os.environ.get("TIDD_PYTEST_TIMEOUT_SECS")
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value >= 1:
            return value
    return DEFAULT_PYTEST_TIMEOUT_SECS


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    """`proc` をプロセスグループごと停止する（Issue #2894・#3877）.

    `os.killpg`/`os.getpgid`/`signal.SIGKILL` は POSIX 専用（Windows には存在せず、
    typeshed のスタブも `sys.platform` ガード配下にのみ定義されているため
    `mypy --strict` が win32 で `[attr-defined]` エラーを出す）。Windows では
    `start_new_session=True` がプロセスグループ生成を行わない（無視される）ため、
    直接 `proc.kill()` で十分（孫プロセスの停止は #3880 のスコープ）。
    """
    if sys.platform != "win32":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    else:
        proc.kill()


def _run_pytest_subprocess(cmd: list[str], cwd: Path, timeout_secs: int) -> int:
    """pytest を独立セッションで起動し、タイムアウト時はプロセスグループごと停止する（Issue #2894）.

    以前は ``subprocess.run(..., timeout=600)`` を使っており、フルスイートの実行時間が
    ハードコードの 600 秒を超えると ``TimeoutExpired`` が未捕捉のまま Traceback で異常
    終了し、``uv run`` 越しの孫プロセス（実体の pytest）が孤児化していた
    （test_plan.py の Issue #2832 と同型の障害）。
    """
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=cwd,
        env=_minimal_env(),
        start_new_session=True,
    )
    try:
        return proc.wait(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.wait()
        print(
            f"==> pytest テスト打ち切り: {timeout_secs} 秒で終了しなかったため停止しました。"
            "TIDD_PYTEST_TIMEOUT_SECS でタイムアウト秒数を延長できます。",
            file=sys.stderr,
        )
        return 1


def _run_py_tests(project_dir: Path) -> int:
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

    print(f"==> Python テスト実行中: {project_dir}", file=sys.stderr)

    cmd = _pytest_cmd(project_dir)
    timeout_secs = _pytest_timeout_secs()
    returncode = _run_pytest_subprocess(cmd, project_dir, timeout_secs)
    if returncode != 0:
        print(f"==> Python テスト失敗: {project_dir}", file=sys.stderr)
        return 1

    print(f"==> Python テスト成功: {project_dir}", file=sys.stderr)
    return 0


def _minimal_env() -> dict[str, str]:
    """`env -i HOME=$HOME PATH=$PATH` 相当の最小環境.

    Issue #3485: `CI`（CircleCI 等が自動設定する非機密フラグ）が設定されている
    場合のみ、そのまま子プロセスへ伝播する。`health_check._check_root_claude_md`
    は `os.environ.get("CI")` が真のときのみ root CLAUDE.md 欠落チェックを
    skip する設計（Issue #3407）だが、この CI 伝播が欠けていたため CircleCI
    nightly 上で `_run_pytest_subprocess` 越しの孫プロセス（実体の pytest）
    内部では CI が未設定になり、gitignore 済みでコンテナに存在しない root
    CLAUDE.md を誤って失敗として検出していた。`CI` は値の有無のみが意味を持つ
    非機密フラグのため、機密環境変数の非漏洩という本関数の設計方針は崩さない。
    """
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ.get("PATH", ""),
    }
    ci = os.environ.get("CI")
    if ci:
        env["CI"] = ci
    return env
