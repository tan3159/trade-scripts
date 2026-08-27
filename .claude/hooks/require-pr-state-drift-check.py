#!/usr/bin/env python3
"""PreToolUse hook: `gh pr edit`/`gh pr merge`/`gh pr close` 実行前に action log との drift を検証する（Issue #4038）.

**背景:** `/issue-next-all` → `/issue-next` の実処理中（2026-08-18）、issue-implementer
subagent が STEP 2 の契約（`gh pr create` で終端するはずの契約）を超えて、orchestrator の
関知しないところで追加コミット・PR本文の全面書き換え・park-and-continue 手順までを完了させて
しまった疑いが濃厚な事象が発生した（詳細: 親 Issue #4029）。既存の「契約違反判定（#2542）」は
subagent からの報告受領時点で 1 回だけ PR 状態を確認しており、その後の処理中の逸脱は検知できない。

本 hook は `cache/issue-next-state/issue-<N>.json` の `actions` ログ（Issue #4037）に
`tidd issue-next-state observe-pr <N> <PR>` で記録された「直近 observed PR 状態」
（`state`・`headRefOid`・`updatedAt`）と、`gh pr view` で取得した現在の PR 状態を
compare-before-mutate 方式で照合する。不一致（drift）があれば `gh pr edit`/`merge`/`close`
を exit 2 でブロックする。

**hook 自身は GitHub API へのラベル付与・コメント投稿を行わない**（drift 検知後の
`🙋 needs-human-input` ラベル付与・差分コメント投稿はオーケストレータ側の既存動作）。

**soft-fail（skip）条件（exit 0 + stderr WARN）:**
- 対象コマンドから PR 識別子（番号/URL/ブランチ）を特定できない
  （`gh pr <edit|merge|close>` に明示的な識別子がない場合。hook プロセスの CWD は
  `$CLAUDE_PROJECT_DIR` に固定されるため、カレントブランチからの PR 解決は行わない）
- `gh pr view` の呼び出し失敗・出力の解析失敗・必須フィールド欠落
- PR 本文から `closes #<N>` 等の Issue 番号を抽出できない
- 抽出した Issue 番号に対応する state ファイルが存在しない、または
  `observe-pr` による観測記録（`actions` 内の `action == "observe-pr"` エントリ）が
  1 件も見つからない（baseline 未記録＝段階的導入時の既定挙動）

stdlib のみ使用。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)
from _lib.shell_parse import split_shell_fragments

DETAIL = "詳細: 上流リポジトリ本体の docs/reference/ 配下・`hooks.md#require-pr-state-drift-checkpy`（consumer 未配布）\n"

_TARGET_SEGMENT_RE = re.compile(r"^\s*gh\s+pr\s+(edit|merge|close)(\s|$)")
_CLOSES_RE = re.compile(r"\b(?:closes?|fixes|fix|resolves)[:\s]+#(\d+)", re.IGNORECASE)
_COMPARE_FIELDS = ("state", "headRefOid", "updatedAt")

# gh pr edit/merge/close が受け付ける値付きオプション（PR 識別子抽出でスキップする対象）
_GH_OPTS_WITH_VALUE = frozenset(
    {
        "--repo",
        "-R",
        "--title",
        "-t",
        "--body",
        "-b",
        "--body-file",
        "--add-assignee",
        "--remove-assignee",
        "--add-label",
        "--remove-label",
        "--add-project",
        "--remove-project",
        "--add-reviewer",
        "--remove-reviewer",
        "--milestone",
        "-m",
        "--base",
        "-B",
        "--comment",
        "--reason",
        "--match-head-commit",
        "--author-email",
        "--subject",
    }
)


def _find_target_segment(command: str) -> tuple[str, str] | None:
    """コマンド文字列から `gh pr edit|merge|close` を含むチェーン片とサブコマンド名を返す."""
    for segment in split_shell_fragments(command):
        m = _TARGET_SEGMENT_RE.search(segment)
        if m:
            return segment, m.group(1)
    return None


def _extract_pr_identifier(segment: str, subcmd: str) -> str:
    """セグメントから PR 識別子（番号/URL/ブランチ）を抽出する（省略時は空文字）."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return ""
    try:
        idx = tokens.index(subcmd)
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


def _extract_closes_issues(body: str) -> list[int]:
    """PR 本文から `closes|fixes|resolves #N` を抽出する（`shared.issue_body` と同型・stdlib のみ複製）."""
    if not body:
        return []
    return [int(m.group(1)) for m in _CLOSES_RE.finditer(body)]


def _gh(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _state_root() -> Path:
    """state ファイルのベースディレクトリ（`issue_next_state._state_dir()` と同型）.

    `ISSUE_NEXT_STATE_ROOT` 環境変数（テスト用）があれば優先する。未設定時は hook プロセスの
    CWD（settings.json のラッパーにより `$CLAUDE_PROJECT_DIR` 固定・worktree に依存しない）を使う。
    """
    override = os.environ.get("ISSUE_NEXT_STATE_ROOT")
    if override:
        return Path(override) / "cache"
    return Path.cwd() / "cache"


def _issue_state_file(issue: int) -> Path:
    return _state_root() / "issue-next-state" / f"issue-{issue}.json"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _find_latest_observation(payload: dict[str, Any]) -> dict[str, Any] | None:
    """actions ログから最新の `observe-pr` 観測（details）を返す（見つからなければ None）."""
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return None
    for entry in reversed(actions):
        if not isinstance(entry, dict) or entry.get("action") != "observe-pr":
            continue
        details = entry.get("details")
        if isinstance(details, dict) and set(_COMPARE_FIELDS) <= details.keys():
            return details
    return None


def _compute_drift(
    baseline: dict[str, Any], current: dict[str, Any]
) -> list[tuple[str, Any, Any]]:
    return [
        (field, baseline.get(field), current.get(field))
        for field in _COMPARE_FIELDS
        if baseline.get(field) != current.get(field)
    ]


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    if get_tool_name(payload) != "Bash":
        return 0
    command = get_command(payload)
    found = _find_target_segment(command)
    if found is None:
        return 0
    segment, subcmd = found

    identifier = _extract_pr_identifier(segment, subcmd)
    if not identifier:
        sys.stderr.write(
            "WARN: require-pr-state-drift-check: gh pr "
            f"{subcmd} に PR 識別子（番号/URL/ブランチ）が明示されていないため drift チェックを skip します\n"
        )
        return 0

    view_proc = _gh(
        ["pr", "view", identifier, "--json", "number,state,headRefOid,updatedAt,body"]
    )
    if view_proc is None or view_proc.returncode != 0 or not view_proc.stdout.strip():
        sys.stderr.write(
            f"WARN: require-pr-state-drift-check: PR {identifier} の情報取得に失敗したため skip します\n"
        )
        return 0
    try:
        meta = json.loads(view_proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(
            "WARN: require-pr-state-drift-check: PR情報の解析に失敗したため skip します\n"
        )
        return 0
    if not isinstance(meta, dict):
        sys.stderr.write(
            "WARN: require-pr-state-drift-check: PR情報の形式が不正なため skip します\n"
        )
        return 0

    current = {field: meta.get(field) for field in _COMPARE_FIELDS}
    if any(current[field] is None for field in _COMPARE_FIELDS):
        sys.stderr.write(
            "WARN: require-pr-state-drift-check: PR情報が不完全なため skip します\n"
        )
        return 0

    body = meta.get("body")
    issue_numbers = _extract_closes_issues(body if isinstance(body, str) else "")
    if not issue_numbers:
        sys.stderr.write(
            "WARN: require-pr-state-drift-check: PR本文から closes #<N> を特定できないため skip します\n"
        )
        return 0

    checked_any_baseline = False
    for issue_num in issue_numbers:
        state_payload = _load_json(_issue_state_file(issue_num))
        if state_payload is None:
            continue
        baseline = _find_latest_observation(state_payload)
        if baseline is None:
            continue
        checked_any_baseline = True
        diffs = _compute_drift(baseline, current)
        if diffs:
            pr_number = meta.get("number", identifier)
            sys.stderr.write(
                f"BLOCK: require-pr-state-drift-check: PR #{pr_number}（Issue #{issue_num}）の"
                " 状態が直近の観測時点から変化しています（drift検知）。\n"
            )
            for field, before, after in diffs:
                sys.stderr.write(
                    f"  {field}: 最後に観測した値={before!r} / 現在の値={after!r}\n"
                )
            sys.stderr.write(
                "  意図した変更であれば `tidd issue-next-state observe-pr "
                f"{issue_num} {pr_number}` で再観測してから再実行してください。\n"
            )
            sys.stderr.write(DETAIL)
            return 2

    if not checked_any_baseline:
        sys.stderr.write(
            "WARN: require-pr-state-drift-check: observe-pr の観測記録が見つからないため drift チェックを skip します\n"
        )
    return 0


def main() -> int:
    if not is_hook_enabled("require-pr-state-drift-check"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
