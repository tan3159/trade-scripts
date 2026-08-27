"""`tidd report-test-status` サブコマンド（旧 `scripts/report-test-status.sh` の Python 移植）.

Jest / pytest テスト結果を GitHub Commit Status API で通知する。

`CONTEXT=jest/<project>` または `CONTEXT=pytest/<project>` を環境変数で渡し、
`TEST_PROJECT_DIR` でプロジェクトディレクトリを指定して実行する。

`CONTEXT=bats/*` は Issue #831 / #962 で廃止された。本サブコマンドで bats モードが
呼ばれた場合は exit 1 で拒否し、`${STATE_DIR}/blocked-bats-calls.log` にフォレンジック
情報を追記する（旧 sh と同じ挙動）。

必須環境変数:
- `GITHUB_TOKEN` (または `GH_TOKEN`)
- `REPO`
- `SHA`

任意環境変数:
- `TEST_PROJECT_DIR`
- `STATE_DIR`

終了コード:
- 0 → テスト全通過 + API 通知成功
- 1 → 環境変数未設定 / API エラー / bats モード呼び出し拒否
- 2 → テスト失敗（GitHub には failure 通知済み）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import SubprocessTimeoutError
from tidd_tools.shared.subprocess_runner import run as run_subprocess
from tidd_tools.shared.subprocess_timeouts import TOOL_VERSION_TIMEOUT_SEC, test_suite_timeout_sec


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "report-test-status",
        help="Jest/pytest 結果を GitHub Commit Status に通知する（旧 scripts/report-test-status.sh）",
        description=__doc__,
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    return execute(env=os.environ)


def execute(*, env: Mapping[str, str]) -> int:
    context = env.get("CONTEXT", "bats/local")

    # GITHUB_TOKEN → GH_TOKEN フォールバック
    github_token = env.get("GITHUB_TOKEN") or ""
    if not github_token:
        gh_token = env.get("GH_TOKEN") or ""
        if gh_token:
            github_token = gh_token
        else:
            print("ERROR: GITHUB_TOKEN が設定されていません", file=sys.stderr)
            print("HINT: export GITHUB_TOKEN=$(pass github/pat) 等で設定してください", file=sys.stderr)
            print("HINT: GH_TOKEN が設定済みの場合は GITHUB_TOKEN=$GH_TOKEN で渡してください", file=sys.stderr)
            return 1

    repo = env.get("REPO") or ""
    if not repo:
        print("ERROR: REPO が設定されていません", file=sys.stderr)
        print("HINT: export REPO=being-gaia-plan/ai-dev-handbook 等で設定してください", file=sys.stderr)
        return 1

    sha = env.get("SHA") or ""
    if not sha:
        print("ERROR: SHA が設定されていません", file=sys.stderr)
        print("HINT: export SHA=$(git rev-parse HEAD) 等で設定してください", file=sys.stderr)
        return 1

    # ── ログディレクトリ ─────────────────────────────────────────────────
    state_dir_env = env.get("STATE_DIR") or ""
    log_dir = Path(state_dir_env) if state_dir_env else Path(tempfile.mkdtemp(prefix="report-test-status-"))
    log_dir.mkdir(parents=True, exist_ok=True)

    # ── bats モード拒否 ──────────────────────────────────────────────────
    if context.startswith("bats/"):
        _log_blocked_bats_call(context=context, log_dir=log_dir, env=env)
        _print_blocked_bats_messages(log_dir=log_dir)
        return 1

    # ── ランナー選択 ───────────────────────────────────────────────────
    if context.startswith("jest/"):
        runner = "jest"
    elif context.startswith("pytest/"):
        runner = "pytest"
    else:
        print(f"ERROR: 未対応の CONTEXT です: {context}", file=sys.stderr)
        print("HINT: jest/<project> または pytest/<project> を指定してください", file=sys.stderr)
        return 1

    project_dir_env = env.get("TEST_PROJECT_DIR") or ""
    if not project_dir_env:
        print(f"ERROR: CONTEXT={context} では TEST_PROJECT_DIR が必要です", file=sys.stderr)
        return 1
    project_dir = Path(project_dir_env)

    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    test_log = log_dir / f"test-run-{ts}.log"

    # ── テスト実行 ─────────────────────────────────────────────────────
    test_exit, test_duration = _run_tests(runner=runner, project_dir=project_dir, test_log=test_log)
    if test_exit < 0:
        # 起動前バリデーション失敗
        return 1

    # ── timing.json 追記 ───────────────────────────────────────────
    timing_file = log_dir / "timing.json"
    _update_timing_file(timing_file=timing_file, duration=test_duration)

    # ── GitHub Commit Status 通知 ─────────────────────────────────
    state = "success" if test_exit == 0 else "failure"
    description = "All tests passed" if test_exit == 0 else "Tests failed"
    print(
        f"==> GitHub Commit Status を送信中: state={state}, context={context}",
        file=sys.stderr,
    )

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

    return 2 if test_exit != 0 else 0


# ── テスト実行 ──────────────────────────────────────────────────────────


def _run_tests(*, runner: str, project_dir: Path, test_log: Path) -> tuple[int, int]:
    """Jest または pytest を実行して (exit_code, duration_seconds) を返す.

    起動前バリデーション失敗は (-1, 0) を返す。
    """
    if runner == "jest":
        package_json = project_dir / "package.json"
        if not package_json.is_file():
            print(f"ERROR: {project_dir}/package.json が見つかりません", file=sys.stderr)
            return -1, 0
        print(f"==> Jest テスト実行中: {project_dir}", file=sys.stderr)
        start = int(time.time())
        result = run_subprocess(["npx", "jest"], cwd=project_dir, timeout=test_suite_timeout_sec())
        duration = int(time.time()) - start
        _write_log(test_log, result.stdout, result.stderr)
        if result.returncode == 0:
            print("==> Jest テスト成功", file=sys.stderr)
        else:
            print("==> Jest テスト失敗", file=sys.stderr)
        return result.returncode, duration

    # pytest
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        print(f"ERROR: {project_dir}/pyproject.toml が見つかりません", file=sys.stderr)
        return -1, 0
    print(f"==> pytest テスト実行中: {project_dir}", file=sys.stderr)
    start = int(time.time())
    # `uv run --project <project_dir> pytest` で各プロジェクトの dev 依存を解決する
    result = run_subprocess(
        _build_pytest_cmd(project_dir),
        cwd=project_dir,
        timeout=test_suite_timeout_sec(),
    )
    duration = int(time.time()) - start
    _write_log(test_log, result.stdout, result.stderr)
    if result.returncode == 0:
        print("==> pytest テスト成功", file=sys.stderr)
    else:
        print("==> pytest テスト失敗", file=sys.stderr)
    return result.returncode, duration


def _build_pytest_cmd(project_dir: Path) -> list[str]:
    """pytest 起動コマンドを組み立てる（Issue #3280・#2969）.

    pre-flight / ai-review では slow マーカー付きテスト（copier E2E・mypy フル実行・
    uv sync 等）を除外して実行時間と資源使用を抑える設計（#2969）。
    `test_plan._build_pytest_cmd` と同一の `-m "not slow"` 基準を適用する。
    """
    return ["uv", "run", "--project", str(project_dir), "pytest", "-m", "not slow"]


def _write_log(log_path: Path, stdout: str, stderr: str) -> None:
    """旧 sh の `... 2>&1 > "$_TEST_LOG"` に合わせ、stdout / stderr をマージして保存."""
    try:
        with log_path.open("w", encoding="utf-8") as fh:
            fh.write(stdout)
            if stderr:
                fh.write(stderr)
    except OSError:
        pass


# ── timing.json ──────────────────────────────────────────────────────────


def _update_timing_file(*, timing_file: Path, duration: int) -> None:
    """`bats_duration` フィールドを追加・更新する（Issue #307 互換）."""
    if not timing_file.is_file():
        timing_file.parent.mkdir(parents=True, exist_ok=True)
        timing_file.write_text(
            json.dumps({"bats_duration": duration}) + "\n",
            encoding="utf-8",
        )
        return

    try:
        content = timing_file.read_text(encoding="utf-8")
    except OSError:
        return

    lines = content.splitlines()
    if not lines:
        timing_file.write_text(
            json.dumps({"bats_duration": duration}) + "\n",
            encoding="utf-8",
        )
        return

    # 最終行を JSON 更新
    last = lines[-1]
    try:
        data = json.loads(last)
        if isinstance(data, dict):
            data["bats_duration"] = duration
            updated = json.dumps(data)
            new_lines = lines[:-1] + [updated]
            timing_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            return
    except json.JSONDecodeError:
        pass

    # 最終行が JSON ではない場合は新規行として追加
    new_lines = [*lines, json.dumps({"bats_duration": duration})]
    timing_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ── bats モード拒否 ──────────────────────────────────────────────────────


def _log_blocked_bats_call(
    *,
    context: str,
    log_dir: Path,
    env: Mapping[str, str],
) -> None:
    blocked_log = log_dir / "blocked-bats-calls.log"
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [
        "---- blocked bats call ----",
        f"timestamp={ts}",
        f"CONTEXT={context}",
        f"PPID={os.getppid()}",
        f"argv={' '.join(sys.argv)}",
        f"env._AI_REVIEW_RUNNING={env.get('_AI_REVIEW_RUNNING', '')}",
        f"env.SKIP_BATS_IN_TEST={env.get('SKIP_BATS_IN_TEST', '')}",
        f"env.STATE_DIR={env.get('STATE_DIR', '')}",
        f"env.PR_NUM={env.get('PR_NUM', '')}",
        f"env.AI_REVIEW_BACKEND={env.get('AI_REVIEW_BACKEND', '')}",
        f"env.BATS_TEST_TMPDIR={env.get('BATS_TEST_TMPDIR', '')}",
        f"env.BATS_RUN_TMPDIR={env.get('BATS_RUN_TMPDIR', '')}",
    ]

    # 親プロセス情報
    parent_info = _read_parent_ps(os.getppid())
    lines.append(f"parent_ps={parent_info}")

    # プロセスツリー（pstree が無ければ ps を 3 段辿る）。診断ログ内なので
    # 補助コマンドの TimeoutExpired が本処理を落とさないよう明示的に catch する。
    if _has_command("pstree"):
        try:
            result = run_subprocess(["pstree", "-p", str(os.getppid())], timeout=TOOL_VERSION_TIMEOUT_SEC)
        except SubprocessTimeoutError:
            lines.append("pstree=(timeout)")
        else:
            if result.returncode == 0:
                lines.append("pstree=")
                lines.append(result.stdout.rstrip())
    else:
        lines.append("ps_chain=")
        cur_pid = str(os.getppid())
        for depth in (1, 2, 3):
            if not cur_pid or cur_pid in ("0", "1"):
                break
            try:
                ps = run_subprocess(["ps", "-o", "pid=,ppid=,cmd=", "-p", cur_pid], timeout=TOOL_VERSION_TIMEOUT_SEC)
            except SubprocessTimeoutError:
                lines.append(f"  depth={depth}: (timeout)")
                break
            if ps.returncode != 0 or not ps.stdout.strip():
                break
            lines.append(f"  depth={depth}: {ps.stdout.rstrip()}")
            try:
                ppid = run_subprocess(["ps", "-o", "ppid=", "-p", cur_pid], timeout=TOOL_VERSION_TIMEOUT_SEC)
            except SubprocessTimeoutError:
                break
            if ppid.returncode != 0:
                break
            cur_pid = ppid.stdout.strip()

    lines.append("---- end ----")

    try:
        with blocked_log.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


def _read_parent_ps(ppid: int) -> str:
    result = run_subprocess(["ps", "-o", "pid,ppid,pgid,sess,cmd", "-p", str(ppid)], timeout=TOOL_VERSION_TIMEOUT_SEC)
    if result.returncode != 0:
        return "(unavailable)"
    out_lines = result.stdout.splitlines()
    if len(out_lines) < 2:
        return "(unavailable)"
    return out_lines[1].strip()


def _has_command(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def _print_blocked_bats_messages(*, log_dir: Path) -> None:
    blocked_log = log_dir / "blocked-bats-calls.log"
    print("ERROR: bats モードは廃止されました（Issue #831 / #962）", file=sys.stderr)
    print(
        "HINT: bats のタグ選択実行と Commit Status 投稿は "
        "tidd_tools ai-review と tidd_tools test-plan に統合されています",
        file=sys.stderr,
    )
    print(
        "HINT: bats 結果は ${STATE_DIR}/bats-status 経由で "
        "tidd_tools ai-review が GitHub Commit Status に直接 POST します",
        file=sys.stderr,
    )
    print(
        "HINT: report-test-status は CONTEXT=jest/* または CONTEXT=pytest/* のみ受け付けます",
        file=sys.stderr,
    )
    print(
        f"HINT: 呼び出し元 PPID={os.getppid()}・フォレンジック情報は {blocked_log} に追記しました",
        file=sys.stderr,
    )


# ── GitHub Commit Status POST ───────────────────────────────────────────


def _post_commit_status(
    *,
    repo: str,
    sha: str,
    token: str,
    state: str,
    context: str,
    description: str,
) -> bool:
    url = f"https://api.github.com/repos/{repo}/statuses/{sha}"
    body = json.dumps({"state": state, "context": context, "description": description}).encode("utf-8")
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
        except Exception:  # pragma: no cover
            err_body = str(exc)
        print(err_body, file=sys.stderr)
        return False
    except urllib.error.URLError as exc:
        print("ERROR: GitHub Commit Status API の呼び出しに失敗しました", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return False
