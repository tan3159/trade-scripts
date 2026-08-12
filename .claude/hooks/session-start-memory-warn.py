#!/usr/bin/env python3
"""SessionStart hook: MEMORY.md の est tokens 超過を stdout に WARN する（Issue #3659）.

AutoMem のインデックス `~/.claude/projects/<repo 変換名>/memory/MEMORY.md` は常時ロード
コンテキストの最大ファイルになり得る（#3644 調査）。リポジトリ外のため既存
context-budget の PR gate が効かないため、本 hook が SessionStart で est tokens を
計測し、`.claude/rules/context-budget.yaml` の `memory_total_warn` 閾値超過を
stdout に WARN する（削除はしない。棚卸しは `tidd memory-triage` に委ねる）。

`session-start-cache.py` の `_run_health_check()` と同様に **ブロックしない・exit 0**。
機能キー `session-start-memory-warn` で on/off できる（default OFF・opt-in）。

stdlib のみ使用（uv run オーバーヘッドを避ける・detect-rule-bloat.py と同じ方針）。

テスト用環境変数:
  - MEMORY_WARN_MEMORY_DIR: メモリディレクトリを上書き（git root 解決をスキップ）
  - CONTEXT_BUDGET_YAML:    閾値 YAML パスを上書き（detect-rule-bloat.py と同一）
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import is_hook_enabled

_HOOK_NAME = "session-start-memory-warn"

# context_budget.py と同じ実トークナイザ近似係数（#3642 校正）。stdlib のみのため
# tidd_tools から import せず定数を二重管理する（detect-rule-bloat.py と同様。
# 変更時は両者を同期すること）。
_NON_ASCII_TOKENS_PER_CHAR = 0.923
_ASCII_TOKENS_PER_CHAR = 0.547

_CONTEXT_BUDGET_YAML_DEFAULT = (
    Path(__file__).resolve().parent.parent / "rules" / "context-budget.yaml"
)


def _estimate_tokens(text: str) -> float:
    """est_tokens = 0.923*非ASCII文字数 + 0.547*ASCII文字数（#3642 校正後）."""
    non_ascii = sum(1 for c in text if ord(c) >= 128)
    ascii_count = len(text) - non_ascii
    return _NON_ASCII_TOKENS_PER_CHAR * non_ascii + _ASCII_TOKENS_PER_CHAR * ascii_count


def _memory_dir(repo_root: Path) -> Path:
    """~/.claude/projects/<repo 変換名>/memory を導出する（context_usage と同式）.

    `context_usage.default_projects_dir()` の変換則（repo root の非英数字 → `-`）を
    stdlib のみで再現する。二重管理となるが tidd_tools 依存を hook に持ち込まない
    （consumer 環境の .venv に tidd_tools が無いため・#3087 と同様の理由）。
    """
    name = re.sub(r"[^A-Za-z0-9]", "-", str(repo_root))
    return Path.home() / ".claude" / "projects" / name / "memory"


def _parse_memory_total_warn(yaml_path: Path) -> int | None:
    """context-budget.yaml から memory_total_warn を読み取る（stdlib の簡易 YAML パーサ）."""
    if not yaml_path.is_file():
        return None
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if line.startswith("memory_total_warn:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _run_memory_warn() -> int:
    """MEMORY.md の est tokens 超過を stdout に WARN する（ブロックしない・exit 0）.

    MEMORY_WARN_MEMORY_DIR が設定されている場合は git root 解決をスキップして
    そのディレクトリを直接使う（テスト用）。git root を解決できない場合・
    MEMORY.md が存在しない場合・閾値未定義の場合は silent skip。
    """
    memory_dir_override = os.environ.get("MEMORY_WARN_MEMORY_DIR")
    if memory_dir_override:
        memory_dir = Path(memory_dir_override)
    else:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return 0
        if result.returncode != 0 or not result.stdout.strip():
            return 0
        memory_dir = _memory_dir(Path(result.stdout.strip()))

    memory_md = memory_dir / "MEMORY.md"
    if not memory_md.is_file():
        return 0

    try:
        text = memory_md.read_text(encoding="utf-8")
    except OSError:
        return 0
    est = _estimate_tokens(text)

    yaml_override = os.environ.get("CONTEXT_BUDGET_YAML")
    yaml_path = Path(yaml_override) if yaml_override else _CONTEXT_BUDGET_YAML_DEFAULT
    threshold = _parse_memory_total_warn(yaml_path)
    if threshold is None:
        return 0

    if est > threshold:
        sys.stdout.write(
            f"session-start-memory-warn: WARN: MEMORY.md が {est:.2f} est tokens"
            f"（memory_total_warn {threshold} 超過）。"
            " `tidd memory-triage` で棚卸し候補を確認してください。\n"
        )
    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled(_HOOK_NAME):
        return 0
    return _run_memory_warn()


if __name__ == "__main__":
    sys.exit(main())
