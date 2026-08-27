"""`tidd classify-test-failure` サブコマンド（Issue #3631）.

既存テスト失敗の突合判定（2 条件 AND）を機械実行し、判定結果を exit code で返す。
従来 `.claude/skills/issue-next/existing-test-failure.md`「突合判定」節で LLM が
複数コマンド出力を目で突き合わせて AND を取っていた処理を 1 コマンドに閉じる。

終了コード:
- 0: 既存問題（条件①かつ②が成立）
- 1: PR / 対象 Issue 起因（条件①不成立、または②不成立）
- 2: 判定不能（失敗ファイルパスを特定できない等）

stdout には判定根拠を JSON 1 行で出力する（キー: `verdict` / `failed_files` /
`changed_files` / `condition1` / `condition2`。`verdict` は `existing` /
`pr_issue` / `unknown` / `crash_unrecoverable` のいずれか）。

`crash_unrecoverable`（#3908）: pytest が `INTERNALERROR`（pytest-xdist worker crash 等）で
サマリ行（`FAILED <path>::<test>`）を一切出力せずクラッシュ終了したケース。ファイルパス抽出
自体に失敗した通常の `unknown` とは stderr メッセージで区別できる。exit code は `unknown` と
同じ `EXIT_UNKNOWN`（判定不能である点は変わらないため）。

条件:
- 条件①: 失敗ファイルパスが変更ファイル一覧に 1 つも含まれない（集合の積が空）
- 条件②: origin/main 単体チェックアウトで同じ失敗が再現する

起点（情報源の違いで分岐）:
- 起点 A（`--pr <N>`）: 失敗ファイルは PR head SHA の commit status（FAILURE/ERROR）
  と対応する CI ログから抽出。変更ファイル一覧は PR のファイル一覧 API から取得。
- 起点 B（`--issue <N>`）: 失敗ファイルは `tidd pre-flight` の失敗チェックの
  再実行結果から抽出。変更ファイル一覧は `git diff --name-only origin/main` から取得。

条件②'（環境依存フレーキーテスト判定・#2094）の機械化は行わない。本コマンドは
条件②不成立時に exit 1 を返し、②' の検討は従来どおり skill 側の LLM 判断に残す。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from tidd_tools import test_plan
from tidd_tools.shared import gh_client
from tidd_tools.shared.cli import add_common_flags, check_dry_run_not_implemented
from tidd_tools.shared.errors import GitCommandError, SubprocessTimeoutError
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)

#: 既存問題（条件①かつ②が成立）
EXIT_EXISTING = 0
#: PR / 対象 Issue 起因（条件①不成立、または②不成立）
EXIT_PR_ISSUE = 1
#: 判定不能（失敗ファイルパスを特定できない等）
EXIT_UNKNOWN = 2

#: commit status context のうちテスト系を表す prefix（ai_review.test_status_gate と同様）
_TEST_CONTEXT_RE = re.compile(r"^(pytest|jest)/")
#: commit status API から FAILURE/ERROR の context 名を取り出す jq
_FAILED_CONTEXT_JQ = '.statuses[] | select(.state=="failure" or .state=="error") | .context'
#: `gh run list` から failed な workflow run の databaseId を取り出す jq
_FAILED_RUN_JQ = '.[] | select(.conclusion=="failure") | .databaseId'
#: CI ログの失敗行（pytest: FAILED / jest: FAIL）からテストファイルパスを抽出する
_FAILED_LOG_FILE_RE = re.compile(r"\b(?:FAILED|FAIL)\s+([^\s:]+)")
#: ruff / mypy のエラー行冒頭からファイルパスを抽出する
_LINT_FILE_RE = re.compile(r"^([^: ]+\.py)", re.MULTILINE)
#: pytest の INTERNALERROR クラッシュ出力を検出する（#3908）
_INTERNALERROR_RE = re.compile(r"INTERNALERROR")
#: pytest の通常サマリ行（`FAILED <path>::<test>`）の存在確認用（#3908）
_FAILED_SUMMARY_TOKEN_RE = re.compile(r"\bFAILED\s")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "classify-test-failure",
        help="既存テスト失敗の突合判定（条件①② AND）を機械実行して exit code で返す",
        description=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--pr",
        type=int,
        default=None,
        metavar="N",
        help="起点 A: PR 番号（commit status と CI ログから失敗ファイルを抽出）",
    )
    group.add_argument(
        "--issue",
        type=int,
        default=None,
        metavar="N",
        help="起点 B: Issue 番号（pre-flight の失敗チェック再実行から失敗ファイルを抽出）",
    )
    add_common_flags(parser, dry_run_supported=False)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    if check_dry_run_not_implemented(args, subcmd="classify-test-failure"):
        return EXIT_UNKNOWN

    repo_root = _resolve_repo_root()
    repo = os.environ.get("REPO") or gh_client.repo_name_with_owner()

    if args.pr is not None:
        changed_files = _changed_files_from_pr(args.pr, repo)
        failed_files, crashed = _failed_files_from_pr(args.pr, repo)
        check_names: list[str] = []
        mode = "pr"
        pr_num = args.pr
    else:
        changed_files = _changed_files_from_diff(repo_root)
        check_names = _preflight_failed_check_names(args.issue, repo_root)
        failed_files, crashed = _failed_files_from_checks(check_names, repo_root, changed_files)
        mode = "issue"
        pr_num = None

    return _emit_and_classify(
        failed_files,
        changed_files,
        mode=mode,
        pr_num=pr_num,
        check_names=check_names,
        repo_root=repo_root,
        crashed=crashed,
    )


def compute_condition1(failed_files: list[str], changed_files: list[str]) -> bool:
    """条件①: 失敗ファイル ∩ 変更ファイル = 空集合（純関数・単体テスト対象）."""
    return not (set(failed_files) & set(changed_files))


# ── 判定・出力 ──────────────────────────────────────────────────────────────


def _emit_and_classify(
    failed_files: list[str],
    changed_files: list[str],
    *,
    mode: str,
    pr_num: int | None,
    check_names: list[str],
    repo_root: Path,
    crashed: bool = False,
) -> int:
    """失敗ファイル・変更ファイルから判定結果を算出し、exit code と JSON 出力を返す."""
    if not failed_files:
        if crashed:
            _emit_json("crash_unrecoverable", failed_files, changed_files, None, None)
            sys.stderr.write(
                "判定不能: pytest が INTERNALERROR でクラッシュ終了し、"
                "FAILED サマリ行を一切出力しませんでした"
                "（ファイルパスの特定失敗ではなくクラッシュが原因です）\n"
            )
        else:
            _emit_json("unknown", failed_files, changed_files, None, None)
            sys.stderr.write("判定不能: 失敗テストのファイルパスを特定できませんでした\n")
        return EXIT_UNKNOWN

    condition1 = compute_condition1(failed_files, changed_files)
    if not condition1:
        _emit_json("pr_issue", failed_files, changed_files, False, None)
        return EXIT_PR_ISSUE

    condition2 = _reproduce_on_main(
        mode=mode,
        pr_num=pr_num,
        check_names=check_names,
        repo_root=repo_root,
        changed_files=changed_files,
    )
    if condition2:
        _emit_json("existing", failed_files, changed_files, True, True)
        return EXIT_EXISTING
    _emit_json("pr_issue", failed_files, changed_files, True, False)
    return EXIT_PR_ISSUE


def _emit_json(
    verdict: str,
    failed_files: list[str],
    changed_files: list[str],
    condition1: bool | None,
    condition2: bool | None,
) -> None:
    payload: dict[str, Any] = {
        "verdict": verdict,
        "failed_files": failed_files,
        "changed_files": changed_files,
        "condition1": condition1,
        "condition2": condition2,
    }
    print(json.dumps(payload, ensure_ascii=False))


# ── 起点 A（--pr）: commit status + CI ログ ───────────────────────────────


def _changed_files_from_pr(pr_num: int, repo: str | None) -> list[str]:
    """PR のファイル一覧 API から変更ファイル一覧を取得する."""
    return gh_client.pr_diff_files(pr_num, repo=repo)


def _failed_files_from_pr(pr_num: int, repo: str | None) -> tuple[list[str], bool]:
    """PR head SHA の commit status（FAILURE/ERROR）と CI ログから失敗ファイルを抽出する.

    テスト系（pytest/*, jest/*）の FAILURE/ERROR commit status が存在しない場合は
    テスト失敗の突合判定対象外として空リストを返す（判定不能 → exit 2 につながる）。

    戻り値の 2 要素目は、いずれかの CI ログが pytest の INTERNALERROR クラッシュで
    サマリ行を出力せず終了したか（#3908）。
    """
    if not _failed_test_contexts(pr_num, repo):
        return [], False
    failed_files: list[str] = []
    crashed = False
    for run_id in _failed_ci_run_ids(pr_num, repo):
        log = gh_client.gh_raw(["run", "view", str(run_id), "--log-failed"])
        extracted = _extract_failed_files_from_log(log)
        failed_files.extend(extracted)
        if not extracted and _is_crash_without_summary(log):
            crashed = True
    return _dedupe(failed_files), crashed


def _failed_test_contexts(pr_num: int, repo: str | None) -> list[str]:
    """PR head SHA の FAILURE/ERROR な commit status context のうちテスト系を返す."""
    sha = gh_client.pr_head_sha(pr_num, repo)
    if not sha:
        return []
    raw = gh_client.gh_raw(["api", f"repos/{repo}/commits/{sha}/status", "--jq", _FAILED_CONTEXT_JQ])
    contexts = [line.strip() for line in raw.splitlines() if line.strip()]
    return [c for c in contexts if _TEST_CONTEXT_RE.match(c)]


def _failed_ci_run_ids(pr_num: int, repo: str | None) -> list[str]:
    """PR ブランチの failed な CI workflow run ID 一覧を返す."""
    branch = _pr_branch_name(pr_num, repo)
    if not branch:
        return []
    raw = gh_client.gh_raw(["run", "list", "--branch", branch, "--json", "databaseId,conclusion", "-q", _FAILED_RUN_JQ])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _pr_branch_name(pr_num: int, repo: str | None) -> str:
    data = gh_client.pr_view(pr_num, repo=repo, fields=("headRefName",))
    name = data.get("headRefName", "")
    return name if isinstance(name, str) else ""


def _extract_failed_files_from_log(log: str) -> list[str]:
    """CI ログの FAILED / FAIL 行からテストファイルパスを抽出する."""
    return _regex_extract(log, _FAILED_LOG_FILE_RE)


# ── 起点 B（--issue）: pre-flight の失敗チェック再実行 ─────────────────────


def _changed_files_from_diff(repo_root: Path) -> list[str]:
    """`git diff --name-only origin/main` から変更ファイル一覧を取得する."""
    result = run_subprocess(["git", "diff", "--name-only", "origin/main"], cwd=repo_root)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _preflight_failed_check_names(issue_num: int, repo_root: Path) -> list[str]:
    """直前の pre-flight で失敗したチェック名一覧を返す（timing_log の記録から）.

    `step3-preflight-end` レコードの `meta.failures` に pre-flight が失敗した
    チェック名（`pytest (project)` / `Jest (project)` / `lint (ruff/mypy/gherkin-lint)`
    等）が記録されている。レコードが存在しない場合は空リストを返す（判定不能へ）。
    """
    from tidd_tools import timing_log

    try:
        events = timing_log.read_events(f"issue-{issue_num}")
    except (OSError, ValueError):
        events = []
    for event in reversed(events):
        if event.get("step") == "step3-preflight-end" and event.get("source") == "pre-flight":
            failures = (event.get("meta") or {}).get("failures")
            if isinstance(failures, list):
                return [str(f) for f in failures]
            return []
    return []


def _failed_files_from_checks(
    check_names: list[str], repo_root: Path, changed_files: list[str]
) -> tuple[list[str], bool]:
    """pre-flight の失敗チェックを再実行して失敗ファイルパスを抽出する.

    戻り値の 2 要素目は、pytest チェックが INTERNALERROR クラッシュでサマリ行を
    出力せず終了したか（#3908）。
    """
    _, failed_files, crashed = _run_preflight_checks(check_names, repo_root, changed_files)
    return failed_files, crashed


def _run_preflight_checks(
    check_names: list[str], repo_root: Path, changed_files: list[str]
) -> tuple[bool, list[str], bool]:
    """失敗チェックを再実行し（いずれかが失敗したか, 抽出した失敗ファイル一覧, クラッシュしたか）を返す.

    条件②（origin/main 上の再現確認）と失敗ファイル抽出の両方から使う。
    3 要素目（クラッシュ検出・#3908）は pytest チェックが INTERNALERROR で
    サマリ行（FAILED 行）を一切出力せず終了した場合に True になる。
    """
    any_failed = False
    failed_files: list[str] = []
    crashed = False
    for check_name in check_names:
        for cmd, cwd in _check_commands(check_name, repo_root, changed_files):
            result = run_subprocess(cmd, cwd=cwd)
            if result.returncode != 0:
                any_failed = True
            extracted = _extract_failed_files_from_check(check_name, result.stdout)
            failed_files.extend(extracted)
            if check_name.startswith("pytest (") and not extracted and _is_crash_without_summary(result.stdout):
                crashed = True
    return any_failed, _dedupe(failed_files), crashed


def _check_commands(check_name: str, repo_root: Path, changed_files: list[str]) -> list[tuple[list[str], Path]]:
    """失敗チェック名を再実行コマンド（コマンド・cwd）のリストに変換する.

    テスト系以外のチェック（context-budget / docs-sync 等）はファイル抽出対象外として
    空リストを返す。
    """
    m = re.fullmatch(r"pytest \((.+)\)", check_name)
    if m:
        out: list[tuple[list[str], Path]] = []
        for project in _split_projects(m.group(1)):
            proj_dir = repo_root / project
            out.append(
                (
                    ["uv", "run", "--project", str(proj_dir), "pytest", str(proj_dir / "tests")],
                    repo_root,
                )
            )
        return out

    m = re.fullmatch(r"Jest \((.+)\)", check_name)
    if m:
        out = []
        for project in _split_projects(m.group(1)):
            proj_dir = repo_root / project
            out.append((["node", str(proj_dir / "node_modules/.bin/jest")], proj_dir))
        return out

    if check_name == "lint (ruff/mypy/gherkin-lint)":
        out = []
        for project in test_plan._detect_projects(changed_files, "projects/py/"):
            proj_dir = repo_root / project
            py_files = [f for f in changed_files if f.startswith(project + "/") and f.endswith(".py")]
            if py_files:
                out.append((["uv", "run", "--project", str(proj_dir), "ruff", "check", *py_files], repo_root))
            out.append((["uv", "run", "--project", str(proj_dir), "mypy", "src", "tests"], proj_dir))
        return out

    return []


def _extract_failed_files_from_check(check_name: str, stdout: str) -> list[str]:
    """チェック種別に応じて再実行出力から失敗ファイルパスを抽出する."""
    if check_name.startswith("pytest ("):
        return _regex_extract(stdout, re.compile(r"\bFAILED\s+([^\s:]+)"))
    if check_name.startswith("Jest ("):
        return _regex_extract(stdout, re.compile(r"\bFAIL\s+([^\s:]+)"))
    if check_name == "lint (ruff/mypy/gherkin-lint)":
        return _regex_extract(stdout, _LINT_FILE_RE)
    return []


def _split_projects(s: str) -> list[str]:
    return [p.strip() for p in s.split(",") if p.strip()]


# ── 条件②（origin/main 単体チェックアウトでの再現確認） ───────────────────


def _reproduce_on_main(
    *,
    mode: str,
    pr_num: int | None,
    check_names: list[str],
    repo_root: Path,
    changed_files: list[str],
) -> bool:
    """条件②: origin/main 単体チェックアウトで同じ失敗が再現するか.

    再現すれば True（既存問題）、再現しなければ False（PR / 対象 Issue 起因）。
    実行後は必ず元のブランチ・worktree の状態へ戻す（例外発生時も戻す）。
    再現確認自体の失敗（git エラー等）は fail-safe で False として扱う。
    """
    original_branch = _current_branch_name(repo_root)
    original_head = _git_head_sha(repo_root)
    try:
        _git_fetch(repo_root)
        _git_checkout_detach_main(repo_root)
        if mode == "pr":
            result = run_subprocess(
                [
                    "uv",
                    "run",
                    "--project",
                    str(_tidd_tools_project(repo_root)),
                    "tidd",
                    "run-project-tests",
                    str(pr_num),
                ],
                cwd=repo_root,
            )
            return result.returncode != 0
        any_failed, _, _ = _run_preflight_checks(check_names, repo_root, changed_files)
        return any_failed
    except (GitCommandError, SubprocessTimeoutError, OSError):
        logger.warning("条件②の再現確認に失敗しました（PR / 対象 Issue 起因として扱います）", exc_info=True)
        return False
    finally:
        _restore_git_state(original_branch, original_head, repo_root)


def _current_branch_name(repo_root: Path) -> str:
    """現在のブランチ名を返す（detached HEAD のときは空文字）."""
    result = run_subprocess(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    name = result.stdout.strip()
    return "" if name == "HEAD" else name


def _git_head_sha(repo_root: Path) -> str:
    result = run_subprocess(["git", "rev-parse", "HEAD"], cwd=repo_root)
    return result.stdout.strip()


def _git_fetch(repo_root: Path) -> None:
    result = run_subprocess(["git", "fetch", "origin"], cwd=repo_root)
    if result.returncode != 0:
        raise GitCommandError(["git", "fetch", "origin"], result.returncode, result.stderr or "")


def _git_checkout_detach_main(repo_root: Path) -> None:
    result = run_subprocess(["git", "checkout", "--detach", "origin/main"], cwd=repo_root)
    if result.returncode != 0:
        raise GitCommandError(["git", "checkout", "--detach", "origin/main"], result.returncode, result.stderr or "")


def _restore_git_state(branch: str, head: str, repo_root: Path) -> None:
    """条件②実行前のブランチ・HEAD へ戻す（失敗時は無視して続行）."""
    target = branch if branch else head
    if not target:
        return
    run_subprocess(["git", "checkout", target], cwd=repo_root, check=False)


# ── 共通 ──────────────────────────────────────────────────────────────────


def _tidd_tools_project(repo_root: Path) -> Path:
    return repo_root / "projects/py/tidd_tools"


def _resolve_repo_root() -> Path:
    return test_plan._resolve_repo_root()


def _regex_extract(text: str, pattern: re.Pattern[str]) -> list[str]:
    """パターン（先頭キャプチャグループ）に一致する文字列を重複なし順序維持で返す."""
    return list(dict.fromkeys(pattern.findall(text)))


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _is_crash_without_summary(text: str) -> bool:
    """pytest が INTERNALERROR でクラッシュしサマリ行を出力しなかったか判定する（#3908）.

    pytest-xdist worker crash（`INTERNALERROR> ...`）等、collection-phase でクラッシュし
    通常の `FAILED <path>::<test>` サマリ行が一切出力されないケースを検出する。
    サマリ行が存在する場合（クラッシュしつつも一部テスト結果が報告された場合を含む）は
    通常の失敗として扱い False を返す。
    """
    return bool(_INTERNALERROR_RE.search(text)) and not _FAILED_SUMMARY_TOKEN_RE.search(text)
