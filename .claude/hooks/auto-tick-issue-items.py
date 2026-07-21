#!/usr/bin/env python3
"""PreToolUse hook: `gh pr create` 前に Issue やることの未 tick 項目を機械チェックする（Issue #2315）.

**背景:** APPROVE 後の Issue やること全消化 gate（exit 4）は
`.claude/rules/review-backends.md` により正しい設計だが、tick 漏れを防ぐ手段が
プロンプト上の注意喚起に留まっており機械的な強制がなかった。実装完了時に
チェックボックスの tick を忘れると、初回 `ai-review` が exit 4 でブロックされ
`tick-evidence` 手動実行 → 再実行という追加ラウンドトリップが発生する（PR #2305 実例）。

**動作:**
1. `gh pr create` / `mcp__github__create_pull_request` の body から `closes #N` を抽出する
2. 対象 Issue の `## やること` 未チェック項目（`[手動]`/`[AI確認]`/`[AI確認-post-merge]` prefix は対象外）を取得する
3. 各項目のバッククォート囲みファイルパスが `git diff --name-only origin/main...HEAD`
   （PR の変更ファイル）に含まれていれば「tick 可能」と判定し、Issue body を
   `- [x]` に更新して evidence コメントを投稿する（tick-evidence 相当の処理）
4. tick 可能と判定できなかった未チェック項目が残る場合は stderr に警告を出す（block しない）

**soft-fail（skip）条件（exit 0 + stderr WARN）:**
- `gh` / `git` コマンドが見つからない・タイムアウト
- Issue body 取得失敗・`closes #N` なし
- `git diff` 失敗（この場合は auto-tick を行わず警告のみ試みる）

**終了コード:** 常に 0（block しない・警告のみ）。

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
from _lib.hook_io import (  # noqa: E402
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)

DETAIL = "詳細: docs/reference/hooks.md#auto-tick-issue-itemspy\n"

_GH_PR_CREATE_RE = re.compile(r"(^|&&|;|\|)\s*gh pr create(\s|$)")
_CLOSES_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)
_YARU_HEADER_RE = re.compile(r"^##\s*やること\s*$", re.MULTILINE)
_NEXT_H2_RE = re.compile(r"^##\s+", re.MULTILINE)
_UNCHECKED_ITEM_RE = re.compile(r"^\s*-\s*\[\s\]\s+(.+?)\s*$")
_EXCLUDE_PREFIX_RE = re.compile(r"^\s*\[(手動|AI確認(-post-merge)?)\]")
_BACKTICK_PATH_RE = re.compile(r"`([^`]+)`")
_MCP_CREATE_PR_TOOL = "mcp__github__create_pull_request"


def _extract_body(command: str) -> str:
    """gh pr create コマンドから --body / --body-file の値を抽出する."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--body", "-b") and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--body="):
            return tok[len("--body=") :]
        if tok in ("--body-file", "-F") and i + 1 < len(tokens):
            try:
                return Path(tokens[i + 1]).read_text(encoding="utf-8")
            except OSError:
                return ""
        if tok.startswith("--body-file="):
            try:
                return Path(tok[len("--body-file=") :]).read_text(encoding="utf-8")
            except OSError:
                return ""
        i += 1
    return ""


def extract_closes_issues(pr_body: str) -> list[int]:
    """PR body から `closes|fixes|resolves #N` を重複排除して抽出する."""
    if not pr_body:
        return []
    seen: list[int] = []
    for m in _CLOSES_RE.finditer(pr_body):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def parse_todo_items(issue_body: str) -> list[str]:
    """Issue body の `## やること` から未チェック項目テキストを抽出する.

    `[手動]`/`[AI確認]`/`[AI確認-post-merge]` prefix の項目は auto-tick 対象外
    （後続 STEP・人間確認で消化される想定のため）除外する。
    """
    if not issue_body:
        return []
    header_match = _YARU_HEADER_RE.search(issue_body)
    if not header_match:
        return []
    start = header_match.end()
    next_h2 = _NEXT_H2_RE.search(issue_body, pos=start)
    end = next_h2.start() if next_h2 else len(issue_body)
    section = issue_body[start:end]

    items: list[str] = []
    for line in section.splitlines():
        m = _UNCHECKED_ITEM_RE.match(line)
        if not m:
            continue
        text = m.group(1)
        if _EXCLUDE_PREFIX_RE.match(text):
            continue
        items.append(text)
    return items


def apply_tick(issue_body: str, item_text: str) -> str:
    """`- [ ] <item_text>` 行を `- [x] <item_text>` に置換する（完全一致のみ）."""
    if not item_text:
        return issue_body
    out_lines: list[str] = []
    for line in issue_body.splitlines(keepends=True):
        stripped = line.rstrip("\n").rstrip("\r")
        m = _UNCHECKED_ITEM_RE.match(stripped)
        if m and m.group(1) == item_text:
            newline = line[len(stripped) :]
            indent_end = stripped.index("-")
            indent = stripped[:indent_end]
            out_lines.append(f"{indent}- [x] {item_text}{newline}")
        else:
            out_lines.append(line)
    return "".join(out_lines)


def find_tick_evidence_path(item_text: str, changed_files: list[str]) -> str | None:
    """item_text 内のバッククォート囲みパスが changed_files に含まれれば返す."""
    for path in _BACKTICK_PATH_RE.findall(item_text):
        normalized = path.strip().lstrip("./")
        for f in changed_files:
            if f.lstrip("./") == normalized:
                return f
    return None


def _get_changed_files() -> list[str] | None:
    """`git diff --name-only origin/main...HEAD` で PR 変更ファイル一覧を返す（失敗時 None）."""
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["git", "diff", "--no-color", "--name-only", "origin/main...HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _gh_issue_body(issue_number: int) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["gh", "issue", "view", str(issue_number), "--json", "body"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    body = data.get("body")
    return str(body) if isinstance(body, str) else None


def _gh_issue_edit_body(issue_number: int, new_body: str) -> bool:
    try:
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write(new_body)
            tmp_path = f.name
        result = subprocess.run(  # noqa: S603, S607
            ["gh", "issue", "edit", str(issue_number), "--body-file", tmp_path],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _gh_issue_comment(issue_number: int, comment: str) -> bool:
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["gh", "issue", "comment", str(issue_number), "--body", comment],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _format_evidence_comment(item_text: str, evidence_path: str, pr_hint: str) -> str:
    return (
        "## やること消化 Evidence\n\n"
        "| やること | Evidence |\n"
        "|---------|----------|\n"
        f"| {item_text.replace('|', chr(92) + '|')} | PR 変更ファイル `{evidence_path}` に該当 |\n"
        f"\n_auto-tick-issue-items hook による自動 tick（{pr_hint}）_\n"
    )


def _process_issue(issue_number: int, *, pr_hint: str) -> list[str]:
    """1 Issue 分の auto-tick 処理を行い、tick できなかった未チェック項目一覧を返す."""
    issue_body = _gh_issue_body(issue_number)
    if issue_body is None:
        sys.stderr.write(
            f"WARN: auto-tick-issue-items: Issue #{issue_number} の取得に失敗したため skip します\n"
        )
        return []

    items = parse_todo_items(issue_body)
    if not items:
        return []

    changed_files = _get_changed_files()
    if changed_files is None:
        sys.stderr.write(
            "WARN: auto-tick-issue-items: git diff 取得に失敗したため auto-tick は行わず警告のみ試みます\n"
        )
        changed_files = []

    remaining: list[str] = []
    updated_body = issue_body
    ticked: list[tuple[str, str]] = []
    for item_text in items:
        evidence_path = find_tick_evidence_path(item_text, changed_files)
        if evidence_path is None:
            remaining.append(item_text)
            continue
        updated_body = apply_tick(updated_body, item_text)
        ticked.append((item_text, evidence_path))

    if ticked and updated_body != issue_body:
        if _gh_issue_edit_body(issue_number, updated_body):
            for item_text, evidence_path in ticked:
                comment = _format_evidence_comment(item_text, evidence_path, pr_hint)
                if not _gh_issue_comment(issue_number, comment):
                    sys.stderr.write(
                        f"WARN: auto-tick-issue-items: Issue #{issue_number} への evidence コメント投稿に失敗しました\n"
                    )
            sys.stderr.write(
                f"auto-tick-issue-items: Issue #{issue_number} の {len(ticked)} 件を自動 tick しました: "
                f"{', '.join(t for t, _ in ticked)}\n"
            )
        else:
            sys.stderr.write(
                f"WARN: auto-tick-issue-items: Issue #{issue_number} の body 更新に失敗したため tick できませんでした\n"
            )
            remaining.extend(t for t, _ in ticked)

    return remaining


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)

    if tool_name == _MCP_CREATE_PR_TOOL:
        body = str(payload.get("tool_input", {}).get("body", ""))
        pr_hint = "PR 作成時（mcp__github__create_pull_request）"
    elif tool_name == "Bash":
        command = get_command(payload)
        if not _GH_PR_CREATE_RE.search(command):
            return 0
        body = _extract_body(command)
        pr_hint = "PR 作成時（gh pr create）"
    else:
        return 0

    issue_numbers = extract_closes_issues(body)
    if not issue_numbers:
        return 0

    all_remaining: dict[int, list[str]] = {}
    for issue_number in issue_numbers:
        remaining = _process_issue(issue_number, pr_hint=pr_hint)
        if remaining:
            all_remaining[issue_number] = remaining

    if all_remaining:
        sys.stderr.write(
            "WARN: auto-tick-issue-items: 以下の Issue やること項目は自動 tick できませんでした"
            "（実装差分から判定できないため）。PR作成前に手動で tick するか、"
            "gh pr create 後に ai-review 実行前 tick-evidence で消化してください:\n"
        )
        for issue_number, items in all_remaining.items():
            sys.stderr.write(f"  Issue #{issue_number}:\n")
            for item in items:
                sys.stderr.write(f"    - [ ] {item}\n")
        sys.stderr.write(DETAIL)

    return 0


def main() -> int:
    if not is_hook_enabled("auto-tick-issue-items"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
