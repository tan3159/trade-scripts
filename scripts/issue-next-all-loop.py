#!/usr/bin/env python3
"""Run the unattended issue-next-all workflow with durable retry behavior."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_MAX_RUN_SECONDS = 5 * 60 * 60
DEFAULT_QUOTA_RETRY_SECONDS = 60 * 60
QUOTA_PATTERN = re.compile(
    r"quota|rate limit|usage limit|too many requests|resets? in|5[- ]hour",
    re.IGNORECASE,
)
NEXT_UNATTENDED_CMD = ("tidd", "issue-next-state", "next-unattended")
_ACTIVE_PROCESS: subprocess.Popen[str] | None = None


class RunResult(NamedTuple):
    returncode: int | None
    output: str
    timed_out: bool = False


def build_codex_command(repo: Path) -> list[str]:
    """Build a non-interactive command using the current Codex CLI flags."""

    return [
        "codex",
        "exec",
        "--cd",
        str(repo),
        "--dangerously-bypass-approvals-and-sandbox",
        "$issue-next-all",
    ]


def run_process(
    command: list[str], *, timeout_seconds: int, cwd: Path | None = None
) -> RunResult:
    """Run a child process, preserving partial output when the run is capped."""

    global _ACTIVE_PROCESS
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        cwd=cwd,
    )
    _ACTIVE_PROCESS = process
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        process.terminate()
        try:
            tail, _ = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            tail, _ = process.communicate()
        partial = error.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        if tail.startswith(partial):
            combined = tail
        elif partial.endswith(tail):
            combined = partial
        else:
            combined = partial + tail
        return RunResult(None, combined, timed_out=True)
    finally:
        _ACTIVE_PROCESS = None
    return RunResult(process.returncode, output)


def has_quota_signal(output: str) -> bool:
    return bool(QUOTA_PATTERN.search(output))


def quota_wait_seconds(output: str, fallback: int) -> int:
    """Use an advertised reset interval when available, with a safe fallback."""

    match = re.search(
        r"resets? in\s+(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?",
        output,
        re.IGNORECASE,
    )
    if not match:
        return fallback
    hours, minutes, seconds = (int(value or 0) for value in match.groups())
    return max(fallback, hours * 3600 + minutes * 60 + seconds + 60)


def has_no_candidate_signal(output: str) -> bool:
    return any(
        marker in output
        for marker in (
            "着手可能な Issue はありません",
            "全件完了",
            "No eligible issues",
        )
    )


class NextUnattendedFailedError(RuntimeError):
    """`next-unattended` 自体の失敗と候補なしを区別する."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        super().__init__(
            f"tidd issue-next-state next-unattended が失敗しました "
            f"(exit code={returncode}): {stderr.strip()}"
        )


def has_actionable_issue(repo: str) -> bool:
    """互換用の1周分API。候補確認の失敗を正常終了と混同しない."""

    result = subprocess.run(
        NEXT_UNATTENDED_CMD,
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise NextUnattendedFailedError(result.returncode, result.stderr)
    return bool(result.stdout.strip())


def run_once(repo: str) -> int:
    """互換用の1周分API。詳細な待機・再開は `main` が担当する."""

    try:
        if not has_actionable_issue(repo):
            return 0
    except NextUnattendedFailedError as exc:
        print(str(exc), file=sys.stderr)
        return exc.returncode
    return subprocess.run(build_codex_command(Path(repo)), check=False).returncode


def next_candidate(repo: Path, tidd_project: str) -> RunResult:
    command = [
        "uv",
        "run",
        "--project",
        tidd_project,
        "tidd",
        "issue-next-state",
        "next-unattended",
    ]
    return run_process(command, timeout_seconds=120, cwd=repo)


def sleep_with_log(seconds: int, *, reason: str, log) -> None:
    log(f"待機 {seconds} 秒（理由: {reason}）")
    time.sleep(seconds)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Codex issue-next-all を任意のリポジトリで反復実行する"
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(os.environ.get("ISSUE_NEXT_REPO", Path.cwd())),
        help="対象リポジトリ（既定: 現在のディレクトリ）",
    )
    parser.add_argument(
        "--tidd-project",
        default="projects/py/tidd_tools",
        help="対象リポジトリ内の tidd_tools プロジェクトパス",
    )
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--max-run-seconds", type=int, default=DEFAULT_MAX_RUN_SECONDS)
    parser.add_argument(
        "--quota-retry-seconds", type=int, default=DEFAULT_QUOTA_RETRY_SECONDS
    )
    parser.add_argument(
        "--keep-running-when-empty",
        action="store_true",
        help="候補がなくても待機して再確認する",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    if not args.repo.is_dir():
        print(f"エラー: リポジトリがありません: {args.repo}", file=sys.stderr)
        return 2
    if min(args.interval, args.max_run_seconds, args.quota_retry_seconds) < 0:
        print("エラー: 待機時間・実行上限は0以上で指定してください", file=sys.stderr)
        return 2

    def log(message: str) -> None:
        print(f"[issue-next-all-loop] {message}", flush=True)

    stop_requested = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True
        if _ACTIVE_PROCESS is not None and _ACTIVE_PROCESS.poll() is None:
            _ACTIVE_PROCESS.terminate()
        log("停止要求を受信しました。現在のCodex実行完了後に終了します")

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    while not stop_requested:
        candidate = next_candidate(args.repo, args.tidd_project)
        if candidate.returncode == 0 and not candidate.output.strip():
            if not args.keep_running_when_empty:
                log("着手可能な Issue がありません。正常終了します")
                return 0
            sleep_with_log(args.interval, reason="候補なし。再確認", log=log)
            continue
        if candidate.returncode not in (0, None):
            log(
                f"候補確認に失敗しました（exit={candidate.returncode}）。次周回で再試行します"
            )
            sleep_with_log(args.interval, reason="候補確認失敗。再試行", log=log)
            continue
        elif candidate.output.strip():
            log(f"次候補: {candidate.output.strip()}")

        result = run_process(
            build_codex_command(args.repo), timeout_seconds=args.max_run_seconds
        )
        if result.output:
            print(
                result.output,
                end="" if result.output.endswith("\n") else "\n",
                flush=True,
            )
        if has_no_candidate_signal(result.output) and not args.keep_running_when_empty:
            log("Issue が尽きたため正常終了します")
            return 0
        if stop_requested:
            break
        if result.timed_out:
            sleep_with_log(
                args.quota_retry_seconds,
                reason="5時間実行上限に到達。stateを保持して再開",
                log=log,
            )
        elif has_quota_signal(result.output):
            wait_seconds = quota_wait_seconds(result.output, args.quota_retry_seconds)
            sleep_with_log(
                wait_seconds, reason="クォータ／rate limit検出。回復後に再試行", log=log
            )
        else:
            sleep_with_log(
                args.interval, reason=f"Codex終了（exit={result.returncode}）", log=log
            )
    return 130


if __name__ == "__main__":
    raise SystemExit(main())
