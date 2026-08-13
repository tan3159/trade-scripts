"""`gh pr create` 検出・PR body 抽出・closes/refs Issue 番号抽出の共通ユーティリティ.

Issue #2952: 「Bash コマンドから `gh pr create` を検出して `--body`/`--body-file` を
抽出する」処理が以下 8 hook にそれぞれ独立実装され、2 系統 3 実装に分裂していた:

- 検出 variant A（改行区切りチェーンと連続空白 `gh  pr create` を見逃す）:
  `auto-tick-issue-items.py` / `require-red-first.py` / `require-mypy.py` /
  `label-pr.py` / `require-issue-id-in-pr-title.py` / `require-ruff-format.py`
- 検出 variant B（`\\n` 区切り・`[ \\t]+` 対応・見逃しなし）:
  `protect-tests.py` / `detect-ai-confirm-misuse.py`
- body 抽出 3 流派: shlex 先勝ち / shlex 後勝ち / regex + heredoc 対応
  （shlex 系は heredoc 非対応で結果が異なる）

本モジュールは検出を variant B 相当（厳密側）に、body 抽出を heredoc 対応の
shlex 後勝ちに統一する。stdlib のみ使用。

**closes/refs Issue 番号抽出の 2 定数（Issue #2638 由来の乖離の明示化）:**
`auto-tick-issue-items.py`（PR body の Issue やること全消化 gate 用途）は
`closes`/`fixes`/`resolves` のみを受理し、`require-issue.py`（コミットメッセージの
NO TICKET NO WORK gate 用途）は `refs` も受理する。用途による意図的な差異のため、
``CLOSES_RE``（refs を含まない）と ``CLOSES_OR_REFS_RE``（refs を含む）の
2 定数として集約し、各 hook が用途に応じて選択する。
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

# Issue #2952: 兄弟モジュール（shell_parse）を "_lib.shell_parse" として
# package-qualified import する。`_lib/yaru_sections.py`（Issue #2953）と同じパターン
# （`_lib/` 自身ではなく親の `hooks/` を sys.path に追加）を踏襲することで、hook 本体側の
# `from _lib.shell_parse import ...` と同一の sys.modules エントリを共有し、
# モジュール二重ロード（グローバル状態の分岐）を避ける。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _lib.shell_parse import find_heredoc_body as _find_heredoc_body

# 検出: 改行区切りチェーン（`\n`）・連続空白（`[ \t]+`）に対応した厳密側（旧 variant B）へ統一。
# `protect-tests.py:49` が採用していた regex を基準にする。
_GH_PR_CREATE_RE = re.compile(r"(?:^|&&|\|\||\||;|\n)[ \t]*gh[ \t]+pr[ \t]+create\b")
_GH_PR_MERGE_RE = re.compile(r"(?:^|&&|\|\||\||;|\n)[ \t]*gh[ \t]+pr[ \t]+merge\b")

# closes/refs 抽出: 用途によって受理キーワードが異なるため 2 定数を提供する（Issue #2638 由来）。
CLOSES_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)
CLOSES_OR_REFS_RE = re.compile(
    r"\b(?:closes|fixes|resolves|refs)\s+#(\d+)", re.IGNORECASE
)

# gh pr create 成功時の stdout に出力される PR URL（Issue #3552）。
# `gh pr create` は成功時に `https://github.com/<owner>/<repo>/pull/<番号>` を
# stdout へ出力するため、この正規表現で PR 番号を抽出する。hook 側で再定義せず
# `record-timing-boundaries.py` が本定数を使用する（成功判定の唯一の真実源）。
PR_URL_RE = re.compile(r"https?://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")


def extract_pr_number_from_url(stdout: str) -> int | None:
    """``gh pr create`` の stdout から PR 番号を抽出する（Issue #3552）.

    PR URL に一致しない・stdout が空の場合は None を返す（＝失敗した
    ``gh pr create``。呼び出し側は記録しない）。
    """
    if not stdout:
        return None
    match = PR_URL_RE.search(stdout)
    if not match:
        return None
    return int(match.group(1))


def is_gh_pr_create(command: str) -> bool:
    """command 内に `gh pr create` 呼び出しが含まれるか判定する.

    改行区切りチェーン（``cmd1\\ngh pr create ...``）と連続空白
    （``gh  pr create``）を見逃さない（旧 variant A の既知の見逃しパターンを
    解消・Issue #2952）。
    """
    if not command:
        return False
    return bool(_GH_PR_CREATE_RE.search(command))


def is_gh_pr_merge(command: str) -> bool:
    """command 内に `gh pr merge` 呼び出しが含まれるか判定する.

    `gh pr create` と同様、改行区切りチェーン・連続空白を見逃さない
    （#3398・sweep-merged-branches hook が使用）。
    """
    if not command:
        return False
    return bool(_GH_PR_MERGE_RE.search(command))


def _read_text_or_empty(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def extract_pr_body(command: str) -> str:
    """`gh pr create` コマンドから PR body を抽出する（heredoc 対応・best-effort）.

    優先順:

    1. コマンド内に heredoc があればその本文を返す
       （``--body "$(cat <<'EOF' ... EOF)"`` 形式・`_lib/shell_parse` に委譲。
       heredoc 本文は引用符バランスが崩れがちで shlex 解析を誤動作させるため、
       shlex より先に判定する）
    2. ``--body``/``-b``/``--body=`` と ``--body-file``/``-F``/``--body-file=`` を
       shlex で走査し、複数指定されていれば最後に出現した指定を採用する
       （実際の ``gh`` CLI と同じく後勝ち）

    どちらにも該当しない場合、または shlex 解析に失敗した場合は空文字列を返す
    （例外は送出しない）。
    """
    if not command:
        return ""

    heredoc_body = _find_heredoc_body(command)
    if heredoc_body is not None:
        return heredoc_body

    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""

    body = ""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--body", "-b") and i + 1 < len(tokens):
            body = tokens[i + 1]
            i += 2
        elif tok.startswith("--body="):
            body = tok[len("--body=") :]
            i += 1
        elif tok in ("--body-file", "-F") and i + 1 < len(tokens):
            body = _read_text_or_empty(tokens[i + 1])
            i += 2
        elif tok.startswith("--body-file="):
            body = _read_text_or_empty(tok[len("--body-file=") :])
            i += 1
        else:
            i += 1
    return body


def extract_closes_issues(
    text: str, *, pattern: re.Pattern[str] = CLOSES_RE
) -> list[int]:
    """text から closes/fixes/resolves（``pattern`` 次第で refs も）参照される

    Issue 番号を出現順・重複排除して抽出する。

    Args:
        text: 検索対象の文字列（PR body・コミットメッセージ等）。
        pattern: 使用する正規表現（デフォルト ``CLOSES_RE``）。
            ``refs`` も受理したい場合は ``CLOSES_OR_REFS_RE`` を渡す。
    """
    if not text:
        return []
    seen: list[int] = []
    for m in pattern.finditer(text):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def gh_pr_view_json(pr_num: str, fields: list[str]) -> dict | None:
    """`gh pr view <pr_num> --json <fields>` を実行して JSON dict を返す（Issue #3558）.

    ``gh`` CLI はタイムアウト・認証失敗・対象 PR 不在等で非 0 終了になることがあるため、
    hook は例外を投げず ``None`` を返して記録をスキップする（tool 実行をブロックしない）。

    - ``subprocess`` + タイムアウト指定（stdlib のみ・#3558）
    - 失敗時は ``None``。正常時でも stdout が JSON でない場合・dict でない場合は ``None``
    - PR 番号の解決は record-timing-boundaries.py（#3558）と同一ロジックで共有するため、
      この関数に集約する（同じ処理を 2 箇所に書かない・#3558 やること 2）
    """
    if not pr_num:
        return None
    cmd = ["gh", "pr", "view", str(pr_num), "--json", ",".join(fields)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None
