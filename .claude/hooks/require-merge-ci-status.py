#!/usr/bin/env python3
"""PreToolUse hook: `gh pr merge` 実行前に PR head SHA の CI commit status を検証する（Issue #2086）.

**背景:** `tidd_tools ai-review` の auto-merge 経路（exit 0 判定直前）には CI status
未送信検出 gate が既にあるが、exit 4（needs-human-merge）を受けて人間が
`gh pr merge` を直接叩く経路にはこの検証が一切介在しない。承認後に新しいコミットを
push しても、それを検知して再検証を強制する仕組みが存在しなかった（PR #2084 で実例）。

**ブロック条件:**
- `gh pr merge` の対象 PR の head SHA に、変更ファイルから判定される必須 CI コンテキスト
  （`pytest/<project>` / `jest/<project>`。`projects/py/<project>/*.py` 変更時は
  `ruff-format/<project>` / `ruff-lint/<project>` / `mypy/<project>` も追加。
  ただし `<project>` に `pyproject.toml` が存在しない場合は対象外・Issue #2858）のうち、
  commit status が未送信、または `success` 以外（`pending` 等含む）のものが 1 件以上ある場合
- 必須コンテキスト以外も含め、head SHA に送信済みの commit status のいずれかが
  `failure` / `error` の場合
- 変更ファイルリストの取得に失敗した場合（#2403 修正: 安全側に倒してブロック）
- head SHA の native GitHub Actions CheckRun（`ci.yml` の `python` job 等。commit status
  API とは別の GitHub API オブジェクト）のいずれかが `conclusion: failure` の場合
  （Issue #3725: private repo・Free プラン等 branch protection が使えない consumer では
  `ci / python` が FAILURE でも commit status のみの検査では素通りしていた）

**soft-fail（skip）条件（exit 0 + stderr WARN + 監査ログ）:**
- `gh` コマンドが見つからない・タイムアウト
- PR 情報（番号・headRefOid）が取得できない
- commit status API 呼び出しが失敗する
- check-runs API 呼び出しが失敗する（Issue #3725）

変更ファイルリスト取得失敗は soft-fail ではなくブロック（#2403）。
理由: ファイルリストが不明な PR のマージを許可すると必須 context の検出漏れが生じる。

stdlib のみ使用。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)
from _lib.shell_parse import split_shell_fragments

DETAIL = "詳細: docs/reference/hooks.md#require-merge-ci-statuspy\n"

_MERGE_SEGMENT_RE = re.compile(r"^\s*gh\s+pr\s+merge(\s|$)")
_GAS_PROJECT_RE = re.compile(r"^projects/gas/([^/\n]+)", re.MULTILINE)
_PY_PROJECT_RE = re.compile(r"^projects/py/([^/\n]+)", re.MULTILINE)
# ai-review が投稿する lint 系 status（core.py::_post_lint_statuses）は
# projects/py/<project> ごとに投稿される（Issue #2858・project 名は動的検出）。
_PY_PROJECT_FILE_RE = re.compile(r"^projects/py/([^/\n]+)/.*\.py$", re.MULTILINE)
_MERMAID_FENCE_RE = re.compile(r"^```mermaid\s*$", re.MULTILINE)
_MERMAID_EXT_RE = re.compile(r"\.(md|markdown|mmd|mermaid)$", re.IGNORECASE)
_FAILED_STATES = frozenset({"failure", "error"})
# gh pr merge の値付きオプション（次トークンが値であるフラグ）
_GH_OPTS_WITH_VALUE = frozenset(
    {
        "--repo",
        "-R",
        "--subject",
        "--body",
        "-b",
        "--body-file",
        "--match-head-commit",
        "--author-email",
    }
)


def _find_merge_segment(command: str) -> str | None:
    """コマンド文字列から `gh pr merge` を含むチェーン片を返す（なければ None）.

    分割は quote-aware な `_lib/shell_parse.split_shell_fragments` を使う
    （Issue #2966。naive な `re.split` はクォート内の区切り文字でも誤分割していた）。
    """
    for segment in split_shell_fragments(command):
        if _MERGE_SEGMENT_RE.search(segment):
            return segment
    return None


def _extract_pr_identifier(segment: str) -> str:
    """`gh pr merge` セグメントから PR 識別子（番号/URL/ブランチ）を抽出する。省略時は空文字."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return ""
    try:
        idx = tokens.index("merge")
    except ValueError:
        return ""
    skip_next = False
    for tok in tokens[idx + 1 :]:
        if skip_next:
            skip_next = False
            continue
        if tok in _GH_OPTS_WITH_VALUE:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        return tok
    return ""


def _file_has_mermaid_fence(repo_root: Path, rel_path: str) -> bool:
    """ファイルを読んで ```mermaid フェンスが含まれるか判定する（Issue #2794）.

    .mmd / .mermaid 拡張子の場合は中身によらず True を返す。
    .md / .markdown の場合はファイルを開いて ```mermaid フェンスを探す。
    """
    path = repo_root / rel_path
    ext = Path(rel_path).suffix.lower()
    if ext in (".mmd", ".mermaid"):
        return True
    # .md / .markdown: ファイルを開いてフェンスを確認
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return bool(_MERMAID_FENCE_RE.search(text))
    except OSError:
        return False


def _lint_project_contexts(
    lint_eligible_files: str, repo_root: Path | None
) -> list[str]:
    """変更 .py ファイルから project 別 ruff-format/ruff-lint/mypy context 名を返す（Issue #2858）.

    ai-review の `_post_lint_statuses` は `projects/py/<project>` に `pyproject.toml` が
    存在する project のみ lint を実行し project 別 context を投稿する。hook 側もこれに
    合わせ pyproject.toml の有無で判定する。ただし `repo_root` が None（呼び出し元がリポジトリ
    ルートを特定できない場合）は fail-safe のため pyproject.toml の有無に関わらず必須とする。
    """
    contexts: list[str] = []
    seen: set[str] = set()
    for m in _PY_PROJECT_FILE_RE.finditer(lint_eligible_files):
        project = m.group(1)
        if project in seen:
            continue
        seen.add(project)
        if repo_root is not None:
            pyproject = repo_root / "projects" / "py" / project / "pyproject.toml"
            if not pyproject.is_file():
                continue
        for ctx in (
            f"ruff-format/{project}",
            f"ruff-lint/{project}",
            f"mypy/{project}",
        ):
            if ctx not in contexts:
                contexts.append(ctx)
    return contexts


def _detect_required_contexts(
    all_changed_files: str,
    lint_eligible_files: str,
    repo_root: Path | None = None,
) -> list[str]:
    """変更ファイル一覧から必須 CI コンテキスト名を返す（test_status_gate.py と同一ロジック）.

    `all_changed_files`（削除を含む全変更ファイル）で pytest/jest の project 判定を行う。
    削除のみの PR でも当該 project の pytest/jest は必須のままとする（#2086 codex 指摘）。
    `lint_eligible_files`（削除を除く）で project 別 lint 系（ruff-format/ruff-lint/mypy）を
    判定する（Issue #2858）。ai-review の `_post_lint_statuses` は削除済みファイルに対して
    status を投稿しないため。
    mermaid-lint は `lint_eligible_files` から mermaid 対象ファイルが存在するときのみ必須とする
    （Issue #2794）。
    """
    contexts: list[str] = []
    for m in _GAS_PROJECT_RE.finditer(all_changed_files):
        ctx = f"jest/{m.group(1)}"
        if ctx not in contexts:
            contexts.append(ctx)
    for m in _PY_PROJECT_RE.finditer(all_changed_files):
        ctx = f"pytest/{m.group(1)}"
        if ctx not in contexts:
            contexts.append(ctx)
    for ctx in _lint_project_contexts(lint_eligible_files, repo_root):
        if ctx not in contexts:
            contexts.append(ctx)
    # hooks の ruff/mypy（Issue #3229）: .claude/hooks / templates/workflow/.claude/hooks の
    # .py 変更時は ruff-format/hooks・ruff-lint/hooks・mypy/hooks を必須とする。
    _HOOKS_FILE_RE = re.compile(
        r"^(\.claude/hooks|templates/workflow/\.claude/hooks)/.*\.py$"
    )
    if any(_HOOKS_FILE_RE.search(f) for f in lint_eligible_files.splitlines()):
        for ctx in ("ruff-format/hooks", "ruff-lint/hooks", "mypy/hooks"):
            if ctx not in contexts:
                contexts.append(ctx)
    # mermaid-lint/docs: .md/.mmd/.mermaid 変更かつ mermaid フェンスあり（Issue #2794）
    if repo_root is not None:
        for rel_path in lint_eligible_files.splitlines():
            f = rel_path.strip()
            if not f:
                continue
            if not _MERMAID_EXT_RE.search(f):
                continue
            if _file_has_mermaid_fence(repo_root, f):
                if "mermaid-lint/docs" not in contexts:
                    contexts.append("mermaid-lint/docs")
                break
    return contexts


def _gh(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=timeout
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _get_changed_all_files(pr_number: int) -> str | None:
    """PR の変更ファイル（削除を含む全件）を改行区切りで返す（取得失敗時は None）.

    戻り値が None の場合は呼び出し元がブロック判定を行う（#2403）。
    """
    proc = _gh(
        [
            "api",
            "--paginate",
            f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/files",
            "--jq",
            ".[] | .filename",
        ]
    )
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout


def _get_changed_non_deleted_files(pr_number: int) -> str | None:
    """PR の変更ファイル（削除を除く）を改行区切りで返す（取得失敗時は None）.

    戻り値が None の場合は呼び出し元がブロック判定を行う（#2403）。
    """
    proc = _gh(
        [
            "api",
            "--paginate",
            f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/files",
            "--jq",
            '.[] | select(.status != "removed") | .filename',
        ]
    )
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout


def _get_check_run_conclusions(head_sha: str) -> dict[str, str] | None:
    """head SHA の native GitHub Actions CheckRun 一覧（name -> conclusion）を取得する（Issue #3725）.

    commit status API とは別の GitHub API オブジェクトである CheckRun（``ci.yml`` の
    ``python`` job が報告する ``ci / python`` 等）を取得する。
    ``tidd_tools.ai_review.test_status_gate._fetch_check_runs`` と同一ロジック
    （hook は stdlib のみ使用のため複製する）。

    Returns:
        - dict: 取得成功。CheckRun が 1 件も存在しない場合は空 dict
        - None: API 呼び出し失敗（gh 不在・タイムアウト・非ゼロ exit）
    """
    proc = _gh(
        [
            "api",
            f"repos/{{owner}}/{{repo}}/commits/{head_sha}/check-runs",
            "--jq",
            '.check_runs[] | {name: .name, conclusion: (.conclusion // "")}',
        ]
    )
    if proc is None or proc.returncode != 0:
        return None

    conclusions: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "name" in obj:
            conclusions[str(obj["name"])] = str(obj.get("conclusion") or "").lower()
    return conclusions


def _write_audit_log(message: str) -> None:
    """soft-fail 発生事実を監査ログに記録する（#2403）.

    ログパスは環境変数 TIDD_MERGE_AUDIT_LOG で指定できる。
    未指定時は XDG_CACHE_HOME（または ~/.cache）/tidd_tools/require-merge-ci-status-softfail.log に書く。
    書き込み失敗時は stderr WARN で観測可能にする（ログ失敗でブロックしない）。
    """
    log_path_env = os.environ.get("TIDD_MERGE_AUDIT_LOG")
    if log_path_env:
        log_path = Path(log_path_env)
    else:
        cache_home = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        log_path = (
            Path(cache_home) / "tidd_tools" / "require-merge-ci-status-softfail.log"
        )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{timestamp} soft-fail: {message}\n")
    except OSError as exc:
        sys.stderr.write(
            f"WARN: require-merge-ci-status: 監査ログの書き込みに失敗しました: {exc}\n"
        )


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    if get_tool_name(payload) != "Bash":
        return 0
    command = get_command(payload)
    segment = _find_merge_segment(command)
    if segment is None:
        return 0

    identifier = _extract_pr_identifier(segment)
    view_args = ["pr", "view"]
    if identifier:
        view_args.append(identifier)
    view_args += ["--json", "number,headRefOid"]
    view_proc = _gh(view_args)
    if view_proc is None or view_proc.returncode != 0 or not view_proc.stdout.strip():
        msg = "PR 情報の取得に失敗したため CI status チェックを skip します"
        sys.stderr.write(f"WARN: require-merge-ci-status: {msg}\n")
        _write_audit_log(msg)
        return 0

    try:
        meta = json.loads(view_proc.stdout)
    except json.JSONDecodeError:
        msg = "PR 情報の解析に失敗したため skip します"
        sys.stderr.write(f"WARN: require-merge-ci-status: {msg}\n")
        _write_audit_log(msg)
        return 0
    if not isinstance(meta, dict):
        msg = "PR 情報の形式が不正なため skip します"
        sys.stderr.write(f"WARN: require-merge-ci-status: {msg}\n")
        _write_audit_log(msg)
        return 0

    pr_number = meta.get("number")
    head_sha = meta.get("headRefOid")
    if not pr_number or not head_sha:
        msg = "PR番号 / headRefOid が取得できないため skip します"
        sys.stderr.write(f"WARN: require-merge-ci-status: {msg}\n")
        _write_audit_log(msg)
        return 0

    # #2403 修正: 変更ファイルリスト取得失敗は soft-fail ではなくブロックする。
    # 取得失敗で required_contexts=[] になると必須 context が判定できず素通りするため。
    all_changed_files = _get_changed_all_files(pr_number)
    if all_changed_files is None:
        sys.stderr.write(
            f"BLOCK: require-merge-ci-status: PR #{pr_number} の変更ファイルリスト取得に失敗しました。\n"
            "  CI status を検証できないためマージをブロックします。\n"
            "  gh api が利用可能な状態で再実行してください。\n"
        )
        sys.stderr.write(DETAIL)
        return 2

    lint_eligible_files = _get_changed_non_deleted_files(pr_number)
    if lint_eligible_files is None:
        sys.stderr.write(
            f"BLOCK: require-merge-ci-status: PR #{pr_number} の変更ファイルリスト取得に失敗しました。\n"
            "  CI status を検証できないためマージをブロックします。\n"
            "  gh api が利用可能な状態で再実行してください。\n"
        )
        sys.stderr.write(DETAIL)
        return 2

    # hook ファイルは .claude/hooks/ に置かれるため、2 階層上がリポジトリルートになる
    hook_repo_root = Path(__file__).resolve().parent.parent.parent
    required_contexts = _detect_required_contexts(
        all_changed_files, lint_eligible_files, hook_repo_root
    )

    status_proc = _gh(
        [
            "api",
            f"repos/{{owner}}/{{repo}}/commits/{head_sha}/status",
            "--jq",
            ".statuses[] | {context: .context, state: .state}",
        ]
    )
    if status_proc is None or status_proc.returncode != 0:
        msg = f"PR #{pr_number} commit status API 呼び出しに失敗したため skip します"
        sys.stderr.write(f"WARN: require-merge-ci-status: {msg}\n")
        _write_audit_log(msg)
        return 0

    statuses: dict[str, str] = {}
    for line in status_proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "context" in obj and "state" in obj:
            statuses[str(obj["context"])] = str(obj["state"]).lower()

    missing = [ctx for ctx in required_contexts if ctx not in statuses]
    # 必須コンテキストに限らず、送信済みの全 status から failure/error を拾う
    # （ruff-format/tidd_tools 等 ai-review が投稿する他の status も含む）
    other_failed = [ctx for ctx, state in statuses.items() if state in _FAILED_STATES]
    pending = [
        ctx
        for ctx in required_contexts
        if ctx in statuses and statuses[ctx] != "success" and ctx not in other_failed
    ]

    # Issue #3725: commit status API では検知できない native GitHub Actions CheckRun
    # （`ci.yml` の `python` job 等）の failure も検査する。API 呼び出し失敗は soft-fail
    # とし（commit status 側の必須 context 検証は継続する）、監査ログに記録する。
    check_run_conclusions = _get_check_run_conclusions(head_sha)
    if check_run_conclusions is None:
        msg = f"PR #{pr_number} check-runs API 呼び出しに失敗したため CheckRun チェックを skip します"
        sys.stderr.write(f"WARN: require-merge-ci-status: {msg}\n")
        _write_audit_log(msg)
        failed_check_runs: list[str] = []
    else:
        failed_check_runs = [
            name
            for name, conclusion in check_run_conclusions.items()
            if conclusion == "failure"
        ]

    if missing or pending or other_failed or failed_check_runs:
        sys.stderr.write(
            f"BLOCK: PR #{pr_number} の head SHA（{head_sha}）に必要な CI status が揃っていません。\n"
        )
        if missing:
            sys.stderr.write(f"  未送信: {', '.join(missing)}\n")
        if pending:
            sys.stderr.write(f"  success 以外（pending 等）: {', '.join(pending)}\n")
        if other_failed:
            sys.stderr.write(f"  failure/error: {', '.join(other_failed)}\n")
        if failed_check_runs:
            sys.stderr.write(f"  failure な CheckRun: {', '.join(failed_check_runs)}\n")
        sys.stderr.write(
            f"  tidd_tools ai-review {pr_number} <試行回数> を再実行してください。\n"
        )
        sys.stderr.write(DETAIL)
        return 2

    return 0


def main() -> int:
    if not is_hook_enabled("require-merge-ci-status"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
