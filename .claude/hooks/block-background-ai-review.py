#!/usr/bin/env python3
"""PreToolUse hook: `tidd ai-review` のバックグラウンド実行をブロックする（Issue #3421）.

`.claude/rules/workflow.md` は ai-review の同期（前景）実行必須
（`run_in_background` / `nohup` / `&` 禁止）と定めているが、機械強制する hook が
無くお願いベースになっていた。バックグラウンド実行されるとレビュー完了（exit code）を
検知できないままセッションが進み、PR が放置される・完了確認のポーリングで余計な
LLM トークンを消費するため、PreToolUse で Bash tool のパラメータを静的に検査して
ブロックする。

判定ロジック（`tool_input.command` に ai-review サブコマンド実行が含まれる場合のみ）:
  (a) `tool_input.run_in_background` が true → exit 2
  (b) ai-review を含むコマンド断片が `nohup` prefix を持つ → exit 2
  (c) ai-review を含むコマンド断片に単独 `&`（`&&` は該当しない・クォート内も該当しない）
      が含まれる → exit 2

コマンドのチェーン分割（`&&` / `||` / `|` / `;` / 改行）は
`_lib/shell_parse.split_shell_fragments()` を再利用する（自前のシェルパーサを持たない）。
`&&` は分割されるため断片内に残らず、断片内の `&` はバックグラウンド化演算子として
検出できる。クォート内の `&`（引数文字列）は quote-aware 走査で誤検知しない。

ai-review を含まないコマンド・Bash 以外の tool は無条件 exit 0 で素通しする。

stdlib のみ使用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import get_command, is_hook_enabled, read_hook_input
from _lib.shell_parse import split_shell_fragments

DETAIL = "詳細: docs/reference/hooks.md#block-background-ai-reviewpy\n"

# `tidd ai-review` / `python -m tidd_tools ai-review`（uv run 経由含む）。
# block-subagent-review-merge.py と同一の正規表現（#3403 の誤検知防止適用済み）。
_AI_REVIEW_RE = re.compile(
    r"\btidd\s+(?:[\w-]+\s+)*ai-review\b|-m\s+tidd_tools(?:\s+[\w./-]+)*\s+ai-review\b"
)

_NOHUP_PREFIX_RE = re.compile(r"^\s*nohup\b")


def _is_run_in_background(value: object) -> bool:
    """tool_input.run_in_background の値が真かどうかを判定する（bool / 文字列対応）."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _has_nohup_prefix(fragment: str) -> bool:
    """コマンド断片が `nohup` prefix（先頭の `nohup` 起動）を持つか判定する."""
    return bool(_NOHUP_PREFIX_RE.search(fragment))


def _has_single_ampersand(fragment: str) -> bool:
    r"""クォート外の単独 `&`（バックグラウンド化演算子）が断片内に存在するか判定する.

    - `&&` は該当しない（2 文字ともスキップする）
    - クォート（`'...'` / `"..."`）内の `&` は該当しない
    - クォート外のバックスラッシュエスケープ（`\&` 等）は該当しない

    `split_shell_fragments()` が `&&` を区切りとして分割済みのため、断片内の
    `&&` は通常存在しないが、防御的にここでも扱う。
    """
    quote: str | None = None
    i = 0
    n = len(fragment)
    while i < n:
        c = fragment[i]
        if quote is not None:
            if quote == '"' and c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "&":
            if fragment[i : i + 2] == "&&":
                i += 2
                continue
            return True
        i += 1
    return False


def _block() -> int:
    sys.stderr.write(
        "BLOCK: ai-review は同期（前景）実行必須です。"
        "run_in_background / nohup / 末尾の `&` を外して再実行してください。\n"
    )
    sys.stderr.write(DETAIL)
    return 2


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    if payload.get("tool_name") != "Bash":
        return 0  # Bash 以外の tool は素通し

    command = get_command(payload)
    if not command:
        return 0

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    # (a) run_in_background 指定（コマンド内のどこかに ai-review があればブロック）
    if _is_run_in_background(
        tool_input.get("run_in_background")
    ) and _AI_REVIEW_RE.search(command):
        return _block()

    # (b) nohup prefix / (c) 単独 `&` は ai-review を含むコマンド断片単位で検査する
    for fragment in split_shell_fragments(command):
        if not _AI_REVIEW_RE.search(fragment):
            continue
        if _has_nohup_prefix(fragment):
            return _block()
        if _has_single_ampersand(fragment):
            return _block()

    return 0


def main() -> int:
    if not is_hook_enabled("block-background-ai-review"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
