#!/usr/bin/env python3
"""PreToolUse hook: SKILL / agent 定義への gh 逆流を防ぐ（Issue #2537）.

.claude/skills/ / .claude/agents/ 配下への Write / Edit で、
MCP 代替可能な `gh` サブコマンド記述を exit 2 でブロックする。

MCP 代替不能な操作（許可リスト）:
  - gh pr merge    : PR マージ + branch 削除の複合操作（MCP に相当ツールなし）
  - gh auth token  : App JWT 生成の前段トークン取得（MCP 非対応）
  - gh api         : 任意 REST API 呼び出し（MCP tool は主要リソースのみ）

CI 待機は `gh pr checks --watch --fail-fast` を直接書かず `tidd wait-ci` を使う
（Issue #3645・ポーリング出力がチェック数 × 更新回数ぶん LLM の文脈を汚染するため）。

詳細: docs/reference/mcp-tool-migration.md「MCP で代替不能で shell 継続の操作」

stdlib のみ使用。
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (
    get_file_path,
    get_new_content,
    get_tool_name,
    is_file_edit_tool,
    is_hook_enabled,
    read_hook_input,
)
from _lib.tidd_uvx import build_uvx_tidd_cmd

# ── 検査対象パスパターン ──────────────────────────────────────────────
_TARGET_PATH_RE = re.compile(r"(^|/)\.claude/(skills|agents)/")

# ── MCP 代替不能な gh サブコマンド（許可リスト）───────────────────────
# docs/reference/mcp-tool-migration.md の「MCP で代替不能で shell 継続の操作」に準拠
_ALLOWED_GH_RE = re.compile(
    r"""
    \bgh\s+
    (?:
        pr\s+merge\b        # PR マージ + branch 削除の複合操作
      | auth\b              # App JWT 生成の前段トークン取得
      | api\b               # 任意 REST API 呼び出し（gh api repos/...）
    )
    """,
    re.VERBOSE,
)

# ── MCP 代替可能な gh サブコマンド → 推奨代替のマッピング ─────────────
# 値は MCP tool 名に加え、`tidd wait-ci`（Issue #3645）のような tidd サブコマンド
# の推奨代替文字列も格納できる（表示は「推奨代替」）。
# 詳細な実行コマンド（uvx 経由）は _wait_ci_guidance() が組み立てる（#3405）。
_GH_TO_MCP: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bgh\s+pr\s+checks\b"),
        "tidd wait-ci <PR番号>（Issue #3645）",
    ),
    (
        re.compile(r"\bgh\s+issue\s+view\b"),
        "mcp__github__get_issue",
    ),
    (
        re.compile(r"\bgh\s+issue\s+list\b"),
        "mcp__github__list_issues",
    ),
    (
        re.compile(r"\bgh\s+issue\s+edit\b"),
        "mcp__github__update_issue",
    ),
    (
        re.compile(r"\bgh\s+issue\s+comment\b"),
        "mcp__github__add_issue_comment",
    ),
    (
        re.compile(r"\bgh\s+issue\s+close\b"),
        "mcp__github__update_issue",
    ),
    (
        re.compile(r"\bgh\s+issue\s+create\b"),
        "mcp__github__create_issue",
    ),
    (
        re.compile(r"\bgh\s+pr\s+view\b"),
        "mcp__github__get_pull_request",
    ),
    (
        re.compile(r"\bgh\s+pr\s+list\b"),
        "mcp__github__list_pull_requests",
    ),
    (
        re.compile(r"\bgh\s+pr\s+diff\b"),
        "mcp__github__get_pull_request_diff",
    ),
    (
        re.compile(r"\bgh\s+pr\s+create\b"),
        "mcp__github__create_pull_request",
    ),
    (
        re.compile(r"\bgh\s+pr\s+edit\b"),
        "mcp__github__update_pull_request",
    ),
]


def _get_new_content(payload: dict) -> str | None:
    """Write / Edit / apply_patch の新 content を取り出す（Issue #3221）.

    Claude Code の `content` / `new_string` 等と、Codex の apply_patch
    （`tool_input.command` の patch 文字列から追加行を抽出）を共通ヘルパー
    `hook_io.get_new_content()` に委譲する。
    """
    return get_new_content(payload)


def _wait_ci_guidance() -> str:
    """`tidd wait-ci` の案内コマンド文字列を組み立てる（Issue #3645・#3405）.

    handbook ローカルパス（`projects/py/tidd_tools`）を直書きせず `_lib/tidd_uvx.py`
    の `build_uvx_tidd_cmd()`（uvx ゼロインストール実行方式）経由で組み立てるため、
    copier で配布される consumer 環境でもそのまま実行可能な案内になる。
    表示用に実行バイナリ名は `uvx`（解決済み絶対パスではなく）で統一する。
    """
    uvx_cmd = build_uvx_tidd_cmd("wait-ci", "<PR番号>")
    if uvx_cmd is None:
        # uvx が PATH に無い環境向けフォールバック（PATH 導入済み tidd 前提）
        return "tidd wait-ci <PR番号>"
    return shlex.join(["uvx", *uvx_cmd[1:]])


def _find_blocked_gh_commands(content: str) -> list[tuple[str, str]]:
    """content 内の代替可能な gh コマンドを検出する.

    MCP tool または tidd サブコマンド（`tidd wait-ci`・Issue #3645）で代替できる
    gh コマンドを検出する。

    Returns:
        [(match_text, alternative), ...] のリスト（空なら違反なし）
    """
    violations: list[tuple[str, str]] = []
    for pattern, mcp_tool in _GH_TO_MCP:
        for match in pattern.finditer(content):
            matched_text = match.group(0).strip()
            # マッチしたテキスト自体が許可リストに合致するか確認する。
            # 行全体ではなくマッチ自体を検査することで、gh api と gh pr view が
            # 同じ行にある場合に gh pr view を誤って免除しない。
            # 例: `gh api ...`（... `gh pr view` は使わない）→ gh pr view はブロック対象
            if _ALLOWED_GH_RE.search(matched_text):
                continue
            violations.append((matched_text, mcp_tool))
    return violations


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)

    if not is_file_edit_tool(tool_name):
        return 0

    raw_path = get_file_path(payload)
    if not raw_path:
        return 0

    # .claude/skills/ または .claude/agents/ 配下でなければスキップ
    if not _TARGET_PATH_RE.search(raw_path):
        return 0

    new_content = _get_new_content(payload)
    if not new_content:
        return 0

    violations = _find_blocked_gh_commands(new_content)
    if not violations:
        return 0

    # 代替 MCP tool の一覧（重複除去・順序保持）
    seen: set[str] = set()
    unique_mcp_tools: list[str] = []
    for _, mcp_tool in violations:
        if mcp_tool not in seen:
            seen.add(mcp_tool)
            unique_mcp_tools.append(mcp_tool)

    sys.stderr.write(
        f"Blocked: SKILL / agent 定義内での gh コマンド使用が検出されました: {raw_path}\n"
        "\n"
        "Claude Code セッション内の GitHub 操作は mcp__github__* MCP tool を使ってください（Issue #2537）。\n"
        "\n"
        "検出されたコマンドと推奨代替:\n"
    )
    for matched_text, mcp_tool in violations:
        sys.stderr.write(f"  {matched_text!r} → {mcp_tool}\n")
    sys.stderr.write(
        "\n"
        "MCP 代替不能な操作（gh pr merge / gh auth / gh api）は引き続き使用可能です。\n"
        f"CI 待機は `{_wait_ci_guidance()}`（Issue #3645）を使ってください。\n"
        "\n"
        "詳細: docs/reference/hooks.md#enforce-mcp-in-skillspy\n"
        "参照: docs/reference/mcp-tool-migration.md\n"
    )
    return 2


def main() -> int:
    if not is_hook_enabled("enforce-mcp-in-skills"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
