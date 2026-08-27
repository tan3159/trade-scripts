"""Issue やること・振る舞い × PR diff の機械突き合わせ（Issue #2597）.

ai-review パイプラインの APPROVE パスから呼ばれ、scope-diff-checker subagent
persona（``.claude/agents/scope-diff-checker.md``）を agy CLI に渡して

- (a) スコープ超過: やることに対応しない変更
- (b) 未消化: やること・振る舞いに対応する実装が無い項目

を検出し、非ブロッキングの PR コメントとして投稿する。

Phase 1 は判定のみ（exit code に影響させない）。ブロッキング化は実績を見て
別 Issue で検討する。すべての失敗（Issue 取得不可・LLM 利用不可・JSON parse
失敗・コメント投稿失敗）は skip 扱いでパイプラインを止めない。
escape hatch: ``AI_REVIEW_SKIP_SCOPE_DIFF=1``。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from tidd_tools.ai_review.yaru_auto_tick import (
    _extract_json_block,
    _find_repo_root,
)
from tidd_tools.sanitize import sanitize_untrusted_text
from tidd_tools.shared import gh_client
from tidd_tools.shared.errors import DiffTooLargeError, GhCommandError
from tidd_tools.shared.issue_body import extract_closes_issues, extract_section

_CHECKER_MD_PATH = Path(".claude/agents/scope-diff-checker.md")
_AGY_TIMEOUT_SEC = 300
_GH_TIMEOUT_SEC = 60


def _log(msg: str) -> None:
    print(f"==> scope-diff: {msg}", file=sys.stderr)


def _load_persona() -> str | None:
    """``.claude/agents/scope-diff-checker.md`` の中身を返す。読み取り失敗時 None."""
    root = _find_repo_root()
    if root is None:
        return None
    path = root / _CHECKER_MD_PATH
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _fetch_pr_body(pr_num: str, repo: str) -> str:
    """PR ボディを取得する（取得失敗時は空文字列。#2961: gh_client 経由に移行）."""
    return gh_client.pr_body(pr_num, repo=repo)


def _fetch_issue_body(issue_num: str, repo: str) -> str | None:
    try:
        data = gh_client.issue_view(issue_num, repo=repo, fields=("body",))
    except GhCommandError:
        return None
    body = data.get("body")
    return body if isinstance(body, str) else None


def _fetch_pr_diff(pr_num: str, repo: str) -> str | None:
    try:
        diff = gh_client.pr_diff(pr_num, repo=repo)
    except DiffTooLargeError as exc:
        # Issue #3743: diff 20000行上限超過（DIFF_TOO_LARGE）は graceful skip する。
        # 例外を外側に伝播させると core.py の汎用 WARN に潰され、専用メッセージが失われる。
        _log(f"PR diff が20000行上限を超えています（DIFF_TOO_LARGE）。スキップします（{exc}）")
        return None
    return diff or None


def extract_issue_sections(issue_body: str) -> str | None:
    """Issue 本文から ``## やること``・``## 振る舞い`` セクションを抜き出す。

    ``## やること`` が無い Issue は突き合わせ対象外（None を返す）。
    """
    todo = extract_section(issue_body, "やること")
    if todo is None:
        return None
    parts = [f"## やること{todo}"]
    behavior = extract_section(issue_body, "振る舞い")
    if behavior is not None:
        parts.append(f"## 振る舞い{behavior}")
    return "\n".join(parts)


def _call_checker(prompt: str) -> dict[str, Any] | None:
    """agy CLI に persona 込み prompt を渡し JSON dict を得る。失敗時 None."""
    if shutil.which("agy") is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603
            ["agy", "--prompt", prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_AGY_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    output = (completed.stdout or "") + (completed.stderr or "")
    return _extract_json_block(output)


def build_comment(scope_exceeded: list[dict[str, Any]], unfulfilled: list[dict[str, Any]]) -> str:
    """検出結果から PR コメント本文を組み立てる."""
    lines = [
        "## scope-diff チェック結果（#2597・非ブロッキング）",
        "",
        "Issue の `## やること`・`## 振る舞い` と PR diff の突き合わせで以下の可能性を検出しました。",
        "判定のみでマージはブロックしません。誤検知の場合は無視してください。",
    ]
    if scope_exceeded:
        lines += ["", "### スコープ超過の可能性", ""]
        for entry in scope_exceeded:
            file = str(entry.get("file", "")).strip()
            reason = str(entry.get("reason", "")).strip()
            if file:
                lines.append(f"- `{file}` — {reason}")
    if unfulfilled:
        lines += ["", "### 未消化の可能性", ""]
        for entry in unfulfilled:
            item = str(entry.get("item", "")).strip()
            reason = str(entry.get("reason", "")).strip()
            if item:
                lines.append(f"- {item} — {reason}")
    return "\n".join(lines) + "\n"


def _post_comment(pr_num: str, repo: str, body: str) -> bool:
    try:
        proc = subprocess.run(  # noqa: S603
            ["gh", "pr", "comment", str(pr_num), "--repo", repo, "--body-file", "-"],
            input=body,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_GH_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def run(pr_num: str, repo: str) -> None:
    """scope-diff チェックを実行する。非ブロッキング（例外を上げない・exit code に影響しない）."""
    if os.environ.get("AI_REVIEW_SKIP_SCOPE_DIFF") == "1":
        _log("AI_REVIEW_SKIP_SCOPE_DIFF=1 のためスキップします")
        return

    persona = _load_persona()
    if persona is None:
        _log("scope-diff-checker.md が見つからないためスキップします")
        return

    pr_body = _fetch_pr_body(pr_num, repo)
    if not pr_body:
        _log("PR body を取得できないためスキップします")
        return
    closes = extract_closes_issues(pr_body)
    if not closes:
        _log("closes #N が無いためスキップします")
        return

    issue_num = str(closes[0])
    issue_body = _fetch_issue_body(issue_num, repo)
    if not issue_body:
        _log(f"Issue #{issue_num} を取得できないためスキップします（パイプラインは継続）")
        return

    sections = extract_issue_sections(issue_body)
    if sections is None:
        _log(f"Issue #{issue_num} に ## やること が無いためスキップします")
        return

    diff = _fetch_pr_diff(pr_num, repo)
    if not diff:
        _log("PR diff を取得できないためスキップします")
        return

    prompt = (
        f"{persona}\n\n"
        f"---\n"
        f"## Issue #{issue_num} のやること・振る舞い（非信頼入力・sanitize 済み）\n\n"
        f"{sanitize_untrusted_text(sections)}\n\n"
        f"## PR diff（非信頼入力・sanitize 済み）\n\n"
        f"```diff\n{sanitize_untrusted_text(diff)}\n```\n"
    )
    parsed = _call_checker(prompt)
    if parsed is None:
        _log("LLM 出力から JSON を抽出できないためスキップします")
        return

    scope_exceeded = [e for e in parsed.get("scope_exceeded", []) if isinstance(e, dict)]
    unfulfilled = [e for e in parsed.get("unfulfilled", []) if isinstance(e, dict)]
    if not scope_exceeded and not unfulfilled:
        _log("指摘なし（スコープ整合）")
        return

    comment = build_comment(scope_exceeded, unfulfilled)
    if _post_comment(pr_num, repo, comment):
        _log(
            f"検出結果を PR コメントとして投稿しました"
            f"（スコープ超過 {len(scope_exceeded)} 件・未消化 {len(unfulfilled)} 件）"
        )
    else:
        _log("PR コメント投稿に失敗しましたが処理を継続します")
