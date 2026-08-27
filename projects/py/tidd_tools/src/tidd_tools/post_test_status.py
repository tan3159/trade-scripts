"""`tidd post-test-status` サブコマンド（Issue #1839）.

pytest / Jest テストを実行して結果を GitHub Commit Status API で投稿する。

引数:
- ``project``: プロジェクト名（例: ``tidd_tools``）。
  ``pyproject.toml`` があれば pytest、``package.json`` があれば Jest を自動判定する。

環境変数（必須）:
- ``GITHUB_TOKEN`` または ``GH_TOKEN``
- ``REPO``: ``owner/repo`` 形式

終了コード:
- 0 → テスト全通過 + API 通知成功
- 1 → 環境変数未設定 / API エラー / プロジェクト判定失敗 / テスト失敗
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from tidd_tools.pytest_flock import PytestFlockContext
from tidd_tools.pytest_workers import calc_pytest_workers_from_system
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import SubprocessTimeoutError
from tidd_tools.shared.recursion import is_recursive_call, mark_recursive_subprocess
from tidd_tools.shared.subprocess_runner import run as run_subprocess
from tidd_tools.shared.subprocess_timeouts import STANDARD_TIMEOUT_SEC, test_suite_timeout_sec


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "post-test-status",
        help="pytest / Jest を実行して結果を GitHub Commit Status に投稿する（Issue #1839）",
        description=__doc__,
    )
    add_common_flags(parser)
    parser.add_argument(
        "project",
        help="プロジェクト名（例: tidd_tools）。pyproject.toml があれば pytest、package.json があれば Jest を実行する",
    )
    parser.add_argument(
        "--project-dir",
        default=None,
        help="プロジェクトディレクトリのパス（省略時は projects/py/<project> または projects/gas/<project> を自動推定）",  # noqa: E501
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    project_dir: Path | None = None
    if args.project_dir:
        project_dir = Path(args.project_dir)
    return execute(project=args.project, project_dir=project_dir, env=os.environ)


def execute(
    *,
    project: str,
    project_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    sha: str | None = None,
) -> int:
    """post-test-status のメインロジック.

    :param project: プロジェクト名（例: ``tidd_tools``）
    :param project_dir: プロジェクトディレクトリ（省略時は自動推定）
    :param env: 環境変数マップ（テスト時に注入可能）
    :param sha: 投稿先 commit SHA（省略時は git rev-parse HEAD で取得）
    :returns: 0 = 成功、1 = 失敗
    """
    if env is None:
        env = os.environ

    # ── 認証情報 ─────────────────────────────────────────────────────────
    github_token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or ""
    if not github_token:
        print("ERROR: GITHUB_TOKEN が設定されていません", file=sys.stderr)
        print("HINT: 環境変数 GITHUB_TOKEN を設定してください（.envrc / CI secrets 等・#3214）", file=sys.stderr)
        return 1

    repo = env.get("REPO") or ""
    if not repo:
        print("ERROR: REPO が設定されていません", file=sys.stderr)
        print("HINT: export REPO=owner/repo 等で設定してください", file=sys.stderr)
        return 1

    # ── SHA 取得 ────────────────────────────────────────────────────────
    if not sha:
        sha_proc = run_subprocess(
            ["git", "rev-parse", "HEAD"],
            timeout=STANDARD_TIMEOUT_SEC,
        )
        if sha_proc.returncode != 0:
            print("ERROR: git rev-parse HEAD に失敗しました", file=sys.stderr)
            return 1
        sha = sha_proc.stdout.strip()

    # ── プロジェクトディレクトリ・ランナー判定 ────────────────────────────
    if project_dir is None:
        project_dir = _resolve_project_dir(project)

    runner = _detect_runner(project_dir)
    if runner is None:
        print(
            f"ERROR: {project_dir} に pyproject.toml も package.json も見つかりません",
            file=sys.stderr,
        )
        print("HINT: プロジェクトディレクトリを --project-dir で明示指定してください", file=sys.stderr)
        return 1

    context = f"{runner}/{project}"

    # ── テスト実行 ─────────────────────────────────────────────────────
    # vendor 配布された project（`tests/` 非同梱・Issue #3979）に pytest を起動すると、
    # `tests/conftest.py` の `pytest_addoption` で登録される ini オプション（`skip_threshold` 等）が
    # 未登録のまま `filterwarnings = ["error", ...]` により INTERNALERROR になる。
    # `tests/` が実在しない project は pytest 対象から除外し、正常系（テストなし）として扱う
    # （Issue #3991）。
    if runner == "pytest" and not (project_dir / "tests").is_dir():
        print(f"==> pytest skip（{project_dir} に tests/ ディレクトリがありません・Issue #3991）", file=sys.stderr)
        test_exit = 0
    else:
        test_exit = _run_tests(runner=runner, project_dir=project_dir)

    # ── Commit Status 投稿 ────────────────────────────────────────────
    state = "success" if test_exit == 0 else "failure"
    description = "All tests passed" if test_exit == 0 else "Tests failed"
    print(f"==> GitHub Commit Status を送信中: state={state}, context={context}", file=sys.stderr)

    if not _post_commit_status(
        repo=repo,
        sha=sha,
        token=github_token,
        state=state,
        context=context,
        description=description,
    ):
        return 1

    print(f"==> GitHub Commit Status 送信完了: {state}", file=sys.stderr)

    return 0 if test_exit == 0 else 1


def _resolve_project_dir(project: str) -> Path:
    """プロジェクト名からディレクトリを推定する.

    - ``projects/py/<project>`` に pyproject.toml があれば Python プロジェクト
    - ``projects/gas/<project>`` に package.json があれば GAS/JS プロジェクト
    - いずれもなければ ``projects/py/<project>`` を返す（runner 判定で失敗する）
    """
    try:
        # git rev-parse で repo root を取得する
        proc = run_subprocess(
            ["git", "rev-parse", "--show-toplevel"],
            timeout=STANDARD_TIMEOUT_SEC,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            repo_root = Path(proc.stdout.strip())
            py_dir = repo_root / "projects" / "py" / project
            gas_dir = repo_root / "projects" / "gas" / project
            if (py_dir / "pyproject.toml").is_file():
                return py_dir
            if (gas_dir / "package.json").is_file():
                return gas_dir
            # どちらにも見つからなければ py を返す
            return py_dir
    except (OSError, SubprocessTimeoutError):
        pass
    return Path(f"projects/py/{project}")


def _detect_runner(project_dir: Path) -> str | None:
    """プロジェクトディレクトリからテストランナーを判定する.

    - ``pyproject.toml`` が存在 → ``pytest``
    - ``package.json`` が存在 → ``jest``
    - どちらもなければ None
    """
    if (project_dir / "pyproject.toml").is_file():
        return "pytest"
    if (project_dir / "package.json").is_file():
        return "jest"
    return None


# テスト subprocess に渡す安全な変数の完全一致リスト（prefix 許可ではなく名前を明示）
_SAFE_ENV_EXACT = frozenset(
    [
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TMPDIR",
        "TMP",
        "TEMP",
        "VIRTUAL_ENV",
        "VIRTUAL_ENV_PROMPT",
        # Node.js（認証情報を持たない変数のみ明示）
        "NODE_ENV",
        "NODE_PATH",
        "NODE_OPTIONS",
        # Issue #2784: pytest-xdist ワーカー数の明示指定を subprocess まで届ける
        "PYTEST_XDIST_AUTO_NUM_WORKERS",
    ]
)

# prefix 一致を許容する安全な接頭辞（認証情報を持たない prefix のみ）
_SAFE_ENV_PREFIXES = ("LC_", "UV_", "PYENV_", "MISE_")


def _safe_subprocess_env() -> dict[str, str]:
    """テスト subprocess に渡す最小環境を返す（allowlist 方式で機密変数を渡さない）.

    Issue #4160: 常に再帰ガード（`_TIDD_TOOLS_RECURSION_GUARD`）を立てて返す。
    `_run_tests()` を呼び出す祖先プロセス（`tidd ai-review` 等）自身の `os.environ`
    には通常ガードが立っていない（祖先自身が再帰呼び出しではないため）。そのため
    allowlist へガード変数を追加して「既存値を保持するだけ」の実装では、この
    子 pytest subprocess の env にガードが伝播しない。`PytestFlockContext` を
    ここで新規取得したか祖先が既に保持しているかに関わらず、これから spawn する
    子プロセス内で実行される（モックなしの）`PytestFlockContext` /
    `is_recursive_call()` の実コードパスが、既に保持済みのロックへ再取得を
    試みて自己デッドロックしないよう、無条件でガードを立てる
    （`mark_recursive_subprocess()` は allowlist で絞った dict を base に取るため、
    機密変数を広げることはない）。
    """
    filtered = {
        k: v for k, v in os.environ.items() if k in _SAFE_ENV_EXACT or any(k.startswith(p) for p in _SAFE_ENV_PREFIXES)
    }
    return mark_recursive_subprocess(filtered)


def _has_pytest_xdist(project_dir: Path) -> bool:
    """project_dir/pyproject.toml の dev deps に pytest-xdist が含まれるか判定する.

    Issue #2486: `-n auto` を無条件付与すると xdist 未導入 project で
    unrecognized arguments で失敗するため、明示的に判定する。
    """
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        return "pytest-xdist" in pyproject.read_text(encoding="utf-8")
    except OSError:
        return False


def _run_tests(*, runner: str, project_dir: Path) -> int:
    """テストを実行して終了コードを返す."""
    safe_env = _safe_subprocess_env()
    if runner == "pytest":
        print(f"==> pytest テスト実行中: {project_dir}", file=sys.stderr)
        start = time.time()
        # Issue #2486: pytest-xdist が dev 依存に入っている project のみ並列化する
        # （test_plan.py と同じパターン）。tidd_tools は ~3000 tests あり
        # 直列実行だと 600s hardcoded timeout を超過するため。
        # projects/py/publish・projects/py/example には xdist が入っておらず
        # 無条件 `-n auto` は unrecognized arguments で失敗するため conditional にする。
        workers: int | None = None
        if _has_pytest_xdist(project_dir):
            # Issue #2784: `-n auto` の代わりにメモリ量を考慮したワーカー数を使う。
            workers = calc_pytest_workers_from_system(stderr=sys.stderr)
        cmd = _build_pytest_cmd(project_dir, workers=workers)
        # Issue #2787: PytestFlockContext で同時実行を直列化してメモリ枯渇を防ぐ。
        # Issue #2835: 入れ子（親がロックを握ったまま起動した子 pytest）では取り直さない。
        # 祖先が保持済みなので直列化の目的は満たされており、取りにいくと必ず上限まで待つ。
        lock_ctx = contextlib.nullcontext() if is_recursive_call() else PytestFlockContext()
        with lock_ctx:
            result = run_subprocess(
                cmd,
                cwd=project_dir,
                timeout=test_suite_timeout_sec(),
                env=safe_env,
            )
        duration = int(time.time() - start)
        print(f"==> pytest 完了: exit={result.returncode} ({duration}s)", file=sys.stderr)
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return result.returncode

    # jest
    print(f"==> Jest テスト実行中: {project_dir}", file=sys.stderr)
    start = time.time()
    result = run_subprocess(
        ["npx", "jest"],
        cwd=project_dir,
        timeout=test_suite_timeout_sec(),
        env=safe_env,
    )
    duration = int(time.time() - start)
    print(f"==> Jest 完了: exit={result.returncode} ({duration}s)", file=sys.stderr)
    if result.stdout:
        print(result.stdout, file=sys.stderr)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


def _build_pytest_cmd(project_dir: Path, *, workers: int | None) -> list[str]:
    """pytest 起動コマンドを組み立てる（Issue #3287・#2969）.

    ai-review が実際に実行する post_test_status 経路でも slow マーカー付きテスト
    （copier E2E・mypy フル実行・uv sync 等）を除外して資源枯渇を防ぐ。
    `test_plan._build_pytest_cmd` と同一の `-m "not slow"` 基準を適用する。
    """
    cmd = ["uv", "run", "--project", str(project_dir), "pytest", "-m", "not slow"]
    if workers is not None:
        cmd.extend(["-n", str(workers)])
    return cmd


def _post_commit_status(
    *,
    repo: str,
    sha: str,
    token: str,
    state: str,
    context: str,
    description: str,
) -> bool:
    """GitHub Commit Status API に状態を投稿する."""
    url = f"https://api.github.com/repos/{repo}/statuses/{sha}"
    body = json.dumps(
        {
            "state": state,
            "context": context,
            "description": description,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as exc:
        print("ERROR: GitHub Commit Status API の呼び出しに失敗しました", file=sys.stderr)
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_body = str(exc)
        print(err_body, file=sys.stderr)
        return False
    except urllib.error.URLError as exc:
        print("ERROR: GitHub Commit Status API の呼び出しに失敗しました", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return False
