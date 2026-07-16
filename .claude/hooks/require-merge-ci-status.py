#!/usr/bin/env python3
"""PreToolUse hook: `gh pr merge` 実行前に PR head SHA の CI commit status を検証する（Issue #2086）.

**背景:** `tidd_tools ai-review` の auto-merge 経路（exit 0 判定直前）には CI status
未送信検出 gate が既にあるが、exit 4（needs-human-merge）を受けて人間が
`gh pr merge` を直接叩く経路にはこの検証が一切介在しない。承認後に新しいコミットを
push しても、それを検知して再検証を強制する仕組みが存在しなかった（PR #2084 で実例）。

**ブロック条件:**
- `gh pr merge` の対象 PR の head SHA に、変更ファイルから判定される必須 CI コンテキスト
  （`pytest/<project>` / `jest/<project>`。`projects/py/tidd_tools/*.py` 変更時は
  `ruff-format/tidd_tools` / `ruff-lint/tidd_tools` / `mypy/tidd_tools` も追加）のうち、
  commit status が未送信、または `success` 以外（`pending` 等含む）のものが 1 件以上ある場合
- 必須コンテキスト以外も含め、head SHA に送信済みの commit status のいずれかが
  `failure` / `error` の場合

**soft-fail（skip）条件（exit 0 + stderr WARN）:**
- `gh` コマンドが見つからない・タイムアウト
- PR 情報（番号・headRefOid）が取得できない
- commit status API 呼び出しが失敗する

stdlib のみ使用。
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import get_command, get_tool_name, is_hook_enabled, read_hook_input  # noqa: E402

DETAIL = "詳細: docs/reference/hooks.md#require-merge-ci-statuspy\n"

_CHAIN_SPLIT_RE = re.compile(r"&&|\|\||\||;")
_MERGE_SEGMENT_RE = re.compile(r"^\s*gh\s+pr\s+merge(\s|$)")
_GAS_PROJECT_RE = re.compile(r"^projects/gas/([^/\n]+)", re.MULTILINE)
_PY_PROJECT_RE = re.compile(r"^projects/py/([^/\n]+)", re.MULTILINE)
# ai-review が投稿する lint 系 status（core.py::_post_lint_statuses）は
# tidd_tools プロジェクトの .py 変更時のみ固定名で投稿される（他 py プロジェクトには存在しない）。
_TIDD_TOOLS_PY_RE = re.compile(r"^projects/py/tidd_tools/.*\.py$", re.MULTILINE)
_TIDD_TOOLS_LINT_CONTEXTS = (
    "ruff-format/tidd_tools",
    "ruff-lint/tidd_tools",
    "mypy/tidd_tools",
)
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
    """コマンド文字列から `gh pr merge` を含むチェーン片を返す（なければ None）."""
    for segment in _CHAIN_SPLIT_RE.split(command):
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


def _detect_required_contexts(
    all_changed_files: str, lint_eligible_files: str
) -> list[str]:
    """変更ファイル一覧から必須 CI コンテキスト名を返す（test_status_gate.py と同一ロジック）.

    `all_changed_files`（削除を含む全変更ファイル）で pytest/jest の project 判定を行う。
    削除のみの PR でも当該 project の pytest/jest は必須のままとする（#2086 codex 指摘）。
    `lint_eligible_files`（削除を除く）で tidd_tools lint 系（ruff-format/ruff-lint/mypy）を
    判定する。ai-review の `_post_lint_statuses` は削除済みファイルに対して status を投稿しないため。
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
    if _TIDD_TOOLS_PY_RE.search(lint_eligible_files):
        for ctx in _TIDD_TOOLS_LINT_CONTEXTS:
            if ctx not in contexts:
                contexts.append(ctx)
    return contexts


def _gh(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(  # noqa: S603
            ["gh", *args], capture_output=True, text=True, check=False, timeout=timeout
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _get_changed_all_files(pr_number: int) -> str:
    """PR の変更ファイル（削除を含む全件）を改行区切りで返す（取得失敗時は空文字）."""
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
        return ""
    return proc.stdout


def _get_changed_non_deleted_files(pr_number: int) -> str:
    """PR の変更ファイル（削除を除く）を改行区切りで返す（取得失敗時は空文字）."""
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
        return ""
    return proc.stdout


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
        sys.stderr.write(
            "WARN: require-merge-ci-status: PR 情報の取得に失敗したため CI status チェックを skip します\n"
        )
        return 0

    try:
        meta = json.loads(view_proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(
            "WARN: require-merge-ci-status: PR 情報の解析に失敗したため skip します\n"
        )
        return 0
    if not isinstance(meta, dict):
        sys.stderr.write(
            "WARN: require-merge-ci-status: PR 情報の形式が不正なため skip します\n"
        )
        return 0

    pr_number = meta.get("number")
    head_sha = meta.get("headRefOid")
    if not pr_number or not head_sha:
        sys.stderr.write(
            "WARN: require-merge-ci-status: PR番号 / headRefOid が取得できないため skip します\n"
        )
        return 0

    all_changed_files = _get_changed_all_files(pr_number)
    lint_eligible_files = _get_changed_non_deleted_files(pr_number)
    required_contexts = _detect_required_contexts(
        all_changed_files, lint_eligible_files
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
        sys.stderr.write(
            "WARN: require-merge-ci-status: commit status API 呼び出しに失敗したため skip します\n"
        )
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

    if missing or pending or other_failed:
        sys.stderr.write(
            f"BLOCK: PR #{pr_number} の head SHA（{head_sha}）に必要な CI status が揃っていません。\n"
        )
        if missing:
            sys.stderr.write(f"  未送信: {', '.join(missing)}\n")
        if pending:
            sys.stderr.write(f"  success 以外（pending 等）: {', '.join(pending)}\n")
        if other_failed:
            sys.stderr.write(f"  failure/error: {', '.join(other_failed)}\n")
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
