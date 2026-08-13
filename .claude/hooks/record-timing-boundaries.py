#!/usr/bin/env python3
"""Timing 境界の機械記録 hook（Issue #3160・断絶修正 #3385・#3518・#3558）.

merge-summary が読む計測境界のうち、SKILL.md / agent のプロンプト指示のみに依存して
いたマークを、既存の tool 呼び出し発火点から自動記録する:

- PreToolUse[Agent] subagent 起動 → 種別ごとの計測境界（#3557・#3558 で対応拡大）:
  - ``issue-implementer`` → ``step2-implementation``
  - ``issue-reviewer`` → ``step1.5-quality-check``
  - ``duplicate-detector`` → ``step1.5.5-duplicate-triage``
  - ``ai-confirm-verifier`` → ``step5-aiconfirm-start``
  - ``issue-fixer`` → ``step5-fix-start``
- PostToolUse[Agent] subagent 完了 → 終了境界（#3558）:
  - ``ai-confirm-verifier`` → ``step5-aiconfirm-end``
  - ``issue-fixer`` → ``step5-fix-end``
- PostToolUse[Bash] ``git worktree add`` / ``tidd worktree-add`` 成功 → ``step2-branch-created``
- PostToolUse[mcp__github__create_pull_request] または PostToolUse[Bash]
  ``gh pr create`` 成功 → ``step4-pr-created``
- PostToolUse[mcp__github__issue_write] で ``🙋 needs-human-input`` ラベルを含む
  update 成功 → ``step1.5-quality-check``（``meta.verdict="needs-human-input"``・#3635）
- PostToolUse[mcp__github__sub_issue_write] で ``method == "add"``（サブ Issue 追加）
  成功 → ``step1.5-quality-check``（``meta.verdict="epic-split"``・#3635）

**#3635:** STEP 1.5-d の needs-human-input（park）・epic-split（Epic 化）分岐は
issue-implementer を起動せずフローが終わるため、従来は SKILL.md のプロンプト指示で
手動 ``mark-quality-check-done --verdict needs-human-input|epic-split`` を実行していた
（#3159 で機械強制のスコープ外・呼び忘れてもブロックされなかった）。本 hook が
``mcp__github__issue_write``（ラベル付与）・``mcp__github__sub_issue_write``
（サブ Issue 追加）の PostToolUse から自動記録することで、計測境界クローズ漏れを
機械強制する。記録は ``_record()`` 経由のため、既に ``step1.5-quality-check`` が
記録済み（PASS/FAIL の issue-reviewer 経路や手動マークを含む）の Issue には二重記録しない。
成功判定は ``_handle_pr_create()`` と同じ流儀（``interrupted`` / ``isError``）。

**#3558:** ``ai-confirm-verifier`` / ``issue-fixer`` の prompt は ``PR番号: <PR番号>``
（Issue 番号を含まない）のため、``_lib.gh_command.gh_pr_view_json(pr_num, ["body"])``
で PR 本文を取得し ``closes #N`` から ``issue-<N>`` を解決する。``gh`` 実行失敗・
``closes #N`` 未検出の場合は記録せず exit 0 で終了する（tool 実行をブロックしない）。
`hook_event_name`（PreToolUse / PostToolUse）で start / end を区別する。

**#3557:** ``_handle_agent`` を subagent 種別ごとのテーブル駆動（`_SUBAGENT_STEP_TABLE`）
に一般化した。Issue 番号抽出の正規表現は ``_lib/issue_ref.py`` に集約し、hook 側で
再定義しない（``issue-reviewer`` は description の ``Issue #<N>``、
``duplicate-detector`` は description の先頭 ``#<N>``）。

**#3518:** ``step2-implementation`` / ``step2-branch-created`` は ``tidd worktree-add``
自身が ``git worktree add`` の前後で統一日誌へ記録するようになったため、本 hook の
step2 分岐は ``git worktree add`` を直接叩いた経路のフォールバックとして残す
（削除しない・#3518 やること 5）。

冪等性（#3160 やること 6）:
  (a) PostToolUse はコマンド/ツールが成功（exit 0・エラーなし）した場合のみ記録する
  (b) 同一 issue の同一 step が既に記録済み（統一日誌）なら再記録しない
      （#3166 の 4 秒差 2 重記録による「実装」「実装（2回目）」分裂の再発防止）

記録先: 統一イベントログ（`timing-events/timing.db`・`_lib/hook_io` の stdlib アクセス・#3340）。

**#3385 断絶修正:**
  - ``step2-implementation``: prompt が ``Issue番号: <N>`` の 1 行のみでないと一致しなかった
    （複数行 prompt で断絶）ため、``re.MULTILINE`` + ``.search()`` へ変更した
  - ``step2-branch-created``: `#3296` で worktree 作成が ``tidd worktree-add`` ラッパー経由に
    変わったケースでも確実に一致するよう、単純部分文字列判定から正規表現判定へ変更した
  - ``step4-pr-created``: ``mcp__github__create_pull_request`` 未使用（``gh pr create`` 経由）の
    実行形態でも記録できるよう PostToolUse[Bash] からも判定するようにした

**#3516 断絶修正:** ``gh pr create --body-file <path>``（``-F <path>``）経由の PR 作成は
``closes #N`` がコマンド文字列自体に現れないため、旧実装では ``step4-pr-created`` が
一切記録されなかった。``_lib.gh_command.extract_pr_body()``（heredoc・``--body-file``
両対応）で PR body 全体を解決してから ``closes #N`` を探すため、コマンド文字列・
heredoc・``--body-file`` の指すファイルのいずれに書かれていても検知できる。

**#3550 重複解消:** コマンド解析（``gh pr create`` 検出・PR body 抽出）と
``closes``/``fixes``/``resolves #N`` 抽出は独自実装をやめ、``_lib/gh_command.py``
（``is_gh_pr_create``/``extract_pr_body``/``extract_closes_issues``）へ委譲する。
``issue-<N>`` 抽出は ``_lib/issue_ref.py`` へ委譲する。旧実装は ``closes``/``close`` のみ
受理していたが、委譲後は ``closes``/``fixes``/``resolves`` を受理する（本 hook の唯一の
観測可能な挙動差分）。

**#3552 成功判定・時刻ソース（gh pr create 経路）:**
- 成功判定: PostToolUse[Bash] の ``tool_response`` には exit_code キーが存在しないため
  （既知キーは stdout / stderr / interrupted / isImage / noOutputExpected）、
  ``interrupted`` の判定（``_lib.hook_io.is_bash_success`` に集約）に加えて、
  ``gh pr create`` が成功時に stdout へ出力する PR URL
  （``_lib.gh_command.PR_URL_RE``）の有無で失敗を検出する。stdout が空・
  エラーメッセージのみ（PR URL を含まない）の場合は記録しない。
- 時刻ソース: 記録する時刻は hook 発火時刻ではなく、stdout の PR 番号で
  ``gh pr view <番号> --json createdAt`` を実行して取得した PR の実 createdAt を
  ``meta.created_at`` に格納する。``gh`` 実行失敗・JSON 解析失敗時は hook 発火時刻へ
  フォールバックし ``meta.created_at_source`` に ``"fallback"`` を入れる
  （記録漏れより多少ズレた記録の方が実害が小さい・#3552 制約）。
  ``mcp__github__create_pull_request`` 経路（``_handle_pr_create``）は
  ``tool_response.isError`` で失敗判定する（PR URL 判定はしない）。
- 冪等性（``record_event_once_safe()``）は維持し、「誤った値を後から訂正する」機構は
  作らない。正しい値を最初から入れることで解決する。

stdlib のみ使用。
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.gh_command import extract_closes_issues as _extract_closes_issues
from _lib.gh_command import extract_pr_body as _extract_pr_body
from _lib.gh_command import extract_pr_number_from_url as _extract_pr_number_from_url
from _lib.gh_command import gh_pr_view_json as _gh_pr_view_json
from _lib.gh_command import is_gh_pr_create as _is_gh_pr_create
from _lib.hook_io import (
    append_timing_event,
    get_command,
    get_tool_name,
    has_timing_event,
    is_hook_enabled,
    read_hook_input,
)
from _lib.hook_io import (
    get_tool_output as _get_tool_output,
)
from _lib.hook_io import (
    is_bash_success as _is_bash_success,
)
from _lib.issue_ref import (
    extract_first_hash_number as _extract_first_hash_number,
)
from _lib.issue_ref import (
    extract_issue_number_from_branch as _extract_issue_number_from_branch,
)
from _lib.issue_ref import (
    extract_issue_number_from_issue_description as _extract_issue_number_from_description,
)
from _lib.issue_ref import (
    extract_issue_number_from_prompt as _extract_issue_number_from_prompt,
)
from _lib.issue_ref import (
    extract_pr_number_from_prompt as _extract_pr_number_from_prompt,
)

# 捕捉する subagent 種別（`-` を `_` に正規化した値）→ {start, end?, source}（#3557・#3558）。
# - ``start``: PreToolUse[Agent]（subagent 起動）時に記録する step
# - ``end``: PostToolUse[Agent]（subagent 完了）時に記録する step（未定義なら Post では何もしない）
# - ``source``: Issue 番号の抽出元
#   - "prompt" は prompt/message の `Issue番号: <N>`
#   - "description" は description の `Issue #<N>`
#   - "description_first_hash" は description の先頭 `#<N>`
#   - "pr" は prompt の `PR番号: <N>` → `gh pr view` → PR 本文の `closes #<N>`（#3558）
_SUBAGENT_STEP_TABLE: dict[str, dict[str, str]] = {
    "issue_implementer": {"start": "step2-implementation", "source": "prompt"},
    "issue_reviewer": {"start": "step1.5-quality-check", "source": "description"},
    "duplicate_detector": {
        "start": "step1.5.5-duplicate-triage",
        "source": "description_first_hash",
    },
    "ai_confirm_verifier": {
        "start": "step5-aiconfirm-start",
        "end": "step5-aiconfirm-end",
        "source": "pr",
    },
    "issue_fixer": {"start": "step5-fix-start", "end": "step5-fix-end", "source": "pr"},
}
# `git worktree add ...` / `tidd worktree-add ...` の両方に一致する（#3385）。
# 旧実装は "git" / "worktree" / "add" の単純部分文字列判定で、コマンドが
# `git fetch origin && ...` を伴わず単独で `tidd worktree-add ...` のみ実行された
# 場合に "git" が含まれず記録漏れになっていた。
_WORKTREE_ADD_CMD_RE = re.compile(r"git\s+worktree\s+add\b|tidd\s+worktree-add\b")

#: ``gh pr view <N> --json createdAt`` のタイムアウト（#3552）。
_GH_PR_VIEW_TIMEOUT_SECONDS = 10.0

#: ``mcp__github__issue_write`` の ``labels`` に含まれると品質チェック修正不能（park）を
#: 意味するラベル名（#3635）。validate-issue.py の ``NEEDS_HUMAN_INPUT_LABEL`` と同一。
_NEEDS_HUMAN_INPUT_LABEL = "🙋 needs-human-input"


def _has_step(issue_num: int, step: str) -> bool:
    """同一 issue の同一 step が既に統一日誌に記録済みか（冪等性 (b)・#3340）."""
    return has_timing_event(f"issue-{issue_num}", step)


def _append_mark(
    issue_num: int,
    step: str,
    source: str,
    *,
    meta: dict[str, Any] | None = None,
) -> None:
    """統一日誌へ point イベントを追記する（#3340）."""
    append_timing_event(f"issue-{issue_num}", step, source, meta=meta)


def _record(
    issue_num: int,
    step: str,
    source: str,
    *,
    meta: dict[str, Any] | None = None,
) -> int:
    if _has_step(issue_num, step):
        return 0  # 冪等性 (b): 既に記録済み
    _append_mark(issue_num, step, source, meta=meta)
    return 0


def _now_utc_str() -> str:
    """現在時刻（UTC）を ISO 8601 文字列で返す（#3552 フォールバック用）."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_pr_created_at(pr_num: int) -> tuple[str, str]:
    """PR の実 createdAt を ``gh pr view`` で取得する（#3552）.

    成功時は ``(createdAt, "github")``、``gh`` 実行失敗・JSON 解析失敗・
    createdAt 取得不能時は ``(現在時刻, "fallback")`` を返す（hook 発火時刻へ
    フォールバック・#3552。記録漏れより多少ズレた記録の方が実害が小さい）。
    """
    fallback = (_now_utc_str(), "fallback")
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_num), "--json", "createdAt"],
            capture_output=True,
            text=True,
            timeout=_GH_PR_VIEW_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return fallback
    if result.returncode != 0:
        return fallback
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return fallback
    created_at = data.get("createdAt")
    if not isinstance(created_at, str) or not created_at:
        return fallback
    return created_at, "github"


def _extract_pr_issue_number(prompt: str) -> int | None:
    """`PR番号: <N>` → `gh pr view` → PR 本文の `closes #<N>` で Issue 番号を解決する（#3558）.

    ai-confirm-verifier / issue-fixer の prompt は Issue 番号を含まず PR 番号のみのため、
    `gh_pr_view_json(pr_num, ["body"])` で PR 本文を取得し `extract_closes_issues()` で
    `issue-<N>` を解決する。`gh` 実行失敗・`closes #<N>` 未検出の場合は None を返し、
    記録をスキップして hook は exit 0 で終了する（tool 実行をブロックしない・#3558 制約）。
    """
    pr_num = _extract_pr_number_from_prompt(prompt)
    if pr_num is None:
        return None
    data = _gh_pr_view_json(str(pr_num), ["body"])
    if data is None:
        return None
    body = data.get("body")
    if not isinstance(body, str):
        return None
    issues = _extract_closes_issues(body)
    if not issues:
        return None
    return issues[0]


def _extract_subagent_issue_number(
    source: str, tool_input: dict[str, Any]
) -> int | None:
    """subagent 種別ごとの契約に従って tool_input から Issue 番号を抽出する（#3557・#3558）."""
    if source in ("prompt", "pr"):
        prompt = str(tool_input.get("prompt") or tool_input.get("message") or "")
        if source == "prompt":
            return _extract_issue_number_from_prompt(prompt)
        return _extract_pr_issue_number(prompt)
    description = str(tool_input.get("description") or "")
    if source == "description":
        return _extract_issue_number_from_description(description)
    return _extract_first_hash_number(description)


def _handle_agent(payload: dict[str, Any]) -> int:
    """PreToolUse[Agent]: subagent 起動 → 計測境界の自動記録（#3557・#3558）.

    捕捉する subagent 種別と対応 start step は `_SUBAGENT_STEP_TABLE` で定義する:
    - issue-implementer 起動 → step2-implementation
    - issue-reviewer 起動 → step1.5-quality-check
    - duplicate-detector 起動 → step1.5.5-duplicate-triage
    - ai-confirm-verifier 起動 → step5-aiconfirm-start（PR 番号 → gh 解決）
    - issue-fixer 起動 → step5-fix-start（PR 番号 → gh 解決）
    """
    tool_input = payload.get("tool_input") or {}
    subagent_type = str(
        tool_input.get("subagent_type") or tool_input.get("task_name") or ""
    ).replace("-", "_")
    spec = _SUBAGENT_STEP_TABLE.get(subagent_type)
    if spec is None:
        return 0
    issue_num = _extract_subagent_issue_number(spec["source"], tool_input)
    if issue_num is None:
        return 0
    return _record(issue_num, spec["start"], "record-timing-boundaries")


def _handle_agent_post(payload: dict[str, Any]) -> int:
    """PostToolUse[Agent]: subagent 完了 → 終了境界の自動記録（#3558）.

    `_SUBAGENT_STEP_TABLE` に ``end`` が定義された種別（ai-confirm-verifier /
    issue-fixer）のみ記録する。``end`` 未定義の種別（issue-implementer 等）は
    start のみ対応のため何もしない。成功判定 (a) に合わせ、``interrupted`` が真の
    場合は記録しない。
    """
    tool_response = payload.get("tool_response") or {}
    if isinstance(tool_response, dict) and tool_response.get("interrupted") is True:
        return 0
    tool_input = payload.get("tool_input") or {}
    subagent_type = str(
        tool_input.get("subagent_type") or tool_input.get("task_name") or ""
    ).replace("-", "_")
    spec = _SUBAGENT_STEP_TABLE.get(subagent_type)
    if spec is None:
        return 0
    end_step = spec.get("end")
    if not end_step:
        return 0
    issue_num = _extract_subagent_issue_number(spec["source"], tool_input)
    if issue_num is None:
        return 0
    return _record(issue_num, end_step, "record-timing-boundaries")


def _handle_gh_pr_create(command: str, payload: dict[str, Any]) -> int:
    """``gh pr create`` 成功 → step4-pr-created を記録する（#3552）.

    成功判定は stdout の PR URL（``_lib.gh_command.PR_URL_RE``）の有無で行う。
    記録する時刻は hook 発火時刻ではなく PR の実 createdAt（``meta.created_at``・
    ``_fetch_pr_created_at()`` で取得。失敗時は ``meta.created_at_source="fallback"``）。
    """
    # #3516/#3550: PR body の解決自体を `_lib.gh_command.extract_pr_body()` に委譲する
    # （heredoc・`--body`・`--body-file`/`-F` のいずれの形式でも body 全体を取得できる）。
    body = _extract_pr_body(command)
    issues = _extract_closes_issues(body)
    if not issues:
        return 0
    stdout = _get_tool_output(payload)
    pr_num = _extract_pr_number_from_url(stdout)
    if pr_num is None:
        # 成功時に stdout へ PR URL が出力される（#3552）。無い = 失敗した gh pr create
        return 0
    created_at, source = _fetch_pr_created_at(pr_num)
    return _record(
        issues[0],
        "step4-pr-created",
        "record-timing-boundaries",
        meta={"created_at": created_at, "created_at_source": source},
    )


def _handle_bash(payload: dict[str, Any]) -> int:
    """PostToolUse[Bash]: worktree 作成成功 → step2-branch-created / gh pr create 成功 → step4-pr-created."""
    tool_response = payload.get("tool_response") or {}
    if not isinstance(tool_response, dict):
        return 0
    # 成功判定 (a): interrupted が真の場合は失敗扱い（_lib.hook_io へ集約・#3552）
    if not _is_bash_success(payload):
        return 0
    command = get_command(payload)
    if _WORKTREE_ADD_CMD_RE.search(command):
        issue_num = _extract_issue_number_from_branch(command)
        if issue_num is not None:
            return _record(
                issue_num, "step2-branch-created", "record-timing-boundaries"
            )
    if _is_gh_pr_create(command):
        return _handle_gh_pr_create(command, payload)
    return 0


def _handle_pr_create(payload: dict[str, Any]) -> int:
    """PostToolUse[mcp__github__create_pull_request]: PR 作成成功 → step4-pr-created."""
    tool_response = payload.get("tool_response") or {}
    if isinstance(tool_response, dict):
        # #3552: mcp 経路は interrupted に加えて isError で失敗判定できる
        if tool_response.get("interrupted") is True:
            return 0
        if tool_response.get("isError") is True:
            return 0
    tool_input = payload.get("tool_input") or {}
    body = str(tool_input.get("body") or "")
    issues = _extract_closes_issues(body)
    if not issues:
        return 0
    return _record(issues[0], "step4-pr-created", "record-timing-boundaries")


def _tool_input_int(payload: dict[str, Any], key: str) -> int | None:
    """tool_input から整数値（``issue_number`` 等）を取り出す（#3635）."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    value = tool_input.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _handle_issue_write(payload: dict[str, Any]) -> int:
    """PostToolUse[mcp__github__issue_write]: `🙋 needs-human-input` ラベル付与 → step1.5-quality-check（#3635）.

    STEP 1.5-d の「修正不能・判断不能（park）」分岐は issue-implementer を起動せず
    フローが終わるため、従来は SKILL.md のプロンプト指示で手動
    ``mark-quality-check-done --verdict needs-human-input`` を実行していた
    （#3159 で機械強制のスコープ外）。本 hook が ``mcp__github__issue_write`` の
    ``labels`` に ``🙋 needs-human-input`` を含む update を検知して自動記録する。
    成功判定は ``_handle_pr_create()`` と同じ流儀（``interrupted`` / ``isError``）。
    """
    tool_response = payload.get("tool_response") or {}
    if isinstance(tool_response, dict):
        if tool_response.get("interrupted") is True:
            return 0
        if tool_response.get("isError") is True:
            return 0
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0
    if str(tool_input.get("method") or "") != "update":
        return 0
    labels = tool_input.get("labels")
    if not isinstance(labels, list) or _NEEDS_HUMAN_INPUT_LABEL not in labels:
        return 0
    issue_num = _tool_input_int(payload, "issue_number")
    if issue_num is None:
        return 0
    return _record(
        issue_num,
        "step1.5-quality-check",
        "record-timing-boundaries",
        meta={"verdict": "needs-human-input"},
    )


def _handle_sub_issue_write(payload: dict[str, Any]) -> int:
    """PostToolUse[mcp__github__sub_issue_write]: サブ Issue 追加 → step1.5-quality-check（#3635）.

    STEP 1.5-d の「粒度が大きすぎる（Epic 化）」分岐も issue-implementer を起動せず
    フローが終わるため、従来は手動 ``mark-quality-check-done --verdict epic-split`` を
    実行していた。本 hook が ``mcp__github__sub_issue_write`` の ``method == "add"``
    （親 Issue へサブ Issue 追加）を検知して、親 Issue 番号（``issue_number``）へ
    自動記録する。成功判定は ``_handle_pr_create()`` と同じ流儀。
    """
    tool_response = payload.get("tool_response") or {}
    if isinstance(tool_response, dict):
        if tool_response.get("interrupted") is True:
            return 0
        if tool_response.get("isError") is True:
            return 0
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0
    if str(tool_input.get("method") or "") != "add":
        return 0
    issue_num = _tool_input_int(payload, "issue_number")
    if issue_num is None:
        return 0
    return _record(
        issue_num,
        "step1.5-quality-check",
        "record-timing-boundaries",
        meta={"verdict": "epic-split"},
    )


def _main() -> int:
    payload = read_hook_input(hook_name=None)
    tool_name = get_tool_name(payload)
    if tool_name in ("Agent", "spawn_agent"):
        # settings.json は PreToolUse[Agent] と PostToolUse[Agent] の両方から本 hook を
        # 登録するため（#3558）、`hook_event_name` で Pre/Post を区別する。
        # フィールド未指定（旧テスト・後方互換）は PreToolUse 扱いにする。
        if payload.get("hook_event_name") == "PostToolUse":
            return _handle_agent_post(payload)
        return _handle_agent(payload)
    if tool_name == "Bash":
        return _handle_bash(payload)
    if tool_name == "mcp__github__create_pull_request":
        return _handle_pr_create(payload)
    if tool_name == "mcp__github__issue_write":
        return _handle_issue_write(payload)
    if tool_name == "mcp__github__sub_issue_write":
        return _handle_sub_issue_write(payload)
    return 0


def main() -> int:
    if not is_hook_enabled("record-timing-boundaries"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
