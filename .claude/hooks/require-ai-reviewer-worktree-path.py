#!/usr/bin/env python3
"""PreToolUse hook: ai-reviewer subagent 起動 prompt に worktree 絶対パスを機械強制する (#4218).

`.claude/skills/issue-next/parser-critical-pr.md` STEP 5.5 は ai-reviewer subagent
（secondary consensus）について「変更ファイルは現在の作業ディレクトリ（PR ブランチの
worktree）から Read / Grep / Glob で直接読む」と記載しており、`.claude/agents/ai-reviewer.md`
の constraints にも同様の前提（cwd = PR worktree）が明記されていた。

しかし copier 配布先のセッションでは Bash ツールの cwd が各コマンド実行後に主リポジトリ
ルートへ都度リセットされることが確認された（`tidd ai-review --stop-before-merge` 実行直後の
単純な `cd <worktree> && pwd` のみのコマンドでも観測）。Agent tool で起動する subagent は
このリセット後の cwd を引き継ぐため、間を置かず ai-reviewer subagent を起動すると、
worktree ではなく主リポジトリ（main ブランチ）の内容を読んでしまい、誤った
REQUEST_CHANGES 判定を返すことがある（実例: consumer 側 PR #1591・約 85k トークン浪費）。

cwd リセットの発生条件は環境依存の可能性があるため、起動手順を worktree 絶対パス明示に
変更すれば cwd の実際の挙動によらず頑健になる。本 hook は Agent tool（Claude Code）/
spawn_agent（Codex）呼び出しの PreToolUse 時に、`subagent_type`（Codex: `task_name`）が
`ai-reviewer` の場合、prompt（Codex: `message`）に worktree 絶対パスパターンが含まれることを
機械検証し、違反の場合は exit 2 でブロックする
（`require-subagent-prompt-contract.py` と同様の PreToolUse payload 判定パターンを踏襲）。

worktree 絶対パスパターンの判定: `tidd worktree-add` の命名規則（`../<repo>-issue-<N>-<slug>`・
`.claude/rules/workflow.md`「着手前」参照）に従い、絶対パス（`/` 始まり）内に
`issue-<数字>` を含むトークンをパスとみなす。

stdlib のみ使用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import is_hook_enabled, read_hook_input

DETAIL = "詳細: 上流リポジトリ本体の docs/reference/ 配下・`hooks.md#require-ai-reviewer-worktree-pathpy`（consumer 未配布）\n"

# worktree 絶対パス: Unix 絶対パス（`/` から始まる）または Windows native 絶対パス
# （`C:\`・`C:/` 等ドライブレターから始まる）で、`issue-<数字>` を含む空白なしトークン
# （`tidd worktree-add` の命名規則 `<repo>-issue-<N>-<slug>` に対応・#4219 レビュー指摘で
# Windows native パスにも対応）。
_WORKTREE_PATH_RE = re.compile(r"(?<!\S)(?:/|[A-Za-z]:[\\/])\S*issue-\d+\S*")


def _normalize_subagent_name(name: str) -> str:
    """Codex の task_name（snake_case）と Claude Code の agent 名（kebab-case）を同一視する."""
    return name.replace("_", "-")


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")

    tool_name = str(payload.get("tool_name", ""))
    if tool_name not in {"Agent", "spawn_agent"}:
        return 0  # Agent tool 以外は対象外

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    subagent_type_raw = str(
        tool_input.get("subagent_type") or tool_input.get("task_name") or ""
    )
    if _normalize_subagent_name(subagent_type_raw) != "ai-reviewer":
        return 0  # ai-reviewer 以外の subagent_type は対象外

    prompt = str(tool_input.get("prompt") or tool_input.get("message") or "")

    if _WORKTREE_PATH_RE.search(prompt):
        return 0  # worktree 絶対パスが明示されている

    sys.stderr.write(
        "BLOCK: 契約違反: ai-reviewer subagent への prompt に worktree 絶対パスが"
        "含まれていません。\n"
        "cwd 依存（Bash ツールの cwd が main リポジトリへリセットされる環境がある・#4218）で"
        "起動すると PR worktree ではなく main ブランチの内容を読み、誤った"
        "REQUEST_CHANGES 判定を返す可能性があります。\n"
        "PR worktree の絶対パスと `gh pr view --json files` で取得した変更ファイル一覧を"
        "prompt に明示してから再起動してください"
        "（`.claude/skills/issue-next/parser-critical-pr.md` STEP 5.5 参照）。\n"
        f"受け取った prompt: {prompt!r}\n"
    )
    sys.stderr.write(DETAIL)
    return 2


def main() -> int:
    if not is_hook_enabled("require-ai-reviewer-worktree-path"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
