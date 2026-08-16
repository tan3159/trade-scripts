"""Issue `## やること` セクション解析・issue body 取得の共通 helper（Issue #2953）.

`auto-tick-issue-items.py` / `block-direct-yaru-tick.py` / `require-yaru-consistency.py` /
`validate-issue.py` に独立実装されていたセクション抽出・checkbox パース・prefix 判定・
issue body 取得を集約する。

**集約前の問題:**
- 未チェック項目 regex が 3 変種（非 MULTILINE 行単位 / MULTILINE / `([ xX])` 両状態）に分裂し、
  `]` と項目テキストの間の空白必須有無など境界挙動が食い違っていた
- issue body 取得（`HOOK_TEST_ISSUE_BODY` override 含め）が 2 hook に逐語コピーされていた
- `require-issue.py` のみ gh_cache（stale-while-revalidate・#1393）を経由し、他 3 hook は
  毎回同期 `gh issue view`（timeout 10 秒）を実行して PreToolUse レイテンシ源になっていた

本モジュールは stdlib のみを使用する（hook はスタンドアロン実行のため GitHub MCP は使用対象外）。

**checkbox item regex の統一方針（厳密側へ統一）:**
`- [ ]` / `- [x]` の `]` と項目テキストの間は「1 文字以上の空白」を必須とする
（旧 3 変種のうち auto-tick-issue-items.py の `\\s+` 版が最も厳密で、Markdown の
一般的な書式と一致するため採用）。項目テキストの前後空白は trim する。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Issue #2953: 兄弟モジュール（gh_cache）を "_lib.gh_cache" として package-qualified import する。
# gh_cache_refresh.py と同じパターン（`_lib/` 自身ではなく親の `hooks/` を sys.path に追加）を
# 踏襲することで、hook 本体側の `from _lib.gh_cache import ...` と同一の sys.modules エントリを
# 共有し、モジュール二重ロード（グローバル状態の分岐）を避ける。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _lib.gh_cache import get_issue as _get_issue_fresh
from _lib.gh_cache import get_issue_or_stale_with_bg_refresh as _get_issue_swr
from _lib.gh_cache import upsert_issue as _upsert_issue

# ── セクション抽出 ──────────────────────────────────────────────────────
#
# 見出し行は「`## <header>` の後に別の見出し annotation（例: `（feat系必須）`）が
# 続いても許容する」validate-issue.py 版の書式を採用する（より広く一致するため安全側）。
# 次セクション境界は行頭 `##`（直後の空白の有無を問わない）とする。

_GENERIC_SECTION_TEMPLATE = r"^##\s*{header}[^\n]*\n?(.*?)(?=^##|\Z)"


def extract_section(body: str, header: str = "やること") -> str | None:
    """Issue body から `## <header>` セクションの本文を抽出する.

    見出し行が存在しない場合は None を返す。
    """
    if not body:
        return None
    pattern = re.compile(
        _GENERIC_SECTION_TEMPLATE.format(header=re.escape(header)),
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(body)
    if not m:
        return None
    return m.group(1)


# ── checkbox item パース ────────────────────────────────────────────────
#
# `]` と項目テキストの間に 1 文字以上の空白を必須とする（厳密側へ統一）。
# セクション全体を対象にする場合は re.MULTILINE 版、1 行ずつ検査する場合は
# 非 MULTILINE 版（`^`/`$` が文字列全体の先頭・末尾を指すため 1 行文字列に対して
# そのまま使える）を使う。

_CHECKBOX_ITEM_PATTERN = r"^\s*-\s*\[([ xX])\]\s+(.+?)\s*$"
CHECKBOX_ITEM_RE = re.compile(_CHECKBOX_ITEM_PATTERN, re.MULTILINE)
_CHECKBOX_ITEM_LINE_RE = re.compile(_CHECKBOX_ITEM_PATTERN)


def parse_checkbox_items(section: str) -> list[tuple[str, bool]]:
    """セクション本文から (項目テキスト, チェック済みか) のリストを順序付きで返す."""
    items: list[tuple[str, bool]] = []
    for m in CHECKBOX_ITEM_RE.finditer(section):
        checked = m.group(1).lower() == "x"
        items.append((m.group(2), checked))
    return items


def parse_unchecked_items(section: str) -> list[str]:
    """セクション本文から未チェック（`- [ ]`）項目のテキストのみを返す."""
    return [text for text, checked in parse_checkbox_items(section) if not checked]


def match_checkbox_line(line: str) -> tuple[str, str] | None:
    """1 行を checkbox item として解析する（(チェック文字, 項目テキスト) または None）.

    `apply_tick` のように行単位で置換対象を判定する用途向け。
    """
    stripped = line.rstrip("\r\n")
    m = _CHECKBOX_ITEM_LINE_RE.match(stripped)
    if not m:
        return None
    return m.group(1), m.group(2)


# ── prefix 判定 ──────────────────────────────────────────────────────────

_MANUAL_OR_AI_CONFIRM_RE = re.compile(r"^\s*\[(手動|AI確認(-post-merge)?)\]")
_POST_MERGE_PREFIX_RE = re.compile(r"^\s*\[AI確認-post-merge\]")


def has_manual_or_ai_confirm_prefix(text: str) -> bool:
    """`[手動]` / `[AI確認]` / `[AI確認-post-merge]` prefix を持つかを判定する."""
    return bool(_MANUAL_OR_AI_CONFIRM_RE.match(text))


def has_post_merge_prefix(text: str) -> bool:
    """`[AI確認-post-merge]` prefix を持つかを判定する（`[AI確認]` 単体は含まない）."""
    return bool(_POST_MERGE_PREFIX_RE.match(text))


# ── issue body 取得（gh_cache 経由 + HOOK_TEST_ISSUE_BODY override） ──────

_ISSUE_VIEW_TIMEOUT_SEC = 10


def _fetch_issue_via_gh(issue_number: int) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--json",
                "number,title,state,body",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_ISSUE_VIEW_TIMEOUT_SEC,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def fetch_issue_body(issue_number: int) -> str | None:
    """Issue body を取得する（gh_cache stale-while-revalidate 優先 + gh 直叩きフォールバック）.

    優先順（require-issue.py と同型パターン・Issue #2953）:
    1. ``HOOK_TEST_ISSUE_BODY`` 環境変数（テスト用 override）
    2. gh_cache fresh hit（``body`` フィールドを持つ場合のみ採用）
    3. gh_cache stale hit（同上。SWR により BG refresh も起動される）
    4. 同期 `gh issue view --json ...,body`（取得できれば cache に upsert して再利用可能にする）

    既存 gh_cache の issue_list は `session-start-cache.py` により ``body`` を含まない
    軽量スキーマで書き込まれるため、fresh/stale hit でも ``body`` を持たない場合は
    cache miss とみなして gh 直叩きにフォールバックする。
    """
    override = os.environ.get("HOOK_TEST_ISSUE_BODY")
    if override is not None:
        return override

    fresh = _get_issue_fresh(issue_number)
    if fresh is not None:
        body = fresh.get("body")
        if isinstance(body, str):
            return body

    cached = _get_issue_swr(issue_number)
    if cached is not None:
        body = cached.get("body")
        if isinstance(body, str):
            return body

    data = _fetch_issue_via_gh(issue_number)
    if data is None:
        return None
    _upsert_issue(issue_number, data)
    body = data.get("body")
    return body if isinstance(body, str) else None


def fetch_issue_body_bypass_cache(issue_number: int) -> str | None:
    """cache を一切参照せず gh 直叩きのみで最新の Issue body を取得する（Issue #3119）.

    `require-yaru-consistency.py` が未チェック項目でのブロックを確定させる直前に、
    gh_cache（TTL 300 秒・SWR）が保持する編集前 body で誤ブロックし続けることを防ぐため、
    1 回だけ同期 `gh issue view` で最新 body を再取得する用途。取得できれば cache に
    upsert し、以降の fresh hit にも反映させる。

    優先順:
    1. ``HOOK_TEST_ISSUE_BODY_FRESH``（テスト用 override・再取得結果のみを差し替える）
    2. ``HOOK_TEST_ISSUE_BODY``（テスト用 override・未設定時のフォールバック。既存テストで
       cache と再取得が同一値であることを前提にできるようにし、実 gh 呼び出しを不要にする）
    3. 同期 `gh issue view --json ...,body`（gh 直叩き失敗時は None を返し、
       呼び出し側は既存のフォールバック挙動＝再取得前の判定結果を維持する）
    """
    fresh_override = os.environ.get("HOOK_TEST_ISSUE_BODY_FRESH")
    if fresh_override is not None:
        return fresh_override
    if "HOOK_TEST_ISSUE_BODY" in os.environ:
        return os.environ["HOOK_TEST_ISSUE_BODY"]

    data = _fetch_issue_via_gh(issue_number)
    if data is None:
        return None
    _upsert_issue(issue_number, data)
    body = data.get("body")
    return body if isinstance(body, str) else None
