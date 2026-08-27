"""LLM を使った Issue 本文生成ヘルパー（Issue #1247・#1302 で API 全廃）.

**Issue #1302 で Anthropic API 直接呼び出しを全廃した。** Issue 本文の LLM 強化は
Claude Code の `/create-issue` skill 経由で subagent に外出しした。本モジュールは
互換性のため残っているが、常に template_body をそのまま返す no-op である。

**フォールバック方針（従来通り）:** 元の template body をそのまま返す（品質担保 + 誤生成防止）。
呼び出し元は `enhance_issue_body()` の戻り値 `enhanced=False` を見て template body に
fallback すればよい設計は維持したまま、内部的には LLM 呼び出しをしない。

意味的な強化が必要な場合: Claude Code セッション内で `/create-issue` skill を実行する。
"""

from __future__ import annotations

import dataclasses
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 読み取り系ツール定義は subagent 側（`.claude/agents/issue-writer.md`）に相当するものが
# 移設された。互換性のため定数は残す（呼び出し側テストで参照されている場合の後方互換）。
LLM_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "リポジトリ内のファイル内容を読む。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_bytes": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "check_file_exists",
        "description": "リポジトリ内のファイル/ディレクトリの存在を返す。",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]

LLM_ALLOWED_TOOL_NAMES: set[str] = {t["name"] for t in LLM_TOOL_DEFINITIONS}

SUBMIT_ISSUE_BODY_TOOL: dict[str, Any] = {
    "name": "submit_issue_body",
    "description": "生成した Issue 本文を返す。'## 背景' と '## やること' セクションを含めること。",
    "input_schema": {
        "type": "object",
        "properties": {
            "body": {
                "type": "string",
                "description": "Markdown 本文全体",
            },
        },
        "required": ["body"],
    },
}

CHECKBOX_RE = re.compile(r"^[ \t]*- \[[ x]\]", re.MULTILINE)


@dataclasses.dataclass
class LLMBodyResult:
    """Issue 本文生成結果."""

    body: str
    enhanced: bool = False
    reason: str = ""


def _fallback(template_body: str, reason: str) -> LLMBodyResult:
    return LLMBodyResult(body=template_body, enhanced=False, reason=reason)


def _validate_body(body: str) -> str:
    """生成 body の品質チェック. NG なら理由を返す（OK なら空文字）."""
    if not body or not body.strip():
        return "empty-body"
    if "## 背景" not in body:
        return "missing-haikei"
    if "## やること" not in body:
        return "missing-yaru-koto"
    if not CHECKBOX_RE.search(body):
        return "missing-checkbox"
    return ""


# ── ツール実装（互換性のため残す・呼び出し元テストで参照される場合あり） ──


def _resolve_safe_path(repo_root: Path, path_str: str) -> Path | None:
    try:
        candidate = (repo_root / path_str).resolve()
        root_resolved = repo_root.resolve()
    except (OSError, RuntimeError):
        return None
    if not (candidate == root_resolved or root_resolved in candidate.parents):
        return None
    return candidate


def _tool_read_file(repo_root: Path, params: dict[str, Any]) -> str:
    path_str = str(params.get("path", ""))
    if not path_str:
        return "ERROR: path が指定されていません"
    try:
        max_bytes = int(params.get("max_bytes", 32768))
    except (TypeError, ValueError):
        max_bytes = 32768
    max_bytes = max(1, min(max_bytes, 131072))
    safe = _resolve_safe_path(repo_root, path_str)
    if safe is None:
        return f"ERROR: パスが repo_root の外部です: {path_str}"
    if not safe.is_file():
        return f"ERROR: ファイルが存在しません: {path_str}"
    try:
        raw = safe.read_bytes()[:max_bytes]
        return raw.decode("utf-8", errors="replace")
    except OSError as exc:
        return f"ERROR: 読み込み失敗: {exc}"


def _tool_check_file_exists(repo_root: Path, params: dict[str, Any]) -> str:
    path_str = str(params.get("path", ""))
    if not path_str:
        return "ERROR: path が指定されていません"
    safe = _resolve_safe_path(repo_root, path_str)
    if safe is None:
        return f"ERROR: パスが repo_root の外部です: {path_str}"
    if safe.is_file():
        return "EXISTS: file"
    if safe.is_dir():
        return "EXISTS: directory"
    return "NOT_FOUND"


def _run_tool(name: str, params: dict[str, Any], repo_root: Path) -> str:
    if name not in LLM_ALLOWED_TOOL_NAMES:
        return f"ERROR: 許可されていないツール: {name}"
    if name == "read_file":
        return _tool_read_file(repo_root, params)
    if name == "check_file_exists":
        return _tool_check_file_exists(repo_root, params)
    return f"ERROR: 未実装のツール: {name}"


# ── メイン ──────────────────────────────────────────────────────────────────


def enhance_issue_body(
    *,
    context: str,
    template_body: str,
    repo_root: Path,
    additional_prompt: str = "",
) -> LLMBodyResult:
    """[互換] LLM 強化は `/create-issue` skill に外出しされたため常に template_body を返す.

    Issue #1302 で Anthropic API 直接呼び出しを廃止した。呼び出し元（`analyze-loop-errors`・
    `watch-circleci-failures` 等）は enhanced=False の場合に template body を使う設計を
    維持したまま、本関数は常に fallback を返すことで自動的に template body 経路に流れる。

    意味的な強化を含めた Issue 起票が必要な場合、Claude Code セッション内で
    `/create-issue` skill を実行する（`.claude/skills/create-issue/SKILL.md`）。

    Args:
        context: エラーログ等（互換性のため受け取るが未使用）
        template_body: フォールバック時に返す既存テンプレート本文
        repo_root: 互換性のため受け取るが未使用
        additional_prompt: 互換性のため受け取るが未使用

    Returns:
        ``LLMBodyResult(body=template_body, enhanced=False, reason="deferred-to-skill")``
    """
    del context, repo_root, additional_prompt
    return _fallback(template_body, "deferred-to-skill")
