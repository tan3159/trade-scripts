"""parser critical PR 判定モジュール（Issue #3630）.

レビュー基盤自身（``tidd_tools/ai_review/``）や Issue バリデーション hook
（``.claude/hooks/validate-issue.py`` / ``.claude/hooks/require-issue.py``）を
変更する PR は、secondary consensus（``--stop-before-merge`` 経路・#2645）を
取らずに単一バックエンドの APPROVE だけで自動マージしてはならない。

本モジュールは `tidd ai-review` 起動時に PR の変更ファイル一覧から
parser critical 判定を行う純関数 ``is_parser_critical()`` と、
gate 本体が PR 情報を取得するための ``check_parser_critical_state()`` を提供する。
"""

from __future__ import annotations

from collections.abc import Iterable

from tidd_tools.shared.gh_client import pr_diff_files

#: 含む一致で判定する parser critical パス（サブディレクトリ含む）。
#: 実リポジトリの変更ファイルパスは `projects/py/tidd_tools/src/tidd_tools/ai_review/...`
#: のように repo root 相対で返るため、`tidd_tools/ai_review/` がパス内に含まれるかで判定する。
PARSER_CRITICAL_PREFIXES: tuple[str, ...] = ("tidd_tools/ai_review/",)

#: 完全一致で判定する parser critical パス。
PARSER_CRITICAL_EXACT_PATHS: frozenset[str] = frozenset(
    {
        ".claude/hooks/validate-issue.py",
        ".claude/hooks/require-issue.py",
    }
)

#: exit 6 中断時の stderr 案内メッセージ（--stop-before-merge 再実行を促す）。
PARSER_CRITICAL_GATE_MESSAGE = (
    "ERROR: parser critical PR です（レビュー基盤または Issue バリデーション hook を変更しています）。"
    "`tidd ai-review --stop-before-merge <PR番号>` で再実行してください。"
)


def is_parser_critical(changed_paths: Iterable[str]) -> bool:
    """変更ファイルパスの一覧が parser critical に該当するかを判定する.

    判定条件:
    - ``tidd_tools/ai_review/`` を含む（サブディレクトリ含む。repo root 相対パスで
      ``projects/py/tidd_tools/src/...`` のプレフィックスが付くため部分一致で判定）
    - ``.claude/hooks/validate-issue.py`` 完全一致
    - ``.claude/hooks/require-issue.py`` 完全一致

    Args:
        changed_paths: PR の変更ファイルパス一覧。

    Returns:
        いずれかに該当すれば True、それ以外は False。
    """
    for path in changed_paths:
        if path in PARSER_CRITICAL_EXACT_PATHS:
            return True
        if any(prefix in path for prefix in PARSER_CRITICAL_PREFIXES):
            return True
    return False


def check_parser_critical_state(pr_num: str, repo: str, token: str = "") -> bool:
    """PR の変更ファイル一覧を取得して parser critical かを判定する.

    ``gh_client.pr_diff_files`` は取得失敗時に空リストを返す fail-soft 契約のため、
    API 失敗時はフェイルセーフで False（gate をブロックしない）。

    Args:
        pr_num: PR 番号
        repo: リポジトリ (owner/name)
        token: GitHub トークン（省略可）

    Returns:
        parser critical に該当すれば True。
    """
    changed_paths = pr_diff_files(pr_num, repo, token=token or None)
    return is_parser_critical(changed_paths)
