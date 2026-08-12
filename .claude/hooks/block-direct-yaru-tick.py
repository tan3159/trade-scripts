#!/usr/bin/env python3
"""PreToolUse hook: Issue body 直接編集による `## やること` tick を検知する（Issue #2912）.

**背景:** Issue #2894 の `## やること` は post-merge 検証待ちの `[AI確認-post-merge]` 項目を
含めて 3 項目とも `[x]` 化されていたが、PR 側の Test plan には該当項目が未チェックのまま
残っており、post-merge 検証が未実施であるにもかかわらず Issue 側だけ済んだことになっていた。
`yaru_auto_tick.py` / `auto-tick-issue-items.py` / `tick_evidence.py` という正規 tick 経路は
いずれも `[AI確認-post-merge]` prefix 項目を除外対象とし、tick 時にエビデンスコメントを
必ず投稿するため、上記の状態はこれら経路では説明できない。一方 `mcp__github__issue_write`
（method=update）や `gh issue edit --body` で Issue body を直接書き換える経路には、
checkbox の `[ ]→[x]` 変化を検知・警告する hook が存在しなかった。本 hook はこの隙間を埋める。

**検知対象:**
- `mcp__github__issue_write`（``tool_input.method == "update"``）
- `Bash` の `gh issue edit ... --body <text>` / `--body-file <path>`
  （`--body-file` は PreToolUse 実行時点でファイルが未作成の場合、コマンド文字列中の
  ヒアドキュメント本文を fallback 抽出する。`--body-file -`（標準入力）も対応・#2917）

**判定ロジック:**
1. 更新前の Issue body を取得する（``gh issue view <N> --json body -q .body``。
   テスト用に ``HOOK_TEST_ISSUE_BODY`` 環境変数で override 可能）
2. 更新前後それぞれの `## やること` セクションから checkbox 項目（テキスト → チェック状態）を抽出する
3. 更新後に `[ ]→[x]` へ変化した項目のうち:
   - `[AI確認-post-merge]` prefix 付きなら **block（exit 2）**
     （post-merge 検証は verify-post-merge cron 経由でのみ tick されるべきため）
   - `[手動]`/`[AI確認]`（`-post-merge` を除く）prefix なしの通常項目なら **warning（exit 0 + stderr）**
     （false positive 回避のため block はしない。エビデンスコメント投稿を促す）
   - `[手動]`/`[AI確認]`（`-post-merge` を除く）prefix 付きは対象外（人間による正当な直接編集を許容）

**silent skip 条件（exit 0）:**
- 対象外の tool 呼び出し（`mcp__github__issue_write` 以外の MCP tool・`gh issue edit` 以外の Bash コマンド）
- `method != "update"`・`body` パラメータなし
- Issue 番号が特定できない
- 更新前 Issue body の取得に失敗（`gh` コマンド不在・timeout・API エラー）
- 更新前後どちらかに `## やること` セクションが存在しない（例外を送出せず許可する）

stdlib のみ使用。
"""

from __future__ import annotations

import difflib
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)
from _lib.shell_parse import find_heredocs as _find_heredocs
from _lib.shell_parse import strip_heredoc_bodies as _strip_heredoc_bodies

# Issue #2998 レビュー指摘: `_lib.yaru_sections` は `_lib.gh_cache` を経由し import 時に
# `git remote get-url origin` を実行するため、matcher: "Bash"/"mcp__github__issue_write" で
# あらゆる該当ツール呼び出しに反応する本 hook では、対象 Issue 番号・body を特定できた
# 後にのみ遅延 import する（auto-tick-issue-items.py と同様のパターン）。

DETAIL = "詳細: docs/reference/hooks.md#block-direct-yaru-tickpy\n"

_MCP_ISSUE_WRITE_TOOL = "mcp__github__issue_write"

_GH_ISSUE_EDIT_RE = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*gh\s+issue\s+edit\b([^\n&|;]*)",
    re.MULTILINE,
)
_ISSUE_NUM_RE = re.compile(r"(?<!\S)(\d+)(?!\S)")

# Issue #2912 PR #2917 レビュー指摘: PreToolUse は Bash 実行前に走るため、
# `cat > <path> <<'EOF' ... EOF && gh issue edit ... --body-file <path>` のように
# 同一コマンド内でヒアドキュメントから一時ファイルを作成してから --body-file する
# 通常パターンでは、hook 実行時点でファイルがまだ存在せず read_text が失敗する。
# その場合コマンド文字列中のヒアドキュメント本文を fallback として抽出する。
# Issue #2951: heredoc の検出・本文抽出は `_lib/shell_parse.find_heredocs` に集約した。


def _find_heredoc_body(command: str, path: str) -> str | None:
    """command 中のヒアドキュメントから ``path`` に書き込まれる本文を抽出する.

    ``path == "-"``（標準入力経由）はコマンド中最初のヒアドキュメントを body とみなす。
    それ以外は、ヒアドキュメント開始行に ``path`` と `>`/`tee` を含む場合のみ対象とする。
    """
    matches = _find_heredocs(command)
    if not matches:
        return None
    if path == "-":
        return matches[0].body
    for m in matches:
        redirect_line = command[m.open_line_start : m.open_start]
        if path in redirect_line and (">" in redirect_line or "tee" in redirect_line):
            return m.body
    return None


def _read_body_file(command: str, path: str) -> str | None:
    """--body-file の path から本文を読む（heredoc fallback 付き）."""
    if path != "-":
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            pass
    return _find_heredoc_body(command, path)


# Issue #2912 PR #2917 レビュー指摘（claude-code, HIGH）: ヒアドキュメント本文は
# shell の `<<` 構文に従う自由形式テキストであり、shlex（POSIX シェルの引用符
# ルール）の対象外である。本文中の引用符（アポストロフィ等）が奇数個だと、
# 本文込みのコマンド全体を ``shlex.split`` に渡した際に ``ValueError`` となり
# fail-open（silent skip）してしまう。本文部分だけを空にしてから
# ``shlex.split`` することで、この種の入力でも安全に解析できる。
# ヒアドキュメント本文自体は ``_find_heredoc_body`` が元の ``command``
# （このストリップを経ていない文字列）から別途抽出する。
# Issue #2951: ストリップ処理自体は `_lib/shell_parse.strip_heredoc_bodies` に集約した
# （`_strip_heredoc_bodies` としてインポート済み）。


def _extract_body_from_edit_command(command: str) -> str | None:
    """`gh issue edit` コマンドから --body / --body-file の値を抽出する."""
    try:
        tokens = shlex.split(_strip_heredoc_bodies(command))
    except ValueError:
        return None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--body" and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--body="):
            return tok[len("--body=") :]
        if tok == "--body-file" and i + 1 < len(tokens):
            return _read_body_file(command, tokens[i + 1])
        if tok.startswith("--body-file="):
            return _read_body_file(command, tok[len("--body-file=") :])
        i += 1
    return None


def _extract_issue_number_from_edit_command(command: str) -> int | None:
    m = _GH_ISSUE_EDIT_RE.search(command)
    if not m:
        return None
    args_part = m.group(1)
    for token in _ISSUE_NUM_RE.finditer(args_part):
        try:
            return int(token.group(1))
        except ValueError:
            continue
    return None


def _get_after_body_and_issue_number(
    payload: dict,
) -> tuple[int | None, str | None]:
    """PreToolUse payload から (Issue 番号, 更新後 body) を抽出する.

    対象外・抽出不能な場合は ``(None, None)`` を返す。
    """
    tool_name = get_tool_name(payload)

    if tool_name == _MCP_ISSUE_WRITE_TOOL:
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None, None
        if str(tool_input.get("method", "")) != "update":
            return None, None
        body = tool_input.get("body")
        if not isinstance(body, str):
            return None, None
        issue_number = tool_input.get("issue_number")
        try:
            issue_number = int(issue_number)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None, None
        return issue_number, body

    if tool_name == "Bash":
        command = get_command(payload)
        if not command:
            return None, None
        issue_number = _extract_issue_number_from_edit_command(command)
        if issue_number is None:
            return None, None
        body = _extract_body_from_edit_command(command)
        if body is None:
            return None, None
        return issue_number, body

    return None, None


def _detect_tick_transitions(
    before_body: str, after_body: str
) -> tuple[list[str], list[str]]:
    """更新前後の body を比較し (block 対象項目, warning 対象項目) を返す.

    `## やること` セクションが更新前後どちらかに存在しない場合は例外を送出せず
    ``([], [])`` を返す（対象外扱い）。

    Issue #2912 PR #2917 レビュー指摘（claude-code, MEDIUM）: 項目テキストの
    完全一致だけで before/after を突き合わせると、tick と同時に文言を変更した
    項目が「新規追加」扱いになりすり抜けてしまう。``difflib.SequenceMatcher``
    で項目リストを位置合わせし、同一位置に対応する項目（``equal``/``replace``）
    を before/after のペアとして扱うことで、文言修正を伴う tick も検知する。
    """
    from _lib.yaru_sections import extract_section as _extract_yaru_section
    from _lib.yaru_sections import has_manual_or_ai_confirm_prefix
    from _lib.yaru_sections import has_post_merge_prefix as _is_post_merge_prefix
    from _lib.yaru_sections import parse_checkbox_items as _parse_checkbox_items

    before_section = _extract_yaru_section(before_body)
    after_section = _extract_yaru_section(after_body)
    if before_section is None or after_section is None:
        return [], []

    before_items = _parse_checkbox_items(before_section)
    after_items = _parse_checkbox_items(after_section)
    before_texts = [text for text, _ in before_items]
    after_texts = [text for text, _ in after_items]

    matcher = difflib.SequenceMatcher(a=before_texts, b=after_texts, autojunk=False)

    blocked: list[str] = []
    warn: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in ("equal", "replace"):
            # "insert"（新規追加）・"delete"（削除）は対象外
            continue
        for offset in range(min(i2 - i1, j2 - j1)):
            checked_before = before_items[i1 + offset][1]
            after_text, checked_after = after_items[j1 + offset]
            if checked_before or not checked_after:
                # 更新前に unchecked だった項目が checked に変化した場合のみ
                # 「直接 tick」とみなす。既に checked だった項目は対象外。
                continue
            if _is_post_merge_prefix(after_text):
                blocked.append(after_text)
            elif not has_manual_or_ai_confirm_prefix(after_text):
                warn.append(after_text)
            # [手動] / [AI確認]（-post-merge を除く）prefix 付きは対象外（人間の正当な直接編集を許容）

    return blocked, warn


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    issue_number, after_body = _get_after_body_and_issue_number(payload)
    if issue_number is None or after_body is None:
        return 0

    from _lib.yaru_sections import fetch_issue_body as _fetch_issue_body

    before_body = _fetch_issue_body(issue_number)
    if before_body is None:
        # 取得失敗は silent skip（network エラー等で通常操作を止めない）
        return 0

    blocked, warn = _detect_tick_transitions(before_body, after_body)

    if blocked:
        sys.stderr.write(
            f"Blocked: Issue #{issue_number} の直接編集で [AI確認-post-merge] 項目が"
            " tick されようとしています。\n\n"
            f"検知項目 ({len(blocked)} 件):\n"
        )
        for item in blocked:
            sys.stderr.write(f"  - [x] {item[:150]}\n")
        sys.stderr.write(
            "\npost-merge 項目は verify-post-merge cron 経由でのみ tick してください"
            "（yaru_auto_tick.py / auto-tick-issue-items.py / tick_evidence.py の"
            "いずれも本項目を tick 対象外としています）。\n"
            f"\n{DETAIL}"
        )
        return 2

    if warn:
        sys.stderr.write(
            f"WARN: Issue #{issue_number} の やること項目が直接編集で tick されようとしています。\n\n"
            f"対象項目 ({len(warn)} 件):\n"
        )
        for item in warn:
            sys.stderr.write(f"  - [x] {item[:150]}\n")
        sys.stderr.write(
            "\nエビデンスコメントの投稿を検討してください"
            "（`tidd tick-evidence` 等の正規経路を使うと自動で投稿されます）。\n"
            f"{DETAIL}"
        )

    return 0


def main() -> int:
    if not is_hook_enabled("block-direct-yaru-tick"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
