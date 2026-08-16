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

Issue #3792: 検出（`is_gh_pr_create` / `is_gh_pr_merge`）は当初コマンド文字列全体への
正規表現サーチだったため、`grep -n "gh pr create" file.py`（grep のパターン引数）や
拡張正規表現の alternation（``a\\|gh pr create\\|b`` 中のエスケープされた `|`）のような、
「実行対象ではない箇所」に含まれるリテラルテキストにも誤爆していた。heredoc 本文除去
（`strip_heredoc_bodies`）→ チェーン分割（`split_shell_fragments`）→ 各フラグメントを
`shlex.split` してコマンドトークン列の先頭が `gh pr create`/`gh pr merge` と一致するかを
判定する方式に変更し、実行されないリテラルテキストと実際の呼び出しを区別する。
`shlex.split` が壊れた引用符で失敗した場合のみ、見逃し（false negative）を避けるため
フラグメント単位の正規表現フォールバックを使う。

**closes/refs Issue 番号抽出の 2 定数（Issue #2638 由来の乖離の明示化）:**
`auto-tick-issue-items.py`（PR body の Issue やること全消化 gate 用途）は
`closes`/`fixes`/`resolves` のみを受理し、`require-issue.py`（コミットメッセージの
NO TICKET NO WORK gate 用途）は `refs` も受理する。用途による意図的な差異のため、
``CLOSES_RE``（refs を含まない）と ``CLOSES_OR_REFS_RE``（refs を含む）の
2 定数として集約し、各 hook が用途に応じて選択する。

**Issue #3817:** `gh` 一本化（GitHub MCP 廃止・`docs/decisions/2026-08-14-abolish-github-mcp.md`）
に伴い、`record-timing-boundaries.py` が STEP 1.5-d の needs-human-input（park）・
epic-split（Epic 化）分岐を検知する対象が到達不能になった `mcp__github__issue_write` /
`mcp__github__sub_issue_write` から `gh issue edit --add-label` / `gh issue create --parent`
へ移った。`is_gh_pr_create`/`is_gh_pr_merge` の検出ロジック（heredoc 除去 → チェーン分割 →
トークン列比較）を `_is_gh_command()` へ一般化し、`is_gh_issue_create`/`is_gh_issue_edit`
と共有する。加えて `gh issue create` の成功判定（stdout の Issue URL）・
`--parent`/`--add-label` の値抽出・`gh issue edit` の対象 Issue 番号抽出のユーティリティを
追加した。
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
from _lib.shell_parse import split_shell_fragments as _split_shell_fragments
from _lib.shell_parse import strip_heredoc_bodies as _strip_heredoc_bodies

# フラグメント単位のフォールバック（Issue #3792）: shlex.split がクォート不整合で
# 失敗した場合のみ使用する。フラグメント先頭（strip 済み）からの一致に限定するため、
# 旧実装のようなコマンド文字列全体へのサーチではなく `^` アンカーのみで足りる。
_GH_PR_CREATE_PREFIX_RE = re.compile(r"^gh[ \t]+pr[ \t]+create\b")
_GH_PR_MERGE_PREFIX_RE = re.compile(r"^gh[ \t]+pr[ \t]+merge\b")
_GH_ISSUE_CREATE_PREFIX_RE = re.compile(r"^gh[ \t]+issue[ \t]+create\b")
_GH_ISSUE_EDIT_PREFIX_RE = re.compile(r"^gh[ \t]+issue[ \t]+edit\b")

#: プレフィックス（トークン列）ごとのフォールバック正規表現（Issue #3817）。
#: `_fragment_invokes_gh()` が shlex 解析失敗時に使う。
_PREFIX_FALLBACK_RES: dict[tuple[str, ...], re.Pattern[str]] = {
    ("gh", "pr", "create"): _GH_PR_CREATE_PREFIX_RE,
    ("gh", "pr", "merge"): _GH_PR_MERGE_PREFIX_RE,
    ("gh", "issue", "create"): _GH_ISSUE_CREATE_PREFIX_RE,
    ("gh", "issue", "edit"): _GH_ISSUE_EDIT_PREFIX_RE,
}

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

# gh issue create 成功時の stdout に出力される Issue URL（Issue #3817）。
# `gh pr create` と同様、`gh issue create` は成功時に
# `https://github.com/<owner>/<repo>/issues/<番号>` を stdout へ出力する。
ISSUE_URL_RE = re.compile(r"https?://github\.com/[\w.-]+/[\w.-]+/issues/(\d+)")


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


def extract_issue_number_from_url(stdout: str) -> int | None:
    """``gh issue create`` の stdout から Issue 番号を抽出する（Issue #3817）.

    Issue URL に一致しない・stdout が空の場合は None を返す（＝失敗した
    ``gh issue create``。呼び出し側は記録しない）。
    """
    if not stdout:
        return None
    match = ISSUE_URL_RE.search(stdout)
    if not match:
        return None
    return int(match.group(1))


def _fragment_invokes_gh(fragment: str, prefix: tuple[str, ...]) -> bool:
    """1 フラグメント（チェーン区切り済みの単一コマンド）が ``prefix`` の gh 呼び出しを実行するか判定する.

    フラグメントを ``shlex.split`` してコマンドトークン列の先頭が ``prefix``
    （例: ``("gh", "pr", "create")``・``("gh", "issue", "edit")``）と一致するかを見る。
    単なる文字列データ（grep のパターン引数・echo の出力文字列等）はコマンドの
    先頭に来ないため、トークン列比較であれば誤検知しない（Issue #3792）。

    **Issue #3817:** 元々 `gh pr <sub>` 専用だった判定を任意プレフィックスへ一般化し、
    `gh issue create` / `gh issue edit` の検出（`is_gh_issue_create` /
    `is_gh_issue_edit`）にも同じロジックを共有する。
    """
    stripped = fragment.strip()
    if not stripped:
        return False
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        # クォート不整合で shlex が解析できない場合、判定不能として黙って
        # False を返すと実際の呼び出しを見逃す（false negative）リスクがある。
        # 安全側に倒し、フラグメント先頭の正規表現一致で保守的に判定する。
        prefix_re = _PREFIX_FALLBACK_RES.get(prefix)
        if prefix_re is None:
            return False
        return bool(prefix_re.match(stripped))
    return tuple(tokens[: len(prefix)]) == prefix


def _is_gh_command(command: str, prefix: tuple[str, ...]) -> bool:
    """command 内に実際に実行される ``prefix`` の gh 呼び出しが含まれるか判定する.

    heredoc 本文（実行対象ではないリテラルテキスト）を除去した上で、シェルの
    チェーン区切り（``&&``・``||``・``|``・``;``・改行）でフラグメントへ分割し、
    各フラグメントをコマンドトークン列として判定する（Issue #3792・#3817）。
    """
    if not command:
        return False
    stripped_command = _strip_heredoc_bodies(command)
    return any(
        _fragment_invokes_gh(fragment, prefix)
        for fragment in _split_shell_fragments(stripped_command)
    )


def is_gh_pr_create(command: str) -> bool:
    """command 内に実際に実行される `gh pr create` 呼び出しが含まれるか判定する.

    改行区切りチェーン（``cmd1\\ngh pr create ...``）と連続空白
    （``gh  pr create``）を見逃さない（旧 variant A の既知の見逃しパターンを
    解消・Issue #2952）。grep のパターン引数・echo の出力文字列・heredoc 本文など、
    実行対象ではない箇所に含まれるリテラルテキストには誤検知しない（Issue #3792）。
    """
    return _is_gh_command(command, ("gh", "pr", "create"))


def is_gh_pr_merge(command: str) -> bool:
    """command 内に実際に実行される `gh pr merge` 呼び出しが含まれるか判定する.

    `gh pr create` と同様、改行区切りチェーン・連続空白を見逃さず（#3398・
    sweep-merged-branches hook が使用）、実行対象ではないリテラルテキストには
    誤検知しない（Issue #3792）。
    """
    return _is_gh_command(command, ("gh", "pr", "merge"))


def is_gh_issue_create(command: str) -> bool:
    """command 内に実際に実行される `gh issue create` 呼び出しが含まれるか判定する（Issue #3817）.

    `is_gh_pr_create` と同じ検出ロジック（heredoc 除去 → チェーン分割 →
    トークン列比較）を共有する。STEP 1.5-d の epic-split 分岐（`--parent <N>`
    でサブ Issue を作成）の検知に使う。
    """
    return _is_gh_command(command, ("gh", "issue", "create"))


def is_gh_issue_edit(command: str) -> bool:
    """command 内に実際に実行される `gh issue edit` 呼び出しが含まれるか判定する（Issue #3817）.

    STEP 1.5-d の needs-human-input（park）分岐（`--add-label "🙋 needs-human-input"`）
    の検知に使う。
    """
    return _is_gh_command(command, ("gh", "issue", "edit"))


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
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
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


# `--parent <N>` / `--parent=<N>` の値（数字末尾）を切り出す（Issue #3817）。
# `gh issue create --parent <number or URL>` はプレーンな数字または
# `https://github.com/<owner>/<repo>/issues/<N>` 形式の URL を受け付けるため、
# どちらでも値文字列の末尾の数字列を Issue 番号として解釈する。
_TRAILING_NUMBER_RE = re.compile(r"(\d+)\s*$")


def extract_gh_issue_create_parent(command: str) -> int | None:
    """``gh issue create --parent <N>`` の親 Issue 番号を抽出する（Issue #3817）.

    STEP 1.5-d の epic-split 分岐（元 Issue を Epic 化し関心事ごとにサブ Issue を
    ``gh issue create --parent <親番号>`` で追加する）の親 Issue 番号解決に使う。
    ``--parent`` 未指定・shlex 解析失敗・値が数字/Issue URL のいずれでもない場合は
    None を返す（呼び出し側は記録をスキップする）。
    """
    if not command:
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    value: str | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--parent" and i + 1 < len(tokens):
            value = tokens[i + 1]
            i += 2
        elif tok.startswith("--parent="):
            value = tok[len("--parent=") :]
            i += 1
        else:
            i += 1
    if value is None:
        return None
    match = _TRAILING_NUMBER_RE.search(value)
    if not match:
        return None
    return int(match.group(1))


def gh_issue_edit_added_label(command: str, label: str) -> bool:
    """``gh issue edit ... --add-label <label>`` で ``label`` が追加指定されているか判定する（Issue #3817）.

    ``--add-label`` はカンマ区切りで複数ラベルを 1 指定にまとめられる
    （例: ``gh issue edit 23 --add-label "bug,help wanted"``）ため、値をカンマで
    分割し前後の空白を trim してから比較する。``--add-label`` が複数回指定されて
    いる場合はいずれかに一致すれば True を返す。shlex 解析失敗時は False（安全側）。
    """
    if not command:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        value: str | None = None
        if tok == "--add-label" and i + 1 < len(tokens):
            value = tokens[i + 1]
            i += 2
        elif tok.startswith("--add-label="):
            value = tok[len("--add-label=") :]
            i += 1
        else:
            i += 1
            continue
        names = [part.strip() for part in value.split(",")]
        if label in names:
            return True
    return False


def extract_gh_issue_edit_numbers(command: str) -> list[int]:
    """``gh issue edit {<numbers> | <urls>} [flags]`` の対象 Issue 番号を抽出する（Issue #3817）.

    `gh issue edit` 直後からフラグ（``-`` 始まりのトークン）が現れるまでの
    位置引数を対象 Issue として扱う。数字・Issue URL のいずれかから末尾の数字列を
    抽出する（複数指定 ``gh issue edit 23 34 --add-label ...`` にも対応）。
    `gh issue edit` 呼び出しを含まない・位置引数がない場合は空リストを返す。
    """
    if not command:
        return []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    prefix = ("gh", "issue", "edit")
    for idx in range(len(tokens) - len(prefix) + 1):
        if tuple(tokens[idx : idx + len(prefix)]) != prefix:
            continue
        numbers: list[int] = []
        j = idx + len(prefix)
        while j < len(tokens) and not tokens[j].startswith("-"):
            match = _TRAILING_NUMBER_RE.search(tokens[j])
            if match:
                numbers.append(int(match.group(1)))
            j += 1
        return numbers
    return []
