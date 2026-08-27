"""廃止済み env var がドキュメント・テンプレートに残っていないか走査する（Issue #2534）.

走査対象は「利用者向けドキュメント」に絞る:
  - README.md（リポジトリ直下）
  - docs/setup/
  - templates/workflow/docs/setup/
  - .mise.toml.example
  - templates/workflow/.mise.toml.example.jinja

設定指示パターン（export VARNAME= や VARNAME= の値代入）のみを検出対象とする。
「SECRETS_BACKEND は廃止済み」のような参照言及は正当なコンテキストなので除外する。

除外ロジックの根拠（PR 本文への 1 行説明として記録）:
  - docs/decisions/ と docs/research/ は廃止経緯の記録・調査メモを含むため対象外。
  - 設定指示パターン（export VARNAME= / VARNAME=value）を検出することで
    「廃止されていると言及する文」を誤検知しない。

終了コード:
  0 → 検出なし
  1 → 廃止済み設定指示を 1 件以上検出
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

from tidd_tools.deprecated_config_registry import DEPRECATED_ENV_VARS

# 走査対象ディレクトリ・ファイル（repo_root からの相対パス）
# consumer 向けドキュメントのみ対象とし、docs/decisions/ 等の経緯文書は除外する。
SCAN_TARGET_DIRS: tuple[str, ...] = (
    "docs/setup",
    "templates/workflow/docs/setup",
)

SCAN_TARGET_FILES: tuple[str, ...] = (
    "README.md",
    ".mise.toml.example",
    "templates/workflow/.mise.toml.example.jinja",
)

# 走査除外ファイル（相対パス）
# 移行ガイドや廃止経緯を記録した文書には「旧 env var の設定例」が
# 正当な言及として含まれるため除外する。
# 除外判断の根拠: これらの文書は「廃止済みであることを説明する」用途であり、
# consumer に廃止済み env var の設定を指示しているのではない。
SCAN_EXCLUDED_FILES: frozenset[str] = frozenset(
    {
        # 移行ガイド：旧 env var 名と移行先の対応表を含む（正当な言及）
        "docs/setup/ai-review-backend-migration.md",
        # secretsmanagement.md の「移行手順」セクションは廃止済み env var を
        # 削除する方法を説明するため、SECRETS_BACKEND への言及が正当に存在する。
        # ただし「export SECRETS_BACKEND=」の設定指示は存在してはならないため対象から外さない。
    }
)


# 廃止済み env var の「設定を指示するパターン」
#
# 対象（設定指示）:
# - export VARNAME=  （bash/zsh export 構文・コメント行も含む）
# - ^VARNAME=value  （行頭からの bash assignment）
# - VARNAME = "value"  （.mise.toml [env] 形式）
# - $env:VARNAME =  （PowerShell 構文）
# - "VARNAME": "value"  （YAML/TOML/JSON での設定例）
# - $env:VARNAME = （PowerShell 代入）
#
# 非対象（説明・言及）:
# - 「VARNAME=bw のときのみ」「（VARNAME=bw）」などの括弧内・文中言及
#   → 行頭ではないため `^VARNAME=` にマッチしない
# - `SECRETS_BACKEND は廃止済み` などの言及文
#   → = がないためマッチしない
def _build_pattern(var_name: str) -> re.Pattern[str]:
    """変数名にマッチする「設定指示パターン」の正規表現を返す."""
    escaped = re.escape(var_name)
    return re.compile(
        r"(?:"
        r"export\s+" + escaped + r"\s*="  # export VARNAME= （コメント行も含む設定例）
        r"|(?:^|\s)" + escaped + r"\s*=\s*['\"]"  # VARNAME="value" または VARNAME = "value"
        r"|"
        r"\$env:" + escaped + r"\s*="  # $env:VARNAME =
        r"|"
        r'"' + escaped + r'"\s*:'  # "VARNAME": （YAML/JSON）
        r")",
        re.MULTILINE,
    )


class DeprecatedSettingMatch(NamedTuple):
    """走査で検出した廃止済み設定の一致情報."""

    file_path: Path
    """マッチしたファイルの絶対パス."""

    line_number: int
    """1-indexed 行番号."""

    line_content: str
    """該当行の内容（strip 済み）."""

    var_name: str
    """検出された廃止済み env var 名."""


def scan_deprecated_settings(repo_root: Path) -> list[DeprecatedSettingMatch]:
    """利用者向けドキュメント内の廃止済み env var 設定指示を走査する.

    Args:
        repo_root: リポジトリのルートディレクトリ。

    Returns:
        検出した一致情報のリスト。空リストなら残存なし。

    """
    patterns: dict[str, re.Pattern[str]] = {var_name: _build_pattern(var_name) for var_name in DEPRECATED_ENV_VARS}

    target_files: list[Path] = []

    # 対象ファイル（直接指定）
    for rel_file in SCAN_TARGET_FILES:
        candidate = repo_root / rel_file
        if candidate.is_file():
            target_files.append(candidate)

    # 対象ディレクトリ（再帰的に全 .md / .jinja を収集）
    for rel_dir in SCAN_TARGET_DIRS:
        scan_dir = repo_root / rel_dir
        if scan_dir.is_dir():
            for ext in ("*.md", "*.jinja"):
                target_files.extend(scan_dir.rglob(ext))

    matches: list[DeprecatedSettingMatch] = []

    for file_path in sorted(set(target_files)):
        # 除外ファイルをスキップ
        try:
            rel_str = str(file_path.relative_to(repo_root))
        except ValueError:
            rel_str = str(file_path)
        if rel_str in SCAN_EXCLUDED_FILES:
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for lineno, line in enumerate(content.splitlines(), start=1):
            for var_name, pattern in patterns.items():
                if pattern.search(line):
                    matches.append(
                        DeprecatedSettingMatch(
                            file_path=file_path,
                            line_number=lineno,
                            line_content=line.strip(),
                            var_name=var_name,
                        )
                    )

    return matches
