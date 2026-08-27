"""`tidd ai-review --verify-ai-confirm` 機能（Issue #1304 で全面書き換え）.

**設計方針（監査 §3-1 Anthropic API 全廃）:**

本モジュールは Anthropic SDK を直接呼び出さない。実際の ``[AI確認]`` 項目検証は
``.claude/skills/issue-next/SKILL.md`` が Agent tool 経由で
``.claude/agents/ai-confirm-verifier.md`` を起動して行う。

Python 側（tidd_tools）は subprocess 起動された時点で **Claude Code セッション外** に
位置するため、Agent tool を呼べない。したがって:

- CLI 経由（``tidd ai-review --verify-ai-confirm``）で呼ばれた場合 → 常に skip 警告を返す
- session_detector で Claude Code セッション外を検知したら stderr に警告を出す
- PR ボディは変更せず ``skipped=True`` で返す
- exit code 0 で終了する（互換性維持）

呼び出し元 skill (``/issue-next``) は本関数の skip 結果を受けて、自身で Agent tool
起動して verification を実行する責務を持つ。

**互換性:**

- ``VerifyResult`` の shape は #1246 実装から変更なし（呼び出し元 subcommands.py 互換）
- ``_extract_ai_confirm_lines()`` / ``_mark_line_done()`` は skill 側からも参照可能な
  公開ヘルパーとして残す（他モジュール・テストからの依存を維持）

**関連:**

- Issue #1281 (``ban-anthropic-import.py`` hook・Anthropic SDK 直接インポート禁止)
- Issue #1246 (旧実装。本ファイルで置き換え)
- ``.claude/hooks/_lib/session_detector.py``
- ``.claude/agents/ai-confirm-verifier.md``
- ``.claude/skills/issue-next/SKILL.md``
- ``docs/reference/session-detector.md``
"""

from __future__ import annotations

import dataclasses
import re
import sys
import types
from pathlib import Path
from typing import cast

# session_detector を .claude/hooks/_lib/ から import する。
# tidd_tools は project 側なので、リポジトリルート起点の相対 import は使えない。
# 代わりに sys.path を動的に足して session_detector を読み込む共通ヘルパーを噛ませる。


def _load_session_detector() -> types.ModuleType | None:
    """`.claude/hooks/_lib/session_detector.py` を動的 import する.

    tidd_tools 側から .claude 配下の共通ライブラリを読むための橋渡し。
    見つからない場合は None を返す（開発環境差の吸収）。

    Issue #2224: `__file__` 起点は uv tool install（site-packages 配下）で
    解決できないため、CWD 優先の `shared.paths.resolve_repo_root` に統一する。
    """
    from tidd_tools.shared.paths import resolve_repo_root

    try:
        repo_root = resolve_repo_root()
    except FileNotFoundError:
        return None
    lib_dir = repo_root / ".claude" / "hooks" / "_lib"
    if not lib_dir.is_dir():
        return None
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    try:
        import session_detector  # type: ignore[import-not-found]
    except ImportError:
        return None
    return cast(types.ModuleType, session_detector)


AI_CONFIRM_LINE_RE = re.compile(r"^([ \t]*- \[)( )(\][ \t]*\[AI確認\][^\n]*)$")


@dataclasses.dataclass
class VerifyResult:
    """[AI確認] 項目の検証結果.

    Attributes:
        updated_body: 更新後の PR ボディ全文（skip 時は元の body をそのまま返す）
        verified_items: 検証成功した [AI確認] 行のテキスト（stripped 済み）
        unverified_items: 検証失敗した [AI確認] 行
        skipped: session 外・依存不足等で検証をスキップしたか
    """

    updated_body: str
    verified_items: list[str] = dataclasses.field(default_factory=list)
    unverified_items: list[str] = dataclasses.field(default_factory=list)
    skipped: bool = False


def _extract_ai_confirm_lines(body: str) -> list[tuple[int, str]]:
    """PR ボディから ``- [ ] [AI確認]`` 行を (行番号, 行文字列) で返す."""
    results: list[tuple[int, str]] = []
    for idx, line in enumerate(body.splitlines()):
        if AI_CONFIRM_LINE_RE.match(line):
            results.append((idx, line))
    return results


def _mark_line_done(body: str, line_index: int) -> str:
    """PR ボディの ``line_index`` 番目の ``- [ ] [AI確認]`` を ``- [x] [AI確認]`` に置換する."""
    lines = body.splitlines(keepends=True)
    if not 0 <= line_index < len(lines):
        return body
    original = lines[line_index]
    trailing = ""
    stripped = original
    if original.endswith("\n"):
        trailing = "\n"
        stripped = original[:-1]
    m = AI_CONFIRM_LINE_RE.match(stripped)
    if not m:
        return body
    new_line = f"{m.group(1)}x{m.group(3)}{trailing}"
    lines[line_index] = new_line
    return "".join(lines)


def verify_ai_confirm_items(pr_body: str, repo_root: Path) -> VerifyResult:  # noqa: ARG001
    """``[AI確認]`` 項目を検証する（session 外では skip 警告のみ返す）.

    Claude Code セッション外で呼ばれた場合（tidd_tools が subprocess として起動された
    場合を含む）は Agent tool を呼べないため、常に skip する。

    セッション内で呼ばれた場合も、本関数は subprocess 側で動く前提のため Agent tool を
    呼べない。実際の検証は ``/issue-next`` skill 側で Agent tool + ai-confirm-verifier
    subagent を起動して行う（本関数は呼ばれない設計）。

    Parameters
    ----------
    pr_body:
        PR ボディ全文。skip 時はこれをそのまま ``updated_body`` として返す。
    repo_root:
        リポジトリルート。将来 Agent tool 経由の検証結果を反映する際に使う想定
        （現状の実装では未使用）。

    Returns
    -------
    VerifyResult
        ``skipped=True`` で元 body をそのまま返す。stderr に skip 警告を出力する。
    """
    detector = _load_session_detector()
    in_session = bool(detector and detector.is_claude_code_session())

    if not in_session:
        sys.stderr.write(
            "ai-confirm skipped (outside Claude Code session): "
            "verify_ai_confirm は Claude Code セッション内でのみ動作します。"
            " /issue-next skill 経由で Agent tool から起動してください。\n"
        )
    else:
        sys.stderr.write(
            "ai-confirm skipped (subprocess cannot call Agent tool): "
            "tidd_tools は subprocess として起動されており Agent tool を呼べません。"
            " /issue-next skill 側で ai-confirm-verifier subagent を起動してください。\n"
        )
    return VerifyResult(updated_body=pr_body, skipped=True)
