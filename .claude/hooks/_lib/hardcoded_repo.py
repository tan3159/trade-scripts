"""リポジトリ名ハードコード検出の共通ロジック（Issue #336・#868・#1057・#2944）.

`tidd_tools.lint_hardcoded_repo`（`tidd lint-hardcoded-repo`）と
`.claude/hooks/ban-hardcoded-repo.py`（ステージング済みファイルの commit 前チェック）
の重複ロジックを共通化する。

DRY 原則: ハードコード検出パターン・除外ファイル・除外パスのリスト・
`.claude/rules/hardcoded-patterns.yaml` 追加パターンの読み込みロジックを
単一の真実源としてこのモジュールに置く。両エントリポイントはここを参照する
（Issue #2944: `tidd lint-hardcoded-repo` は動的 import でこのモジュールを直接参照し、
独自の重複した除外リストを再定義しない）。

stdlib のみ使用（hook 起動オーバヘッド最小化のため）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

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
    # #3683: 配布物（notify-copier-staleness.py / tidd_uvx.py / hook_io.py /
    # block-unauthorized-fallback-review.py / state_dir.py / validate-skill.py /
    # notify-template-sync.py / hardcoded-patterns.yaml）は上流固有文字列を
    # `_src_path`（`.copier-answers.yml`）へ局所化したため、検出対象に戻っている。
    # 固有文字列を再追加するとブロックされる（意図的な保護）。
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
    return bool(_HINT_LINE_RE.match(line))


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


def load_yaml_patterns(git_root: str) -> list[dict[str, str]]:
    """`.claude/rules/hardcoded-patterns.yaml` から追加パターンリストを読み込む（Issue #1642）.

    PyYAML を使わず、手書きの YAML パーサーを使う（stdlib のみ）。
    YAML は `git_root`（リポジトリルート）の `.claude/rules/hardcoded-patterns.yaml` を参照する。
    ファイルが存在しない場合は空リストを返す（後方互換・yaml 未設定環境対応）。

    フォーマット:
        patterns:
          - id: <str>
            regex: '<str>'
            description: '<str>'
            message: '<str>'

    Returns:
        パターン dict のリスト（regex キーを持つ）。読み込み失敗・ファイル不在は空リスト。
    """
    yaml_path = Path(git_root) / ".claude" / "rules" / "hardcoded-patterns.yaml"
    if not yaml_path.is_file():
        return []
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return []

    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        # コメント行・空行はスキップ
        if stripped.startswith("#") or not stripped:
            continue
        # 新しいエントリの開始
        if stripped.startswith("- id:"):
            if current:
                entries.append(current)
            current = {"id": stripped[len("- id:") :].strip()}
        elif ":" in stripped and current:
            key, _, val = stripped.partition(":")
            key = key.strip().lstrip("- ")
            val = val.strip().strip("'\"")
            if key and val:
                current[key] = val
    if current:
        entries.append(current)

    # 'regex' キーを持つエントリのみ返す（patterns: [] 等のダミー行を除外）
    return [e for e in entries if "regex" in e]


def compile_yaml_patterns(
    yaml_patterns: list[dict[str, str]],
) -> list[tuple[re.Pattern[str], str, str]]:
    """YAML パターンを事前コンパイルし、不正 regex は WARN + skip して返す."""
    compiled_yaml: list[tuple[re.Pattern[str], str, str]] = []
    for entry in yaml_patterns:
        raw_regex = entry.get("regex", "")
        message = entry.get(
            "message", f"個人値パターン '{raw_regex}' がハードコードされています"
        )
        try:
            compiled = re.compile(raw_regex)
        except re.error:
            sys.stderr.write(
                f"WARN: hardcoded-patterns.yaml の regex が不正です: {raw_regex}\n"
            )
            continue
        compiled_yaml.append((compiled, message, raw_regex))
    return compiled_yaml


def find_yaml_pattern_match(
    line: str,
    compiled_yaml: list[tuple[re.Pattern[str], str, str]],
) -> tuple[str, str] | None:
    """行が YAML 追加パターンにマッチするか判定する。なければ None.

    除外行（コメント・HINT）は自動的にスキップする。

    Returns:
        (message, raw_regex) のタプル。マッチしなければ None。
    """
    if is_excluded_line(line):
        return None
    for compiled, message, raw_regex in compiled_yaml:
        if compiled.search(line):
            return message, raw_regex
    return None
