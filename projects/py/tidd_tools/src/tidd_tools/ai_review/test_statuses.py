"""ai-review の commit status 投稿サブシステム（Issue #2964）.

``_run_test_plan`` を入口に、``_post_test_statuses`` → ``_post_lint_statuses`` →
``_post_commit_status`` という内部呼び出しグラフを構成する commit status 投稿系
（旧 ``core.py`` 328-1226 行・約 900 行）を 1 モジュールへ物理的に移設する
（挙動変更なし）。

**設計方針（既存テスト互換の限界・#2964）:** ``gates.py``（Issue #2964 前半）は
main() → gate 関数の 1 段のみの境界越えだったため依存注入で既存テスト互換を
100% 維持できたが、本モジュールは内部で ``_run_test_plan`` → ``_post_test_statuses``
→ ``_post_lint_statuses`` → ``_post_commit_status`` と何段も自己完結して呼び合う
構造のため、境界は ``core.main()`` → ``_run_test_plan`` の 1 箇所のみ（他は本モジュール
内で閉じる）。したがって ``patch.object(core, "_post_commit_status", ...)`` のように
「本モジュールへ移設された関数を core 側から patch しつつ、同じく移設された別関数
経由で間接的に呼び出す」形の既存テストは、移設後は patch 対象を
``tidd_tools.ai_review.test_statuses`` 側に変更する必要がある
（bare name 解決は定義モジュールの globals に基づくため）。該当テストは本 Issue で
機械的に patch 対象パスのみ更新済み（アサーション内容は無変更）。

``core.py`` 側は ``_run_test_plan`` / ``_read_pytest_executed`` / ``_is_bats_env``
を re-export し、``main()`` の呼び出し方は変更しない。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from tidd_tools import context_budget, preflight_markers
from tidd_tools import post_test_status as _post_test_status_mod
from tidd_tools import test_plan as _test_plan_mod
from tidd_tools.ai_review.timing_steps import measure_step
from tidd_tools.ai_review.tokens import get_effective_review_token
from tidd_tools.shared import gh_client
from tidd_tools.shared.errors import GhCommandError, SubprocessTimeoutError

logger = logging.getLogger(__name__)

# GitHub App Installation Token（bot トークン）の prefix（Issue #345・#3788）。
# 個人 PAT（`ghp_` 等）と区別し、test-plan サブプロセスへ継承させない対象を限定する。
_BOT_TOKEN_PREFIX = "ghs_"

_STATUS_PERMISSION_ERROR = "commit status 投稿の権限が不足しています"
_status_permission_gate_triggered = False


def _is_status_permission_error(exc: GhCommandError) -> bool:
    """Commit Status API の権限不足（HTTP 403）を判定する（Issue #4094）."""
    stderr = str(exc.stderr or "")
    return "HTTP 403" in stderr or "Resource not accessible by personal access token" in stderr


def _record_status_permission_error(exc: GhCommandError) -> bool:
    """権限不足を記録し、escape hatch が有効かどうかを返す（Issue #4094）."""
    global _status_permission_gate_triggered  # noqa: PLW0603 — 1 回の ai-review 実行内で共有する状態
    if not _is_status_permission_error(exc):
        return False
    if os.environ.get("AI_REVIEW_SKIP_STATUS_PERMISSION_GATE") == "1":
        return False
    _status_permission_gate_triggered = True
    return True


def _is_bats_env() -> bool:
    """bats から呼ばれているかを判定する（旧 sh の SKIP_BATS_IN_TEST / BATS_*_TMPDIR チェック）."""
    if os.environ.get("SKIP_BATS_IN_TEST") == "1":
        return True
    if os.environ.get("BATS_RUN_TMPDIR"):
        return True
    return bool(os.environ.get("BATS_TEST_TMPDIR"))


def _post_bats_commit_status(
    repo: str,
    sha: str,
    state: str,
    description: str,
    token: str,
) -> None:
    """`gh_client.commit_status_create` 経由で bats/local の Commit Status を投稿する（Issue #2960）.

    Issue #3957: `_post_commit_status` と同じく `gh_client.commit_status_create` はリトライ上限
    到達時に `SubprocessTimeoutError` を送出する。旧実装は `GhCommandError` のみを catch しており、
    このタイムアウト例外が `tidd ai-review` プロセス全体を異常終了させていた（commit status 投稿は
    fail-soft の設計意図と矛盾）。`SubprocessTimeoutError` も捕捉し WARN を出して継続する。
    """
    try:
        gh_client.commit_status_create(repo, sha, state, "bats/local", description, token=token or None)
    except GhCommandError as exc:
        _record_status_permission_error(exc)
        sys.stderr.write(f"WARN: gh api で commit status 投稿に失敗しました (exit={exc.returncode}): {exc.stderr}\n")
    except SubprocessTimeoutError as exc:
        sys.stderr.write(
            f"WARN: gh api で commit status 投稿がタイムアウトしました"
            f"（context=bats/local, リトライ上限到達）: {exc}\n"
            "再試行する場合は `tidd ai-review <PR> <試行回数>` を再実行してください。\n"
        )


def _detect_py_projects(repo_root: Path) -> list[Path]:
    """``projects/py/`` 配下の Python プロジェクト dir を検出する（Issue #3720）.

    copier 配布物（consumer）は ``projects/py/<python_package_name>/`` のみを配布し
    ``projects/py/tidd_tools`` を持たないため、``tidd_tools`` 固定パスでなく実在する
    Python プロジェクトを動的に列挙する（#2709・#3382 等の固定パス排除方針と整合）。

    判定は旧実装と同じ ``is_dir()`` 基準に合わせる（pyproject.toml の有無は要求しない。
    既存の test_fix_1407 が tidd_tools dir を pyproject.toml なしで作るため・#3720）。
    """
    py_root = repo_root / "projects" / "py"
    if not py_root.is_dir():
        return []
    return sorted(p for p in py_root.iterdir() if p.is_dir())


def _run_test_plan(
    pr_num: str,
    repo: str,
    repo_root: Path,
    state_dir: Path,
) -> int:
    """``tidd_tools test-plan`` をサブプロセス実行する.

    旧 sh と同様、bats / Jest / pytest の commit status 投稿も担う。
    pytest の実行有無は state_dir / "pytest-executed.txt" に記録する（Issue #2636）。

    Issue #3720: consumer（copier 配布物）は ``projects/py/tidd_tools`` を持たないため、
    従来の固定パスチェックで early return され pytest/jest の commit status が投稿されなかった。
    ``projects/py/*`` を動的検出し、Python プロジェクトが 1 つでもあれば test-plan チェックを行う。

    Issue #3762: #3720 では commit status 投稿経路のみ consumer 対応し、``tidd_tools
    test-plan`` サブプロセス自体の起動判定は ``projects/py/tidd_tools`` の実在
    （``tidd_tools_dir in py_projects``）に固定されたままだったため、consumer では
    ``tidd test-plan``（未カバー Test plan 項目の検出）が一度も起動しなかった。
    このコード自体が既に ``tidd_tools`` パッケージとして実行中である（``uv run --project
    projects/py/tidd_tools`` で起動されていれば sys.executable は常に tidd_tools を
    import 可能。consumer も vendor 配布・#3979 により同一経路）ため、``uv run
    --project <dir>`` を経由せず ``sys.executable -m tidd_tools test-plan`` で直接起動する
    （hooks lint の toolchain 解決方式・#3693 と同方針）。これにより ``projects/py/tidd_tools``
    の実在有無に関わらず、Python プロジェクトが 1 つでもあれば test-plan サブプロセスが起動する。
    """
    # Issue #3720/#3762: projects/py/tidd_tools 固定でなく projects/py/* を動的検出する。
    # Python プロジェクト（pyproject.toml）が 1 つも無ければ test-plan 自体を skip する
    # （上流・consumer のどちらでもない repo・異常系・クラッシュしない）。
    py_projects = _detect_py_projects(repo_root)
    if not py_projects:
        print(
            "==> テスト計画チェックをスキップします（projects/py/ 配下に Python プロジェクトが存在しないため）",
            file=sys.stderr,
        )
        return 0

    print("==> テスト計画チェックを実行します（python -m tidd_tools test-plan）...", file=sys.stderr)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "test-plan.log"

    env = dict(os.environ)
    # Issue #3788: #345 では bot トークン（GitHub App Installation Token・`ghs_` prefix）
    # の継承を断つために GH_TOKEN/GITHUB_TOKEN を無条件で空文字に潰していたが、Python
    # 実装では App トークンは呼び出し単位でスコープされ（post_review.py・merge_summary.py
    # 参照）、プロセス env には個人 PAT がそのまま残る。無条件潰しは個人 PAT まで破壊し
    # `gh` を無関係アカウントへフォールバックさせ PR ボディ取得を失敗させていた。
    # bot トークン（`ghs_` prefix）が入っているときだけ除去し、#345 の意図を保つ。
    for _token_key in ("GH_TOKEN", "GITHUB_TOKEN"):
        if env.get(_token_key, "").startswith(_BOT_TOKEN_PREFIX):
            env.pop(_token_key, None)
    env["_AI_REVIEW_RUNNING"] = "1"

    # bats 環境内検知（旧 sh と同じ条件）
    if _is_bats_env():
        env["SKIP_BATS_IN_TEST"] = "1"

    # Issue #1407: 実 pytest 時間 (75s+) が旧 timeout=60 より長いため常に TimeoutExpired で
    # 異常終了していた。300s を既定にし、大規模 CI 環境では AI_REVIEW_TEST_PLAN_TIMEOUT で
    # 上書き可能にする。
    try:
        test_plan_timeout = int(os.environ.get("AI_REVIEW_TEST_PLAN_TIMEOUT", "300"))
    except ValueError:
        test_plan_timeout = 300

    try:
        proc = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "tidd_tools",
                "test-plan",
                str(pr_num),
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=test_plan_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # Issue #1407: timeout 時に例外を伝播せず test-plan 失敗として扱う。
        # AI_REVIEW_TEST_PLAN_TIMEOUT で timeout を延長するようメッセージで案内する。
        sys.stderr.write(
            f"ERROR: test-plan が {test_plan_timeout} 秒を超過しました。"
            "AI_REVIEW_TEST_PLAN_TIMEOUT 環境変数で timeout を延長できます。\n"
        )
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write((exc.stdout or "") if isinstance(exc.stdout, str) else "")
                fh.write((exc.stderr or "") if isinstance(exc.stderr, str) else "")
                ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                fh.write(f"[test-plan] TIMEOUT after {test_plan_timeout}s ts={ts}\n")
        except OSError as write_exc:
            logger.debug("test-plan.log 書き込み失敗: %s", write_exc)
        return 1

    combined = (proc.stdout or "") + (proc.stderr or "")
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(combined)
            fh.write(f"[test-plan] exit={proc.returncode} ts={datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}\n")
    except OSError as exc:
        logger.debug("test-plan.log 書き込み失敗: %s", exc)
    if combined:
        sys.stderr.write(combined)
        if not combined.endswith("\n"):
            sys.stderr.write("\n")

    exit_code = proc.returncode

    # bats / Jest / pytest の Commit Status 投稿
    if not _is_bats_env():
        exit_code = _post_test_statuses(pr_num, repo, repo_root, state_dir, exit_code)

    return exit_code


def _record_marker_hit_origin(state_dir: Path, marker_full_path: Path, repo_root: Path) -> None:
    """test-plan gate でマーカー hit した際、マーカーの出自を state_dir/test-plan.log に記録する（Issue #2800）.

    `test-plan.log` は既に `_run_test_plan` が subprocess 出力の追記先として使っている
    診断ログのため、新規ファイルを増やさずここに追記する（best-effort・失敗はサイレントに無視）。
    """
    marker_path, marker_written_at = preflight_markers.marker_origin(repo_root, marker_full_path)
    if marker_path is None:
        return
    log_path = state_dir / "test-plan.log"
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[test-plan] marker_hit path={marker_path} written_at={marker_written_at} ts={ts}\n")
    except OSError as exc:
        logger.debug("test-plan.log 書き込み失敗: %s", exc)


def _resolve_ci_inputs(pr_num: str, repo: str) -> tuple[str, str]:
    """PR の変更ファイル一覧と head SHA を解決する（#3315 で抽出）."""
    changed_files_list = gh_client.pr_diff_files(pr_num, repo=repo)
    changed_files = "\n".join(changed_files_list)
    if not changed_files:
        git_proc = subprocess.run(  # noqa: S603
            ["git", "diff", "--name-only", "main"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        changed_files = git_proc.stdout if git_proc.returncode == 0 else ""

    sha = gh_client.pr_head_sha(pr_num, repo=repo)
    if not sha:
        git_proc = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
            errors="replace",
        )
        sha = git_proc.stdout.strip() if git_proc.returncode == 0 else ""
    return changed_files, sha


def _detect_test_projects(changed_files: str) -> tuple[list[str], list[str]]:
    """変更ファイルから必要 CI コンテキスト（jest/pytest）の対象プロジェクトを検出する（#3315）."""
    from tidd_tools.ai_review.test_status_gate import detect_required_ci_contexts as _detect_ci

    required_contexts = _detect_ci(changed_files)
    gas_projects = sorted(
        {
            p
            for ctx in required_contexts
            if ctx.startswith("jest/")
            for p in [f"projects/gas/{ctx.removeprefix('jest/')}"]
        }
    )
    py_projects = sorted(
        {
            p
            for ctx in required_contexts
            if ctx.startswith("pytest/")
            for p in [f"projects/py/{ctx.removeprefix('pytest/')}"]
        }
    )
    return gas_projects, py_projects


def _post_bats_status_if_present(
    state_dir: Path,
    repo: str,
    sha: str,
    token: str,
) -> None:
    """bats-status ファイルがあれば Commit Status を投稿する（#3315 で抽出）."""
    bats_status_file = state_dir / "bats-status"
    if not bats_status_file.is_file():
        return
    try:
        bats_status = bats_status_file.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if bats_status == "passed":
        print("==> bats Commit Status 投稿: bats/local (success)", file=sys.stderr)
        _post_bats_commit_status(repo, sha, "success", "bats tests passed", token)
    elif bats_status == "failed":
        print("==> bats Commit Status 投稿: bats/local (failure)", file=sys.stderr)
        _post_bats_commit_status(repo, sha, "failure", "bats tests failed", token)


def _post_jest_statuses(
    gas_projects: list[str],
    *,
    repo_root: Path,
    exec_env: dict[str, str],
    sha: str,
    exit_code: int,
) -> int:
    """Jest Commit Status を投稿し、失敗時は exit_code を 1 に格上げする（#3315 で抽出）."""
    for proj_path in gas_projects:
        proj_name = proj_path.removeprefix("projects/gas/")
        print(f"==> Jest Commit Status 投稿: {proj_path} → jest/{proj_name}", file=sys.stderr)
        rts_exit = _post_test_status_mod.execute(
            project=proj_name,
            project_dir=repo_root / proj_path,
            env=exec_env,
            sha=sha,
        )
        if rts_exit != 0:
            print(
                f"ERROR: Jest テストが失敗しました（{proj_path}）。"
                "テストコードまたはプロダクションコードを修正してください。",
                file=sys.stderr,
            )
            exit_code = 1
    return exit_code


def _post_pytest_statuses(
    py_projects: list[str],
    *,
    repo_root: Path,
    exec_env: dict[str, str],
    sha: str,
    state_dir: Path,
    repo: str,
    token: str,
    skip_cache: bool,
    exit_code: int,
) -> tuple[int, bool | None]:
    """pytest Commit Status を投稿し、実行有無を返す（#3315 で抽出）."""
    tree_hash = "" if skip_cache else preflight_markers._git_commit_tree_hash(repo_root, sha)
    # pytest_executed: py_projects がある場合に実際に pytest を実行したか追跡する（Issue #2636）
    pytest_executed: bool | None = None if not py_projects else False
    for proj_path in py_projects:
        proj_name = proj_path.removeprefix("projects/py/")
        context = f"pytest/{proj_name}"

        if not skip_cache and _has_fresh_success_status(repo, sha, context, token):
            print(f"test-plan: skip pytest（commit status fresh・SHA={sha}）", file=sys.stderr)
            continue

        if not skip_cache and tree_hash and preflight_markers.has_fresh_preflight_tree_marker(repo_root, tree_hash):
            print(f"test-plan: skip pytest（pre-flight マーカー hit・tree={tree_hash}）", file=sys.stderr)
            _record_marker_hit_origin(
                state_dir, preflight_markers._preflight_tree_marker_path(repo_root, tree_hash), repo_root
            )
            print(f"==> pytest Commit Status 投稿: {proj_path} → {context}", file=sys.stderr)
            _post_commit_status(repo, sha, "success", context, "pytest passed（pre-flight cache, Issue #2449）", token)
            continue

        if not skip_cache and preflight_markers.has_fresh_preflight_marker(repo_root, sha):
            print(f"test-plan: skip pytest（pre-flight マーカー hit・SHA={sha}）", file=sys.stderr)
            _record_marker_hit_origin(state_dir, preflight_markers._preflight_marker_path(repo_root, sha), repo_root)
            print(f"==> pytest Commit Status 投稿: {proj_path} → {context}", file=sys.stderr)
            _post_commit_status(repo, sha, "success", context, "pytest passed（pre-flight cache, Issue #2311）", token)
            continue

        print(f"==> pytest Commit Status 投稿: {proj_path} → {context}", file=sys.stderr)
        pytest_executed = True
        rts_exit = _post_test_status_mod.execute(
            project=proj_name,
            project_dir=repo_root / proj_path,
            env=exec_env,
            sha=sha,
        )
        if rts_exit != 0:
            print(
                f"ERROR: pytest テストが失敗しました（{proj_path}）。"
                "テストコードまたはプロダクションコードを修正してください。",
                file=sys.stderr,
            )
            exit_code = 1

    # pytest 実行有無を state_dir に記録する（Issue #2636: save_timing での参照用）
    if pytest_executed is not None:
        try:
            value = "true" if pytest_executed else "false"
            (state_dir / "pytest-executed.txt").write_text(value + "\n", encoding="utf-8")
        except OSError as exc:
            logger.debug("pytest-executed.txt 書き込み失敗: %s", exc)
    return exit_code, pytest_executed


def _post_test_statuses(
    pr_num: str,
    repo: str,
    repo_root: Path,
    state_dir: Path,
    initial_exit: int,
    app_token: str = "",
) -> int:
    """test-plan の結果に基づいて GitHub Commit Status を投稿する.

    Args:
        app_token: 呼び出し元が既に保持している GitHub App installation token（省略可）。
            ``GITHUB_TOKEN``/``GH_TOKEN`` 環境変数が無い実行環境（Issue #2074:
            ``continue_with_verdict`` 経由）でも ``get_effective_review_token()`` の
            fallback として使われる。

    Returns:
        更新後の test-plan exit code（Jest/pytest 失敗で 1 に格上げする可能性あり）。
        pytest の実行有無は state_dir / "pytest-executed.txt" に記録する（Issue #2636）。
    """
    global _status_permission_gate_triggered  # noqa: PLW0603 — 1 回の ai-review 実行ごとにリセットする
    _status_permission_gate_triggered = False
    exit_code = initial_exit
    changed_files, sha = _resolve_ci_inputs(pr_num, repo)

    token = get_effective_review_token(app_token)

    # linter/formatter は sha/token なしでも実行する（Commit Status 投稿は可能な場合のみ）
    exit_code = _post_lint_statuses(
        changed_files=changed_files,
        repo=repo,
        sha=sha,
        token=token,
        repo_root=repo_root,
        initial_exit=exit_code,
    )

    # detect_required_ci_contexts() で必要な CI コンテキストを検出する（Issue #2028 共有関数）
    # NOTE: early return より先に実行する（Issue #1984）。
    gas_projects, py_projects = _detect_test_projects(changed_files)

    if not (sha and token):
        # projects 変更あり & sha/token 欠如 → テスト CI が機能しないためブロックする（Issue #1984）
        if gas_projects or py_projects:
            missing = []
            if not token:
                missing.append("GITHUB_TOKEN")
            if not sha:
                missing.append("sha")
            print(
                f"ERROR: {' / '.join(missing)} が未設定のため projects のテスト CI を実行できません。"
                f" 変更対象: {gas_projects + py_projects}",
                file=sys.stderr,
            )
            return 1
        return exit_code

    _post_bats_status_if_present(state_dir, repo, sha, token)

    exec_env = dict(os.environ)
    exec_env["GITHUB_TOKEN"] = token
    exec_env["REPO"] = repo
    exit_code = _post_jest_statuses(gas_projects, repo_root=repo_root, exec_env=exec_env, sha=sha, exit_code=exit_code)

    skip_cache = os.environ.get("AI_REVIEW_SKIP_TEST_PLAN_CACHE") == "1"
    exit_code, _pytest_executed = _post_pytest_statuses(
        py_projects,
        repo_root=repo_root,
        exec_env=exec_env,
        sha=sha,
        state_dir=state_dir,
        repo=repo,
        token=token,
        skip_cache=skip_cache,
        exit_code=exit_code,
    )
    if _status_permission_gate_triggered and (gas_projects or py_projects):
        print(f"ERROR: {_STATUS_PERMISSION_ERROR}", file=sys.stderr)
        exit_code = 1
    return exit_code


def _read_pytest_executed(state_dir: Path) -> bool | None:
    """state_dir / "pytest-executed.txt" から pytest 実行有無を読み取る（Issue #2636）.

    "true" → True, "false" → False, ファイルなし → None（pytest 対象なし or bats 環境）。
    """
    path = state_dir / "pytest-executed.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _post_commit_status(
    repo: str,
    sha: str,
    state: str,
    context: str,
    description: str,
    token: str,
) -> None:
    """GitHub Commit Status を 1 件投稿する（`gh_client.commit_status_create` 経由・Issue #2960）.

    Issue #3957: `gh_client.commit_status_create` はタイムアウトを内部で短くリトライするが、
    リトライ上限に到達すると最後の `SubprocessTimeoutError` を送出する。旧実装は
    `GhCommandError` のみを catch しており、このタイムアウト例外が `tidd ai-review` プロセス
    全体を異常終了させていた（commit status 投稿は fail-soft の設計意図と矛盾）。
    `SubprocessTimeoutError` も捕捉し WARN を出してレビュー処理を継続する。
    """
    try:
        gh_client.commit_status_create(repo, sha, state, context, description, token=token or None)
    except GhCommandError as exc:
        if _record_status_permission_error(exc):
            return
        sys.stderr.write(
            f"WARN: gh api で commit status 投稿に失敗しました"
            f" (context={context}, exit={exc.returncode}): {exc.stderr}\n"
        )
    except SubprocessTimeoutError as exc:
        sys.stderr.write(
            f"WARN: gh api で commit status 投稿がタイムアウトしました"
            f"（context={context}, リトライ上限到達）: {exc}\n"
            "再試行する場合は `tidd ai-review <PR> <試行回数>` を再実行してください。\n"
        )


def _has_fresh_success_status(repo: str, sha: str, context: str, token: str) -> bool:
    """指定 SHA・context の GitHub Commit Status が success 済みか判定する（Issue #2311）.

    同一 SHA に対する ai-review の再実行（attempt 2 以降・``--continue-with-verdict``）で
    pytest を再実行しないための判定に使う。
    """
    jq_filter = f'.statuses[] | select(.context == "{context}") | .state'
    out = gh_client.gh_raw(
        ["api", f"repos/{repo}/commits/{sha}/status", "--jq", jq_filter],
        token=token or None,
    )
    return out.strip() == "success"


# health-check（Issue #2213）の対象パス。templates/workflow/**・.claude/{hooks,rules,skills,agents}/**
# は prefix 一致、それ以外は完全一致のファイル 2 件。
_HEALTH_CHECK_TARGET_PREFIXES = (
    "templates/workflow/",
    ".claude/hooks/",
    ".claude/rules/",
    ".claude/skills/",
    ".claude/agents/",
)
_HEALTH_CHECK_TARGET_FILES = frozenset(
    {
        "projects/py/tidd_tools/src/tidd_tools/sandbox_copier_poc.py",
        "projects/py/tidd_tools/pyproject.toml",
    }
)

# health_check.py の failures.append() が付与する prefix 一覧（health_check.py と同期させる）。
# ``uv run`` 自体が stderr に出す進捗ログ・警告を失敗件数に誤カウントしないよう、
# 既知 prefix で始まる行のみを失敗としてカウントする。
_HEALTH_CHECK_FAILURE_PREFIXES = ("DRIFT:", "MISSING_TEMPLATE:", "UNREGISTERED:", "UNDISTRIBUTED:", "MISSING_CMD:")


def _has_health_check_targets(changed_files: str) -> bool:
    """変更ファイルに health-check 対象パスが含まれるか判定する（Issue #2213）."""
    for raw in changed_files.splitlines():
        f = raw.strip()
        if not f:
            continue
        if f in _HEALTH_CHECK_TARGET_FILES:
            return True
        if any(f.startswith(prefix) for prefix in _HEALTH_CHECK_TARGET_PREFIXES):
            return True
    return False


_MERMAID_FENCE_RE = re.compile(r"^```mermaid\s*$", re.MULTILINE)


def _file_has_mermaid_fence(path: Path) -> bool:
    """ファイルを読んで ```mermaid フェンスが含まれるか判定する（Issue #2794）."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return bool(_MERMAID_FENCE_RE.search(text))
    except OSError:
        return False


def _get_mermaid_lint_targets(changed_files: str, repo_root: Path) -> list[Path]:
    """変更ファイルから mermaid-lint の対象パスを返す（Issue #2794）.

    - .mmd / .mermaid: 無条件で対象
    - .md / .markdown: ```mermaid フェンスを含むものだけ対象
    削除済みファイル（存在しないもの）は除外する。
    """
    targets: list[Path] = []
    for raw in changed_files.splitlines():
        f = raw.strip()
        if not f:
            continue
        path = repo_root / f
        if not path.exists():
            continue
        ext = path.suffix.lower()
        if ext in (".mmd", ".mermaid") or (ext in (".md", ".markdown") and _file_has_mermaid_fence(path)):
            targets.append(path)
    return targets


def _record_ran_check(ran_checks: list[str] | None, name: str) -> None:
    """実行された（スキップされなかった）チェック名を ran_checks に記録する（Issue #2859・#3079）."""
    if ran_checks is not None:
        ran_checks.append(name)


def _run_ruff_subcommand_check(
    *,
    subcommand: list[str],
    label: str,
    context: str,
    check_name: str,
    timing_step: str,
    project_path: Path,
    project_name: str,
    changed_py_files: list[str],
    repo: str,
    sha: str,
    token: str,
    can_post: bool,
    timing_id: str | None,
    ran_checks: list[str] | None,
) -> bool:
    """ruff format / ruff lint 共通の実行・commit status 投稿処理（Issue #2858・#3079）.

    ``subcommand``（``["ruff", "format", "--check"]`` / ``["ruff", "check"]``）だけが
    ruff format と ruff lint の違いで、それ以外の実行・投稿ロジックは同一のため共通化する。

    Returns:
        ruff が違反を検出した場合 True。
    """
    _record_ran_check(ran_checks, check_name)
    print(f"==> {label} チェックを実行します（{project_name}）...", file=sys.stderr)
    with measure_step(timing_id, timing_step):
        proc = subprocess.run(  # noqa: S603
            [
                "uv",
                "run",
                "--project",
                str(project_path),
                *subcommand,
                *changed_py_files,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
    if proc.returncode == 0:
        print(f"==> {label}: OK（{project_name}）", file=sys.stderr)
        if can_post:
            _post_commit_status(repo, sha, "success", f"{context}/{project_name}", f"{label} passed", token)
        return False

    print(f"ERROR: {label} 違反があります（{project_name}）。\n{proc.stdout}{proc.stderr}", file=sys.stderr)
    if can_post:
        _post_commit_status(repo, sha, "failure", f"{context}/{project_name}", f"{label} failed", token)
    return True


def _run_mypy_check(
    project_path: Path,
    project_name: str,
    repo: str,
    sha: str,
    token: str,
    can_post: bool,
    timing_id: str | None,
    ran_checks: list[str] | None,
) -> bool:
    """project の mypy (strict) チェックを実行する（Issue #1893・#2858・#3079）.

    Returns:
        mypy エラーがある場合 True。
    """
    _record_ran_check(ran_checks, "mypy")
    print(f"==> mypy チェックを実行します（{project_name}）...", file=sys.stderr)
    # repo root からは `src/` を解決できず config 探索も cwd 基準のため、
    # require-mypy hook（#1892）と同じく project_path を cwd に実行する。
    # 検査対象は CI（consumer の `uv run mypy src tests`）と揃えて src と tests の両方を検査し、
    # テストファイルの型注釈欠落が pre-flight を GREEN で通過して CI で FAILURE になるのを防ぐ
    # （Issue #3727・consumer mn-scripts #734 で実被害）。
    # vendor 配布された project（`tests/` 非同梱・Issue #3979）には `tests` を渡すと
    # `Cannot read file 'tests'` で mypy 自体が失敗するため、`tests/` が実在する場合のみ
    # 検査対象に含める（Issue #3991）。
    mypy_targets = ["src", "tests"] if (project_path / "tests").is_dir() else ["src"]
    with measure_step(timing_id, "preflight.mypy"):
        proc = subprocess.run(  # noqa: S603
            [
                "uv",
                "run",
                "--project",
                str(project_path),
                "mypy",
                *mypy_targets,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=300,
            cwd=str(project_path),
        )
    if proc.returncode == 0:
        print(f"==> mypy: OK（{project_name}）", file=sys.stderr)
        if can_post:
            _post_commit_status(repo, sha, "success", f"mypy/{project_name}", "mypy (strict) passed", token)
        return False

    print(
        f"ERROR: mypy (strict) エラーがあります（{project_name}）。\n{proc.stdout}{proc.stderr}",
        file=sys.stderr,
    )
    if can_post:
        _post_commit_status(repo, sha, "failure", f"mypy/{project_name}", "mypy (strict) failed", token)
    return True


def _run_project_lint_checks(
    project_dir: str,
    repo_root: Path,
    changed_files: str,
    repo: str,
    sha: str,
    token: str,
    can_post: bool,
    timing_id: str | None,
    ran_checks: list[str] | None,
) -> bool:
    """project 単位で ruff format / ruff lint / mypy を実行し commit status を投稿する（Issue #2858・#3079）.

    Returns:
        いずれかのチェックが失敗した場合 True。
    """
    project_path = repo_root / project_dir
    project_name = Path(project_dir).name
    py_file_re = re.compile(rf"^{re.escape(project_dir)}/.*\.py$")
    changed_py_files = [
        str(repo_root / f) for f in changed_files.splitlines() if py_file_re.match(f) and (repo_root / f).exists()
    ]
    has_py_files = bool(changed_py_files)
    # mypy は src tests 全体を検査するため、削除のみの .py 変更（存在しないファイル）でも実行する
    has_py_changes = any(py_file_re.match(f) for f in changed_files.splitlines())
    if not has_py_files and not has_py_changes:
        # この project は .feature 等 .py 以外の変更のみのため lint 対象外（Issue #2858）
        return False

    pyproject = project_path / "pyproject.toml"
    if not pyproject.is_file():
        print(f"==> lint skip（{project_dir} に pyproject.toml が見つかりません）", file=sys.stderr)
        return False

    failed = False

    # ── ruff format / ruff lint ─────────────────────────────────────────────
    if has_py_files:
        if _run_ruff_subcommand_check(
            subcommand=["ruff", "format", "--check"],
            label="ruff format",
            context="ruff-format",
            check_name="ruff-format",
            timing_step="preflight.ruff-format",
            project_path=project_path,
            project_name=project_name,
            changed_py_files=changed_py_files,
            repo=repo,
            sha=sha,
            token=token,
            can_post=can_post,
            timing_id=timing_id,
            ran_checks=ran_checks,
        ):
            failed = True
        if _run_ruff_subcommand_check(
            subcommand=["ruff", "check"],
            label="ruff lint",
            context="ruff-lint",
            check_name="ruff-lint",
            timing_step="preflight.ruff-lint",
            project_path=project_path,
            project_name=project_name,
            changed_py_files=changed_py_files,
            repo=repo,
            sha=sha,
            token=token,
            can_post=can_post,
            timing_id=timing_id,
            ran_checks=ran_checks,
        ):
            failed = True
    else:
        print(f"==> ruff format skip（対象 .py ファイルなし・{project_name}）", file=sys.stderr)
        print(f"==> ruff lint skip（対象 .py ファイルなし・{project_name}）", file=sys.stderr)

    # ── mypy（Issue #1893・#2858）────────────────────────────────────────
    if has_py_changes:
        if _run_mypy_check(project_path, project_name, repo, sha, token, can_post, timing_id, ran_checks):
            failed = True
    else:
        print(f"==> mypy skip（対象 .py ファイルなし・{project_name}）", file=sys.stderr)

    return failed


# hooks（.claude/hooks / templates/workflow/.claude/hooks）はプロジェクト単位の lint 対象外
# （pyproject.toml なし）のため、この定数で明示的に検査対象とする（Issue #3229）。
# 実行時には存在する dir のみを対象にフィルタする（consumer には templates/workflow/ が
# 無いため・#3693）。
_HOOKS_LINT_DIRS: tuple[str, str] = (
    ".claude/hooks",
    "templates/workflow/.claude/hooks",
)


def _run_hooks_lint_checks(
    changed_files: str,
    repo_root: Path,
    repo: str,
    sha: str,
    token: str,
    can_post: bool,
    timing_id: str | None,
    ran_checks: list[str] | None,
) -> bool:
    """`.claude/hooks/` と `templates/workflow/.claude/hooks/` の ruff/mypy を実行する（Issue #3229）.

    hooks は ``sys.path.insert`` + ``from _lib.hook_io import ...`` の動的 import を
    使うため、mypy は ``--follow-imports=skip --ignore-missing-imports`` で
    実行する（import 解決エラーを実エラーから分離する方式・#3229 の設計判断）。
    mypy には ``--native-parser`` を付与し、ruff 0.16 の py314 target が生成する
    PEP 758（``except A, B:``）を runtime Python 非依存で受理する（#3698）。

    toolchain は repo パスに依存しない（``sys.executable -m ruff`` / ``-m mypy`` で
    実行中環境から解決・#3693）。検査対象は存在する hooks dir のみで、consumer の
    ように ``templates/workflow/.claude/hooks`` が無い repo では ``.claude/hooks`` のみ検査する。

    Returns:
        いずれかのチェックが失敗した場合 True。
    """
    hooks_re = re.compile(r"^(\.claude/hooks|templates/workflow/\.claude/hooks)/.*\.py$")
    if not any(hooks_re.match(f) for f in changed_files.splitlines()):
        return False

    # 存在する hooks dir のみを検査対象にする。consumer には templates/workflow/ が
    # 無いため自動的に .claude/hooks のみが対象になる（#3693）。1 つも無ければ
    # skip して失敗しない（consumer で hooks 追加だけの PR をブロックしない）。
    hooks_dirs = [d for d in _HOOKS_LINT_DIRS if (repo_root / d).is_dir()]
    if not hooks_dirs:
        print("==> hooks lint skip（検査対象の hooks ディレクトリが存在しない）", file=sys.stderr)
        return False

    failed = False

    # ruff format / ruff lint（両 dir をまとめて実行）
    for subcommand, label, context, check_name, timing_step in (
        (["ruff", "format", "--check"], "ruff format", "ruff-format", "ruff-format", "preflight.ruff-format-hooks"),
        (["ruff", "check"], "ruff lint", "ruff-lint", "ruff-lint", "preflight.ruff-lint-hooks"),
    ):
        _record_ran_check(ran_checks, check_name)
        print(f"==> {label} チェックを実行します（hooks）...", file=sys.stderr)
        with measure_step(timing_id, timing_step):
            # toolchain は repo パスではなく実行中環境の python -m で解決する
            # （consumer のどの起動経路（uv run --project 等）でも検査可能にする・#3693）。
            proc = subprocess.run(  # noqa: S603
                [sys.executable, "-m", *subcommand, *hooks_dirs],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=120,
                cwd=str(repo_root),
            )
        if proc.returncode == 0:
            print(f"==> {label}: OK（hooks）", file=sys.stderr)
            if can_post:
                _post_commit_status(repo, sha, "success", f"{context}/hooks", f"{label} passed", token)
        else:
            print(f"ERROR: {label} 違反があります（hooks）。\n{proc.stdout}{proc.stderr}", file=sys.stderr)
            if can_post:
                _post_commit_status(repo, sha, "failure", f"{context}/hooks", f"{label} failed", token)
            failed = True

    # mypy（両 dir を別々に実行。_lib import は --follow-imports=skip で解決）
    _record_ran_check(ran_checks, "mypy")
    print("==> mypy チェックを実行します（hooks）...", file=sys.stderr)
    mypy_failed = False
    for hooks_dir in hooks_dirs:
        with measure_step(timing_id, "preflight.mypy-hooks"):
            # Issue #3698: --native-parser を付与し PEP 758（except A, B:・Python 3.14）を
            # runtime Python 非依存で受理する。ruff 0.16 の py314 target（copier の
            # min_python_version 3.14 由来）が生成する形式と mypy を整合させ、consumer の
            # hooks-lint がどちらの except 形式でも通るようにする（mypy>=2.1 で利用可）。
            proc = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "--native-parser",
                    "--follow-imports=skip",
                    "--ignore-missing-imports",
                    hooks_dir,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=300,
                cwd=str(repo_root),
            )
        if proc.returncode != 0:
            mypy_failed = True
            print(f"ERROR: mypy エラーがあります（{hooks_dir}）。\n{proc.stdout}{proc.stderr}", file=sys.stderr)
    if mypy_failed:
        if can_post:
            _post_commit_status(repo, sha, "failure", "mypy/hooks", "mypy failed", token)
        failed = True
    else:
        print("==> mypy: OK（hooks）", file=sys.stderr)
        if can_post:
            _post_commit_status(repo, sha, "success", "mypy/hooks", "mypy passed", token)

    return failed


def _run_context_budget_check(
    changed_files: str,
    repo_root: Path,
    repo: str,
    sha: str,
    token: str,
    can_post: bool,
    ran_checks: list[str] | None,
) -> None:
    """CLAUDE.md / rules の context budget（detect-rule-bloat.py）を実行する（Issue #1914・#3079）.

    exit code は格上げしない（warning 由来のため）。failure status 自体が
    Commit Status gate（Issue #831）で auto-merge をブロックする。
    """
    budget_targets = [f for f in changed_files.splitlines() if context_budget.is_gate_trigger(f, repo_root)]
    bloat_hook = repo_root / ".claude" / "hooks" / "detect-rule-bloat.py"
    if not (budget_targets and bloat_hook.is_file()):
        print("==> context-budget skip（対象 rules ファイルなし）", file=sys.stderr)
        return

    _record_ran_check(ran_checks, "context-budget")
    print("==> context budget チェックを実行します...", file=sys.stderr)
    # 削除のみの変更でも総量チェックが動くよう、存在しない場合は CLAUDE.md を anchor にする
    anchor = next(
        (repo_root / f for f in budget_targets if (repo_root / f).is_file()),
        repo_root / "CLAUDE.md",
    )
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(anchor)}})
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(bloat_hook)],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=60,
    )
    # detect-rule-bloat.py は非 blocking 設計で常に exit 0 を返すため、
    # WARN 出力中の「context budget 超過」文字列でも failure と判定する
    exceeded = proc.returncode != 0 or "context budget 超過" in (proc.stdout + proc.stderr)
    if not exceeded:
        print("==> context budget: OK", file=sys.stderr)
        if can_post:
            _post_commit_status(repo, sha, "success", "context-budget/rules", "context budget passed", token)
        return

    print(f"ERROR: context budget 超過を検出しました。\n{proc.stdout}{proc.stderr}", file=sys.stderr)
    if can_post:
        _post_commit_status(repo, sha, "failure", "context-budget/rules", "context budget exceeded", token)


def _run_gherkin_lint_check(
    has_feature_files: bool,
    repo_root: Path,
    repo: str,
    sha: str,
    token: str,
    can_post: bool,
    timing_id: str | None,
    ran_checks: list[str] | None,
) -> bool:
    """projects/py/*/tests/features を横断して gherkin-lint を実行する（Issue #2858・#3079）.

    Returns:
        gherkin-lint が違反を検出した場合 True。
    """
    py_root = repo_root / "projects" / "py"
    features_dirs = sorted(d for d in py_root.glob("*/tests/features") if d.is_dir()) if py_root.is_dir() else []
    gherkin_lintrc = repo_root / ".gherkin-lintrc"
    # npx --yes は PR のたびに npm レジストリへアクセスしレジストリ障害で
    # silent 失敗するため、ローカルインストール済みバイナリを直接実行する（Issue #1899）
    gherkin_lint_bin = repo_root / "node_modules" / ".bin" / "gherkin-lint"
    if not (has_feature_files and features_dirs):
        print("==> gherkin-lint skip（対象 .feature ファイルなし）")
        return False
    if not gherkin_lint_bin.is_file():
        print(
            "WARN: node_modules/.bin/gherkin-lint が見つかりません。"
            "リポジトリルートで `npm ci` を実行してください（gherkin-lint を skip・Issue #1899）",
            file=sys.stderr,
        )
        return False

    _record_ran_check(ran_checks, "gherkin-lint")
    print("==> gherkin-lint チェックを実行します...", file=sys.stderr)
    cmd = [str(gherkin_lint_bin), *(str(d) for d in features_dirs)]
    if gherkin_lintrc.is_file():
        cmd += ["--config", str(gherkin_lintrc)]
    with measure_step(timing_id, "preflight.gherkin-lint"):
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
            cwd=str(repo_root),
        )
    if proc.returncode == 0:
        print("==> gherkin-lint: OK", file=sys.stderr)
        if can_post:
            _post_commit_status(repo, sha, "success", "gherkin-lint/features", "gherkin-lint passed", token)
        return False

    print(f"ERROR: gherkin-lint 違反があります。\n{proc.stdout}{proc.stderr}", file=sys.stderr)
    if can_post:
        _post_commit_status(repo, sha, "failure", "gherkin-lint/features", "gherkin-lint failed", token)
    return True


def _run_mermaid_lint_check(
    changed_files: str,
    repo_root: Path,
    repo: str,
    sha: str,
    token: str,
    can_post: bool,
    timing_id: str | None,
    ran_checks: list[str] | None,
) -> bool:
    """Mermaid フェンスを含む .md/.mmd 変更に対して mermaid-lint を実行する（Issue #2794・#3079）.

    Returns:
        mermaid-lint が違反を検出した場合 True。

    Note（Issue #4138）: mermaid-lint/docs は ``detect_required_ci_contexts()`` で
    必須コンテキストとして検知される（対象ファイルが存在する場合）。node_modules/mermaid
    不在で lint 自体を実行できず skip するときも commit status を必ず投稿する
    （投稿しないと「必須なのに未送信」で merge gate を恒久的にブロックしてしまう）。
    実際の lint 結果ではなく環境未整備による skip のため success として投稿する
    （従来の「WARN + exit 0 のまま」という非ブロッキング挙動を維持する）。
    """
    mermaid_lint_script = repo_root / "scripts" / "mermaid-lint.mjs"
    mermaid_module = repo_root / "node_modules" / "mermaid"
    mermaid_targets = _get_mermaid_lint_targets(changed_files, repo_root)
    if not mermaid_targets:
        print("==> mermaid-lint skip（対象ファイルなし）", file=sys.stderr)
        return False
    if not mermaid_module.is_dir():
        print(
            "WARN: node_modules/mermaid が見つかりません。"
            "リポジトリルートで `npm ci` を実行してください（mermaid-lint を skip・Issue #2794）",
            file=sys.stderr,
        )
        if can_post:
            _post_commit_status(
                repo,
                sha,
                "success",
                "mermaid-lint/docs",
                "mermaid-lint skipped (node_modules/mermaid missing; run npm ci)",
                token,
            )
        return False

    _record_ran_check(ran_checks, "mermaid-lint")
    print("==> mermaid-lint チェックを実行します...", file=sys.stderr)
    target_strs = [str(p) for p in mermaid_targets]
    with measure_step(timing_id, "preflight.mermaid-lint"):
        proc = subprocess.run(  # noqa: S603
            ["node", str(mermaid_lint_script), *target_strs],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
            cwd=str(repo_root),
        )
    if proc.returncode == 0:
        print("==> mermaid-lint: OK", file=sys.stderr)
        if can_post:
            _post_commit_status(repo, sha, "success", "mermaid-lint/docs", "mermaid-lint passed", token)
        return False

    print(f"ERROR: mermaid-lint 違反があります。\n{proc.stdout}{proc.stderr}", file=sys.stderr)
    if can_post:
        _post_commit_status(repo, sha, "failure", "mermaid-lint/docs", "mermaid-lint failed", token)
    return True


def _run_health_check(
    changed_files: str,
    repo_root: Path,
    tidd_tools_dir: Path,
    repo: str,
    sha: str,
    token: str,
    can_post: bool,
    timing_id: str | None,
    ran_checks: list[str] | None,
) -> bool:
    """tidd_tools health-check（drift 検知）を実行する（Issue #2213・#3079）.

    Returns:
        health-check が drift を検出した場合 True。
    """
    if not _has_health_check_targets(changed_files):
        print("==> health-check skip（対象パス変更なし）", file=sys.stderr)
        return False
    if not tidd_tools_dir.is_dir():
        print("==> health-check skip（tidd_tools ディレクトリ未検出）", file=sys.stderr)
        return False

    _record_ran_check(ran_checks, "health-check")
    print("==> health-check を実行します...", file=sys.stderr)
    with measure_step(timing_id, "preflight.health-check"):
        proc = subprocess.run(  # noqa: S603
            [
                "uv",
                "run",
                "--project",
                str(tidd_tools_dir),
                "python",
                "-m",
                "tidd_tools",
                "health-check",
                "--repo-root",
                str(repo_root),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
    if proc.returncode == 0:
        print("==> health-check: OK", file=sys.stderr)
        if can_post:
            _post_commit_status(repo, sha, "success", "health-check/tidd_tools", "health-check passed", token)
        return False

    failure_lines = [
        line for line in proc.stderr.splitlines() if line.strip().startswith(_HEALTH_CHECK_FAILURE_PREFIXES)
    ]
    print(f"ERROR: health-check で drift を検出しました。\n{proc.stdout}{proc.stderr}", file=sys.stderr)
    # 実行時クラッシュ（既知 prefix なしで非ゼロ終了）を「0 issue(s)」と誤表示しないよう
    # 区別する（agy レビュー指摘・PR #2235）。
    description = (
        f"health-check failed ({len(failure_lines)} issue(s))"
        if failure_lines
        else "health-check failed (execution error)"
    )
    if can_post:
        _post_commit_status(repo, sha, "failure", "health-check/tidd_tools", description, token)
    return True


def _run_prettier_css_check(
    has_css_files: bool,
    css_files: list[str],
    repo_root: Path,
    repo: str,
    sha: str,
    token: str,
    can_post: bool,
    timing_id: str | None,
    ran_checks: list[str] | None,
) -> bool:
    """config/ 配下の変更 CSS に対して prettier --check を実行する（Issue #2501・#3079）.

    Returns:
        prettier が差分を検出した場合 True。
    """
    if not has_css_files:
        print("==> prettier/css skip（対象 CSS ファイルなし）", file=sys.stderr)
        return False
    if not css_files:
        print("==> prettier/css skip（存在する対象 CSS ファイルなし）", file=sys.stderr)
        return False

    prettier_bin = repo_root / "node_modules" / ".bin" / "prettier"
    if not prettier_bin.is_file():
        print(
            "WARN: node_modules/.bin/prettier が見つかりません。"
            "リポジトリルートで `npm ci` を実行してください（prettier/css を skip・Issue #2501）",
            file=sys.stderr,
        )
        return False

    _record_ran_check(ran_checks, "prettier-css")
    print("==> prettier/css チェックを実行します...", file=sys.stderr)
    with measure_step(timing_id, "preflight.prettier-css"):
        proc = subprocess.run(  # noqa: S603
            [str(prettier_bin), "--check", *css_files],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
            cwd=str(repo_root),
        )
    if proc.returncode == 0:
        print("prettier/css: passed (0 problems)")
        if can_post:
            _post_commit_status(repo, sha, "success", "prettier/css", "prettier/css passed", token)
        return False

    print(f"ERROR: prettier/css 差分があります。\n{proc.stdout}{proc.stderr}", file=sys.stderr)
    if can_post:
        _post_commit_status(repo, sha, "failure", "prettier/css", "prettier/css failed", token)
    return True


def _run_stylelint_css_check(
    has_css_files: bool,
    css_files: list[str],
    repo_root: Path,
    repo: str,
    sha: str,
    token: str,
    can_post: bool,
    timing_id: str | None,
    ran_checks: list[str] | None,
) -> bool:
    """config/ 配下の変更 CSS に対して stylelint を実行する（Issue #2501・#3079）.

    Returns:
        stylelint が違反を検出した場合 True。
    """
    if not has_css_files:
        print("==> stylelint/css skip（対象 CSS ファイルなし）", file=sys.stderr)
        return False
    if not css_files:
        print("==> stylelint/css skip（存在する対象 CSS ファイルなし）", file=sys.stderr)
        return False

    stylelint_bin = repo_root / "node_modules" / ".bin" / "stylelint"
    if not stylelint_bin.is_file():
        print(
            "WARN: node_modules/.bin/stylelint が見つかりません。"
            "リポジトリルートで `npm ci` を実行してください（stylelint/css を skip・Issue #2501）",
            file=sys.stderr,
        )
        return False

    _record_ran_check(ran_checks, "stylelint-css")
    print("==> stylelint/css チェックを実行します...", file=sys.stderr)
    with measure_step(timing_id, "preflight.stylelint-css"):
        proc = subprocess.run(  # noqa: S603
            [str(stylelint_bin), *css_files],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
            cwd=str(repo_root),
        )
    if proc.returncode == 0:
        print("stylelint/css: passed (0 problems)")
        if can_post:
            _post_commit_status(repo, sha, "success", "stylelint/css", "stylelint/css passed", token)
        return False

    print(f"ERROR: stylelint/css 違反があります。\n{proc.stdout}{proc.stderr}", file=sys.stderr)
    if can_post:
        _post_commit_status(repo, sha, "failure", "stylelint/css", "stylelint/css failed", token)
    return True


def _post_lint_statuses(
    changed_files: str,
    repo: str,
    sha: str,
    token: str,
    repo_root: Path,
    initial_exit: int,
    *,
    timing_id: str | None = None,
    ran_checks: list[str] | None = None,
) -> int:
    """linter/formatter を実行し GitHub Commit Status を投稿する（Issue #1851）.

    対象:
    - ruff format  → ``ruff-format/<project>``（Issue #2858・``projects/py/`` 配下の変更 project ごと）
    - ruff lint    → ``ruff-lint/<project>``（同上）
    - mypy         → ``mypy/<project>``（Issue #1893・#2858）
    - context budget → ``context-budget/rules``（Issue #1914）
    - gherkin-lint → ``gherkin-lint/features``（Issue #2858・``projects/py/*/tests/features`` を横断）
    - mermaid-lint → ``mermaid-lint/docs``（Issue #2794・Mermaid フェンスを含む .md/.mmd 変更時のみ実行）
    - health-check  → ``health-check/tidd_tools``（Issue #2213・対象パス変更時のみ実行。tidd_tools 固定のまま。
      Issue #2858 の project 別汎用化のスコープ外: 他 project のコード品質検査ではなく tidd_tools 自身が持つ
      repo 全体のドリフト検知ツールのため）

    ruff-format/ruff-lint/mypy は ``test_plan._detect_projects`` と同じ方式で
    ``projects/py/<project>`` を動的検出し、project ごとに ``pyproject.toml`` の
    存在を確認したうえで実行する（存在しない project は skip・Issue #2858）。

    ``timing_id`` が指定された場合のみ（Issue #2101・pre-flight からの呼び出し）
    ruff format / ruff lint / mypy / gherkin-lint の各ステップを
    ``preflight.<step>`` として統一日誌（``timing_log``・#2100/#2936）に記録する。
    ``timing_id`` 省略時（``tidd test-plan`` 等の既存呼び出し）は一切記録しない。

    ``ran_checks`` にリストを渡すと（Issue #2859）、実際に実行された（スキップされなかった）
    チェック名（``"ruff-format"``・``"ruff-lint"``・``"mypy"``・``"context-budget"``・
    ``"gherkin-lint"``・``"mermaid-lint"``・``"health-check"``・``"prettier-css"``・
    ``"stylelint-css"``）を追記する。``tidd pre-flight`` がこれを自己記録の ``checks`` フィールドに
    含め、``merge-summary report`` の「検証・テスト」行に実施チェック内容を反映するために使う。
    ``None``（省略）時は一切追記しない（既存呼び出し互換）。

    個別チェックの実行・commit status 投稿処理は ``_run_project_lint_checks`` /
    ``_run_context_budget_check`` / ``_run_gherkin_lint_check`` / ``_run_mermaid_lint_check`` /
    ``_run_health_check`` / ``_run_prettier_css_check`` / ``_run_stylelint_css_check`` に
    分割している（Issue #3079・C901 baseline drain）。

    Returns:
        更新後の exit code（lint 失敗で 1 に格上げ）
    """
    exit_code = initial_exit

    # sha / token がなければ lint は実行するが Commit Status 投稿は行わない
    can_post = bool(sha and token)

    has_feature_files = bool(re.search(r"\.feature$", changed_files, re.MULTILINE))

    tidd_tools_dir = repo_root / "projects" / "py" / "tidd_tools"

    # ── ruff format / ruff lint / mypy（project 別・Issue #2858）──────────────
    # test_plan._detect_projects と同じ方式で projects/py/<project> を動的検出し、
    # project ごとに ruff-format/<project>・ruff-lint/<project>・mypy/<project> を投稿する。
    py_projects = _test_plan_mod._detect_projects(changed_files.splitlines(), "projects/py/")
    if not py_projects:
        # 後方互換: projects/py/ 配下の変更が一切ない場合は project 名なしの
        # 汎用スキップメッセージを 1 回ずつ出す（既存テストの固定アサーション互換）。
        print("==> ruff format skip（対象 .py ファイルなし）", file=sys.stderr)
        print("==> ruff lint skip（対象 .py ファイルなし）", file=sys.stderr)
        print("==> mypy skip（対象 .py ファイルなし）", file=sys.stderr)

    for project_dir in py_projects:
        if _run_project_lint_checks(
            project_dir, repo_root, changed_files, repo, sha, token, can_post, timing_id, ran_checks
        ):
            exit_code = 1

    # ── hooks の ruff / mypy（Issue #3229）────────────────────────────────────
    # `.claude/hooks/` と `templates/workflow/.claude/hooks/` はプロジェクトではなく
    # pyproject.toml を持たないため、tidd_tools の uv 環境から直接検査する。
    if _run_hooks_lint_checks(changed_files, repo_root, repo, sha, token, can_post, timing_id, ran_checks):
        exit_code = 1

    # ── context budget（Issue #1914）─────────────────────────────────────────
    _run_context_budget_check(changed_files, repo_root, repo, sha, token, can_post, ran_checks)

    # ── gherkin-lint ─────────────────────────────────────────────────────────
    if _run_gherkin_lint_check(has_feature_files, repo_root, repo, sha, token, can_post, timing_id, ran_checks):
        exit_code = 1

    # ── mermaid-lint（Issue #2794）────────────────────────────────────────────
    if _run_mermaid_lint_check(changed_files, repo_root, repo, sha, token, can_post, timing_id, ran_checks):
        exit_code = 1

    # ── health-check（Issue #2213）────────────────────────────────────────────
    if _run_health_check(changed_files, repo_root, tidd_tools_dir, repo, sha, token, can_post, timing_id, ran_checks):
        exit_code = 1

    # ── prettier/css・stylelint/css（Issue #2501）────────────────────────────
    has_css_files = any(re.match(r"^config/.*\.css$", f) for f in changed_files.splitlines())
    # 存在するファイルのみを対象にする（削除コミット時に空リストになる場合に備えて先頭で 1 回だけ算出）
    css_files = [
        str(repo_root / f)
        for f in changed_files.splitlines()
        if re.match(r"^config/.*\.css$", f) and (repo_root / f).exists()
    ]
    if _run_prettier_css_check(has_css_files, css_files, repo_root, repo, sha, token, can_post, timing_id, ran_checks):
        exit_code = 1
    if _run_stylelint_css_check(has_css_files, css_files, repo_root, repo, sha, token, can_post, timing_id, ran_checks):
        exit_code = 1

    return exit_code
