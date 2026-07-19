"""リポジトリ名ハードコード検出の共通ロジック（Issue #336・#868・#1057）.

`scripts/lint-hardcoded-repo.sh`（プロジェクト全体スキャン）と
`.claude/hooks/ban-hardcoded-repo.py`（ステージング済みファイルの commit 前チェック）
の重複ロジックを共通化する。

DRY 原則: ハードコード検出パターン・除外ファイル・除外パスのリストを単一の真実源として
このモジュールに置く。両エントリポイントはここを参照する。

stdlib のみ使用（hook 起動オーバヘッド最小化のため）。
"""

from __future__ import annotations

import re

# ハードコード検出パターン（リポジトリ固有文字列）
PATTERNS: tuple[str, ...] = (
    "being-gaia-plan",
    "ai-dev-handbook",
)

# 除外ファイル（basename ベース・意図的にパターンを含むファイル）
EXCLUDE_BASENAMES: tuple[str, ...] = (
    "lint-hardcoded-repo.sh",
    "lint_hardcoded_repo.py",
    "ban-hardcoded-repo.sh",
    "ban-hardcoded-repo.py",
    "hardcoded_repo.py",  # この共通モジュール自身
    "bootstrap.sh",
    "ai-review-repo-config.sh",
    "ai-review-repo-config.toml",
    # #1224: Copier template staleness hook（consumer に配布される）は
    # 上流 ai-dev-handbook の URL を default 値として保持する必要がある。
    # `TIDD_COPIER_UPSTREAM_URL` で override 可能（bootstrap.sh と同じ扱い）。
    "notify-copier-staleness.py",
    # #2221: 配布物自身（templates/workflow/ 経由で consumer に配布される）が
    # docstring / description 文言でリポジトリ固有文字列を含むため、consumer の
    # 初回コミットが自己検出でブロックされる。以下の 3 ファイルは意図的に
    # 「ai-dev-handbook」文字列を保持する（コメント外・非除外行のため
    # コメント行スキップ (`is_excluded_line`) では吸収できない）。
    "validate-skill.py",
    "notify-template-sync.py",
    "hardcoded-patterns.yaml",
)

# 除外パス（フルパス・相対パスのいずれにも対応するため部分一致で判定）
EXCLUDE_PATH_FRAGMENTS: tuple[str, ...] = (
    "/tests/",
    "/docs/",
    # #2221: __pycache__/ 配下のバイトコードは配布・commit 対象ではなく、
    # 稀に文字列化した際に検出パターン (being-gaia-plan / ai-dev-handbook)
    # を偶然含みうるため走査から除外する。
    "__pycache__/",
)

# 除外拡張子（basename の末尾一致で判定）
EXCLUDE_SUFFIXES: tuple[str, ...] = (
    # #2221: Python バイトコード（.pyc / .pyo）は走査対象外。
    ".pyc",
    ".pyo",
)


# コメント行の判定（先頭が # で始まる）
_COMMENT_LINE_RE = re.compile(r"^\s*#")
# echo/printf の HINT 行の判定（説明文中の例示）
_HINT_LINE_RE = re.compile(r"^\s*(echo|printf)\s.*HINT")


def is_excluded_basename(basename: str) -> bool:
    """basename が除外対象か判定する."""
    return basename in EXCLUDE_BASENAMES


def is_excluded_path(path: str) -> bool:
    """パス文字列が除外対象か判定する.

    以下のいずれかを満たす場合に True を返す:
    - `.md` で終わる（Markdown はドキュメント）
    - `EXCLUDE_PATH_FRAGMENTS` のいずれかを部分文字列として含む
      （`/tests/`, `/docs/`, `__pycache__/`）
    - `EXCLUDE_SUFFIXES` のいずれかで終わる（`.pyc`, `.pyo`）
    """
    if path.endswith(".md"):
        return True
    for fragment in EXCLUDE_PATH_FRAGMENTS:
        if fragment in path:
            return True
    for suffix in EXCLUDE_SUFFIXES:
        if path.endswith(suffix):
            return True
    return False


def is_excluded_line(line: str) -> bool:
    """行内容が除外対象（コメント行 or HINT 例示行）か判定する."""
    if _COMMENT_LINE_RE.match(line):
        return True
    if _HINT_LINE_RE.match(line):
        return True
    return False


def line_contains_pattern(line: str) -> str | None:
    """行に含まれるハードコードパターンを返す。なければ None.

    除外行（コメント・HINT）は自動的にスキップする。
    """
    if is_excluded_line(line):
        return None
    for pattern in PATTERNS:
        if pattern in line:
            return pattern
    return None
