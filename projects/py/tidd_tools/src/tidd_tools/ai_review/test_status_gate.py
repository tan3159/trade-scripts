"""ai-review テスト status gate（Issue #1982）.

backend review 呼び出し前に GitHub commit status を確認し、
pytest/* または jest/* が FAILURE / ERROR のときは
レビューを中断する。

また、merge gate で CI status 未送信を検出する共有関数
``detect_required_ci_contexts()``（Issue #2028）と
``check_missing_ci_statuses()``（Issue #2036）を提供する。

commit status API（``GET /commits/{sha}/status``）とは別の GitHub API オブジェクトである
native GitHub Actions CheckRun（``GET /commits/{sha}/check-runs``。``ci.yml`` の ``python``
job 等が該当）の failure を検出する ``check_runs_failed()``（Issue #3725）も提供する。

**使い方:**

ai-review の main フローで、backend review 実行前に
``check_test_statuses()`` を呼ぶ。

merge gate では ``detect_required_ci_contexts(changed_files)`` を呼び、
必要な CI コンテキスト名のリストを取得する。

Returns:
    (passed, failed_contexts):
      - passed=True: 全テスト系 status が成功（または存在しない）→ 続行してよい
      - passed=False: 1 件以上が FAILURE / ERROR → レビューを中断すべき
      - failed_contexts: 失敗したコンテキスト名のリスト（passed=True のとき空リスト）

stdlib のみ使用。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_TEST_CONTEXT_RE = re.compile(r"^(pytest|jest)/")
_GAS_PROJECT_RE = re.compile(r"^projects/gas/([^/\n]+)", re.MULTILINE)
_PY_PROJECT_RE = re.compile(r"^projects/py/([^/\n]+)", re.MULTILINE)
_FAILED_STATES = frozenset({"failure", "error"})
_CHECK_RUN_FAILED_CONCLUSIONS = frozenset({"failure"})
# mermaid-lint/docs 判定（Issue #2794 で hook 側に追加・Issue #4138 でこちらへも反映）。
# 述語は `.claude/hooks/require-merge-ci-status.py::_file_has_mermaid_fence` と同一。
# hook は stdlib のみ使用のため複製する（同ファイルの既存コメント方針を踏襲）。
_MERMAID_FENCE_RE = re.compile(r"^```mermaid\s*$", re.MULTILINE)
_MERMAID_EXT_RE = re.compile(r"\.(md|markdown|mmd|mermaid)$", re.IGNORECASE)


def _file_has_mermaid_fence(repo_root: Path, rel_path: str) -> bool:
    """ファイルを読んで ```mermaid フェンスが含まれるか判定する（Issue #2794・#4138）.

    .mmd / .mermaid 拡張子の場合は中身によらず True を返す。
    .md / .markdown の場合はファイルを開いて ```mermaid フェンスを探す。
    """
    path = repo_root / rel_path
    ext = Path(rel_path).suffix.lower()
    if ext in (".mmd", ".mermaid"):
        return True
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return bool(_MERMAID_FENCE_RE.search(text))
    except OSError:
        return False


def detect_required_ci_contexts(
    changed_files: str,
    repo_root: Path | None = None,
    lint_eligible_files: str | None = None,
) -> list[str]:
    """変更ファイル一覧から必要な CI コンテキスト名を返す（Issue #2028）.

    ``_post_test_statuses()`` と merge gate の両方から呼ぶ共有関数。

    Args:
        changed_files: 改行区切りの変更ファイルパス文字列（``gh pr diff --name-only`` 相当・
            削除を含む全件）。pytest/jest の project 判定に使う（削除のみの PR でも
            当該 project のテストは必須のままとする）。
        repo_root: mermaid フェンス判定用のリポジトリルート（Issue #4138）。
            None の場合（既存呼び出し互換）は mermaid-lint/docs の判定を行わない
            （ファイル内容を読めないため fail-safe に「必須としない」側へ倒す。
            呼び出し元はファイルシステムへアクセス可能なら必ず渡すこと）。
        lint_eligible_files: mermaid-lint/docs 判定用の削除除外済みファイル一覧
            （``.claude/hooks/require-merge-ci-status.py::_detect_required_contexts`` の
            ``lint_eligible_files`` と同一・Issue #4140）。None の場合は ``changed_files``
            をそのまま使う（既存呼び出し互換）。

    Returns:
        必要な CI コンテキスト名のリスト（例: ["jest/foo", "pytest/tidd_tools"]）。
        docs のみ変更のような場合は空リストを返す。

    Note（Issue #2858）: ruff-format/ruff-lint/mypy は意図的に対象外。
    lint 失敗は ``_post_lint_statuses`` の戻り値が exit code を格上げすることで
    pre-flight/ai-review 自体を失敗させる経路で既にブロックされており、
    このゲート（テスト系 status の事前チェック）に含める必要がない。
    project 別汎用化（`ruff-format/<project>` 等）は
    ``.claude/hooks/require-merge-ci-status.py::_detect_required_contexts`` 側で行う。

    Note（Issue #4138）: mermaid-lint/docs は
    ``.claude/hooks/require-merge-ci-status.py::_detect_required_contexts`` と同一の
    判定条件（mermaid フェンスを含む .md/.markdown、または .mmd/.mermaid 拡張子）で
    検知する。以前はこちらにのみ判定漏れがあり、内部 merge gate と PreToolUse hook の
    判定が経路により非対称になっていた（consumer 側 PR #1220 で実測）。

    Note（Issue #4140）: mermaid 判定は ``lint_eligible_files``（指定時）を使う。
    ``.mmd``/``.mermaid`` 拡張子は内容によらず対象になる述語のため、削除済みファイルが
    ``changed_files`` に残ったままだと hook 側（削除済みを除外した ``lint_eligible_files``
    を使う）と判定が非対称になる（削除専用 PR で内部 merge gate だけが
    mermaid-lint/docs を要求し続ける）。
    """
    contexts: list[str] = []
    for m in _GAS_PROJECT_RE.finditer(changed_files):
        ctx = f"jest/{m.group(1)}"
        if ctx not in contexts:
            contexts.append(ctx)
    for m in _PY_PROJECT_RE.finditer(changed_files):
        ctx = f"pytest/{m.group(1)}"
        if ctx not in contexts:
            contexts.append(ctx)
    if repo_root is not None:
        mermaid_source = changed_files if lint_eligible_files is None else lint_eligible_files
        for rel_path in mermaid_source.splitlines():
            f = rel_path.strip()
            if not f or not _MERMAID_EXT_RE.search(f):
                continue
            if _file_has_mermaid_fence(repo_root, f):
                if "mermaid-lint/docs" not in contexts:
                    contexts.append("mermaid-lint/docs")
                break
    return contexts


def _resolve_lint_eligible_files(changed_files: str, pr_num: str, repo: str, env: dict[str, str]) -> str:
    """``changed_files`` から削除済みファイルを除外した一覧を返す（Issue #4140）.

    ``.claude/hooks/require-merge-ci-status.py::_get_changed_non_deleted_files`` と
    同一の判定（``status != "removed"``）を REST API で取得する。取得失敗時は
    soft-fail として ``changed_files`` をそのまま返す（従来動作を維持し、
    mermaid-lint/docs の要否判定はフィルタなしの保守的側に倒す）。
    """
    proc = subprocess.run(  # noqa: S603
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{pr_num}/files",
            "--jq",
            '.[] | select(.status == "removed") | .filename',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        return changed_files
    removed_files = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    if not removed_files:
        return changed_files
    return "\n".join(f for f in (line.strip() for line in changed_files.splitlines()) if f and f not in removed_files)


def check_missing_ci_statuses(pr_num: str, repo: str, token: str = "", repo_root: Path | None = None) -> list[str]:
    """変更ファイルに必要な CI コンテキストのうち commit status 未送信のものを返す（Issue #2036）.

    変更ファイル取得・必要コンテキスト判定・未送信検出を内包する共有関数。
    ``core.py::_handle_approve()``（通常経路）と ``subcommands.py::_continue_approve()``
    （fallback / ``--continue-with-verdict`` 経路）の両方の merge gate から呼ぶ。

    Args:
        pr_num: PR 番号
        repo: リポジトリ (owner/name)
        token: GitHub トークン（省略可）
        repo_root: mermaid フェンス判定用のリポジトリルート（Issue #4138・省略可）。
            未指定時は ``detect_required_ci_contexts()`` と同様に mermaid-lint/docs の
            判定を行わない。呼び出し元はファイルシステムへアクセス可能なら必ず渡すこと。

    Returns:
        未送信の CI コンテキスト名のリスト。未送信があれば各コンテキストについて
        ``ERROR: <ctx> の commit status が未送信です`` を stderr に出力する。
        docs のみ変更などで必要コンテキストがない場合は空リストを返す。
    """
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    try:
        diff_proc = subprocess.run(  # noqa: S603
            ["gh", "pr", "diff", str(pr_num), "--repo", repo, "--name-only"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
            timeout=60,
        )
        changed_files = diff_proc.stdout if diff_proc.returncode == 0 else ""
        lint_eligible_files = _resolve_lint_eligible_files(changed_files, pr_num, repo, env)
        required_contexts = detect_required_ci_contexts(
            changed_files, repo_root=repo_root, lint_eligible_files=lint_eligible_files
        )
        if not required_contexts:
            return []

        statuses = _fetch_commit_statuses(pr_num, repo, token)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        # check_test_statuses() と対称の soft-fail（gh 不在・タイムアウト時は gate を skip）
        sys.stderr.write(f"WARN: test_status_gate: CI status 未送信チェック失敗のため skip: {exc}\n")
        return []
    if statuses is None:
        # Issue #2039: statuses API 呼び出し失敗（App token の statuses 権限不足等）。
        # 「status 空」と区別して soft-fail（WARN + skip）にする。
        sys.stderr.write(
            "WARN: test_status_gate: commit status 取得に失敗したため CI status 未送信チェックを skip します\n"
        )
        return []
    existing_contexts = {s["context"] for s in statuses}
    missing = [ctx for ctx in required_contexts if ctx not in existing_contexts]
    for ctx in missing:
        sys.stderr.write(f"ERROR: {ctx} の commit status が未送信です\n")
    return missing


def _fetch_commit_statuses(pr_num: str, repo: str, token: str) -> list[dict[str, str]] | None:
    """PR の headRefOid の commit statuses を取得して context/state の dict リストを返す.

    Returns:
        - list: 取得成功。statuses が存在しない場合は空リスト
        - None: API 呼び出し失敗（SHA 取得失敗・statuses API が非ゼロ exit。Issue #2039）
    """
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token

    # 1. PR の HEAD SHA を取得する
    sha_proc = subprocess.run(  # noqa: S603
        [
            "gh",
            "pr",
            "view",
            str(pr_num),
            "--repo",
            repo,
            "--json",
            "headRefOid",
            "-q",
            ".headRefOid",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=60,
    )
    if sha_proc.returncode != 0 or not sha_proc.stdout.strip():
        # Issue #2074: 「statuses が 0 件」（gh api 成功・空リスト）と区別できるよう、
        # SHA 取得自体が失敗した旨を明示する。
        sys.stderr.write(
            f"WARN: test_status_gate: PR #{pr_num} の SHA 取得に失敗しました"
            f"（exit={sha_proc.returncode}）: {sha_proc.stderr.strip()}\n"
        )
        return None
    sha = sha_proc.stdout.strip()

    # 2. combined status API から context/state を取得する
    proc = subprocess.run(  # noqa: S603
        [
            "gh",
            "api",
            f"repos/{repo}/commits/{sha}/status",
            "--jq",
            ".statuses[] | {context: .context, state: .state}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        # Issue #2039: App token の statuses read 権限不足等で API が失敗するケース。
        # 「statuses が空」（returncode 0・空 stdout）とは区別して None を返す。
        # Issue #2074: fail-open の発生を検知しやすくするため理由をログに残す。
        sys.stderr.write(
            f"WARN: test_status_gate: commit status API 呼び出しに失敗しました"
            f"（exit={proc.returncode}）: {proc.stderr.strip()}\n"
        )
        return None
    if not proc.stdout.strip():
        return []

    statuses: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "context" in obj and "state" in obj:
            statuses.append({"context": str(obj["context"]), "state": str(obj["state"]).lower()})
    return statuses


def check_test_statuses(
    pr_num: str,
    repo: str,
    token: str = "",
) -> tuple[bool, list[str]]:
    """pytest/* / jest/* context の commit status を検査する.

    Args:
        pr_num: PR 番号
        repo: リポジトリ (owner/name)
        token: GitHub トークン（省略可）

    Returns:
        (passed, failed_contexts):
          - passed=True: 全テスト系 status が成功（または存在しない）
          - passed=False: 1 件以上が FAILURE / ERROR
          - failed_contexts: 失敗したコンテキスト名のリスト
    """
    try:
        statuses = _fetch_commit_statuses(pr_num, repo, token)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        sys.stderr.write(f"WARN: test_status_gate: commit status 取得失敗のため skip: {exc}\n")
        return True, []
    if statuses is None:
        # Issue #2039 以前は API 失敗も空リスト扱い（= pass）だった。同じ挙動を維持する。
        statuses = []

    failed_contexts: list[str] = []
    failed_context_states: dict[str, str] = {}
    for status in statuses:
        context = status["context"]
        state = status["state"]
        if _TEST_CONTEXT_RE.match(context) and state in _FAILED_STATES:
            failed_contexts.append(context)
            failed_context_states[context] = state.upper()

    if failed_contexts:
        for ctx in failed_contexts:
            sys.stderr.write(f"ERROR: {ctx} が {failed_context_states[ctx]} のためレビューを中断しました\n")
        return False, failed_contexts

    return True, []


def _fetch_check_runs(sha: str, repo: str, token: str) -> list[dict[str, str]] | None:
    """指定 SHA の native GitHub Actions CheckRun 一覧（name/conclusion）を取得する（Issue #3725）.

    commit status API とは別の GitHub API オブジェクトである CheckRun
    （``ci.yml`` の ``python`` job が報告する ``ci / python`` 等）を取得する。

    Returns:
        - list: 取得成功。CheckRun が 1 件も存在しない場合は空リスト
        - None: API 呼び出し失敗（非ゼロ exit）
    """
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token

    proc = subprocess.run(  # noqa: S603
        [
            "gh",
            "api",
            f"repos/{repo}/commits/{sha}/check-runs",
            "--jq",
            '.check_runs[] | {name: .name, conclusion: (.conclusion // "")}',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"WARN: test_status_gate: check-runs API 呼び出しに失敗しました"
            f"（exit={proc.returncode}）: {proc.stderr.strip()}\n"
        )
        return None
    if not proc.stdout.strip():
        return []

    check_runs: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "name" in obj:
            check_runs.append({"name": str(obj["name"]), "conclusion": str(obj.get("conclusion") or "").lower()})
    return check_runs


def check_runs_failed(sha: str, repo: str, token: str = "") -> list[str]:
    """指定 SHA の native GitHub Actions CheckRun に failure conclusion があるか判定する（Issue #3725）.

    ``ci.yml`` 等が報告する CheckRun（例: ``ci / python``）は commit status API では
    検知できないため、merge gate（``core.py::_check_commit_status_gate`` /
    ``.claude/hooks/require-merge-ci-status.py``）から呼ぶ共有関数として追加した。

    Args:
        sha: 検査対象の commit SHA
        repo: リポジトリ (owner/name)
        token: GitHub トークン（省略可）

    Returns:
        conclusion が failure な CheckRun 名のリスト。API 呼び出し失敗・タイムアウト・
        gh 不在時は soft-fail として空リストを返す（他の gate 関数と対称の挙動）。
    """
    try:
        check_runs = _fetch_check_runs(sha, repo, token)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        sys.stderr.write(f"WARN: test_status_gate: check-runs 取得失敗のため skip: {exc}\n")
        return []
    if check_runs is None:
        return []

    failed = [cr["name"] for cr in check_runs if cr["conclusion"] in _CHECK_RUN_FAILED_CONCLUSIONS]
    for name in failed:
        sys.stderr.write(f"ERROR: check-run '{name}' が failure のためマージを中断しました\n")
    return failed
