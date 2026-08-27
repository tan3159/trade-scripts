#!/usr/bin/env python3
"""PreToolUse hook: subagent による `allow-large-pr` / `allow-xxl` マーカーの自己付与をブロックする（Issue #3992・#3994）.

`tidd pre-flight` の diff サイズゲート（#3081）と ai-review の size/XXL gate（#1296）は
どちらも `<!-- allow-xxl: <理由> -->`（旧名 `<!-- allow-large-pr: <理由> -->` も後方互換）を
PR ボディの escape hatch として受理する（Issue #3994 でマーカーの置き場所を PR ボディへ
統一した。旧仕様ではコミットメッセージも受理していた）。issue-implementer subagent がこの
escape hatch を **自分の判断だけ**で埋め込み、1000 行ゲートを無検証のまま通過させた実例
（2026-08-17・当時はコミットメッセージ経由）が発生した。分割可能性の検証を誰も行わないまま
自己申告の理由だけでゲートが無効化されるのは `.claude/rules/workflow.md`「1000 行超: 必ず
分割」の意図と乖離しているため、subagent 文脈からの `git commit` / `gh pr create` に
これらのマーカーが含まれる場合を機械的にブロックする。

判定シグナル（`block-subagent-review-merge.py` と同一の設計）:
- PreToolUse payload の `agent_type` フィールドは subagent（Agent tool）実行時のみ付与される
  （main session からの実行には存在しない）
- `agent_type` が `issue-next` / `issue-next-all`（Codex の snake_case を含む）の場合は
  オーケストレータ自身であり、分割可能性を独立検証した上でマーカーを付与する権限を持つため
  許可リストとしてスキップする（Issue #3992 の設計: マーカー付与は issue-implementer から
  剥奪しオーケストレータ・人間の判断に委ねる）

マーカー検出は `git commit` 呼び出しの heredoc 本文（`-m "$(cat <<'EOF' ... EOF)"` 形式の
複数行コミットメッセージ）も対象に含める必要があるため、heredoc 本文を除去しない生の
command 文字列に対して正規表現検索する（構造判定〔git commit 呼び出しかどうか〕のみ
heredoc 本文除去済みの文字列を使う）。`gh pr create` は `--body`/`--body-file`（heredoc 含む）
から抽出した PR body に対してマーカーを検索する（`_lib.gh_command.extract_pr_body` を再利用）。

Issue #4032: PR #4023 のレビュー往復中、issue-fixer subagent が diff-size gate に抵触した
際、`gh pr create` ではなく事後の `gh pr edit --body` でマーカーを自己付与し、hook の検査
対象外だったため通過した（`pre_flight._check_diff_size` / `ai_review.size_gate.check_xxl_gate`
はどちらも `gh pr view --json body` で取得する現在の PR ボディを参照するため、`gh pr create`
時点だけでなく事後の `gh pr edit` も検知対象に含める必要がある）。`gh pr edit` も
`gh pr create` と同じ `extract_pr_body` で `--body`/`--body-file` を抽出しマーカーを検索する。

stdlib のみ使用。
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.gh_command import extract_pr_body as _extract_pr_body
from _lib.gh_command import is_gh_pr_create as _is_gh_pr_create
from _lib.gh_command import is_gh_pr_edit as _is_gh_pr_edit
from _lib.hook_io import get_command, is_hook_enabled, read_hook_input
from _lib.shell_parse import split_shell_fragments, strip_heredoc_bodies

DETAIL = "詳細: 上流リポジトリ本体の docs/reference/ 配下・`hooks.md#block-subagent-size-markerpy`（consumer 未配布）\n"

# issue-next（-all）自身は分割可能性を独立検証した上でマーカーを付与する権限を持つ（#3992）。
# block-subagent-review-merge.py と同一の許可リスト（Codex の snake_case 対応込み）。
_ALLOWED_AGENT_TYPES = {"issue-next", "issue-next-all"}

# pre_flight.py._ALLOW_LARGE_PR_RE / ai_review/size_gate.py._ALLOW_XXL_RE と同一の
# マーカー検出パターン（HTML コメント構文のみを対象とし、地の文の「allow-large-pr」への
# 言及だけでは誤検知しない）。
_MARKER_RE_STRS = (
    r"<!--\s*allow-large-pr\s*:",
    r"<!--\s*allow-xxl\s*:",
)


def _normalize_agent_type(agent_type: str) -> str:
    return agent_type.replace("_", "-")


def _contains_marker(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in _MARKER_RE_STRS)


def _is_commit_invocation(command: str) -> bool:
    """command 内に `git commit` 起動が含まれるか判定する（heredoc 本文除去済みで判定）.

    heredoc 本文を除去した文字列を使うことで、コミットメッセージ本文中の文言
    （`git` や `commit` を含む説明テキスト）を誤って構造判定に使わない。
    heredoc 開始行の残骸（閉じクォート欠如）で `shlex.split` が失敗する場合は
    空白区切りにフォールバックする（`block-dangerous-git.py._is_amend_invocation`
    と同じ方針・fail-closed）。
    """
    stripped = strip_heredoc_bodies(command)
    for frag in split_shell_fragments(stripped):
        try:
            tokens = shlex.split(frag)
        except ValueError:
            tokens = frag.split()
        if len(tokens) < 2 or tokens[0] != "git":
            continue
        if "commit" in tokens:
            return True
    return False


def _write_block_message(agent_type: str, *, location: str) -> None:
    sys.stderr.write(
        f"BLOCK: 契約違反: subagent（{agent_type}）が allow-large-pr / allow-xxl マーカーを\n"
        f"自己判断で{location}に付与することはできません（Issue #3992・#3994）。\n"
        "サイズゲート（1000 行超）に抵触した場合は park してオーケストレータ・人間の判断に\n"
        "委ねてください。分割可能性の独立検証手順は\n"
        ".claude/skills/issue-next/subagent-delegation.md「diff-size gate park の追加検証」\n"
        "を参照してください。\n"
    )
    sys.stderr.write(DETAIL)


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    agent_type = payload.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type:
        return 0  # main session（オーケストレータ・人間）からの実行は対象外

    if _normalize_agent_type(agent_type) in _ALLOWED_AGENT_TYPES:
        return 0  # 許可リスト一致（#3992）

    if payload.get("tool_name") != "Bash":
        return 0

    command = get_command(payload)
    if not command:
        return 0

    if _is_commit_invocation(command):
        # マーカー検出は heredoc 本文を含む生の command 文字列に対して行う
        # （多くのコミットメッセージは `-m "$(cat <<'EOF' ... EOF)"` 形式のため）。
        if not _contains_marker(command):
            return 0
        _write_block_message(agent_type, location="コミットメッセージ")
        return 2

    # Issue #3994: マーカーの置き場所が PR ボディへ統一されたため、
    # `gh pr create --body`/`--body-file`（heredoc 含む）への自己付与も検知する。
    # Issue #4032: `gh pr create` 時点だけでなく、事後の `gh pr edit --body` による
    # マーカー自己付与も同じロジックで検知する（PR #4023 で実際にすり抜けた実例）。
    if _is_gh_pr_create(command) or _is_gh_pr_edit(command):
        pr_body = _extract_pr_body(command)
        if not pr_body or not _contains_marker(pr_body):
            return 0
        _write_block_message(agent_type, location="PR ボディ")
        return 2

    return 0


def main() -> int:
    if not is_hook_enabled("block-subagent-size-marker"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
