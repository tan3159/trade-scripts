#!/usr/bin/env python3
"""PostToolUse hook: Write / Edit / MultiEdit 後に ruff format を自動適用する.

対象: projects/py/tidd_tools/ 配下の .py ファイルのみ。
その他のファイル（docs/ 等）は対象外として即 exit 0 する。

ruff は `git_root/.venv/bin/ruff format <file>` で直接実行する（Issue #1890）。
workspace venv（git_root/.venv）に ruff がインストール済みであればネットワーク不要で最速。
`.venv/bin/ruff` が存在しない場合は WARN + exit 0 でスキップする（ネットワーク依存を排除）。
旧実装（Issue #1403）は `uv run --with ruff` を使っていたが、ネットワーク断で ruff の取得に
失敗し silent 失敗するケースがあった。
hook 失敗（venv 不在等）はコミットをブロックせず stderr 警告のみ出す（exit 0）。
`require-ruff-format.py` が PR 作成前の最終ゲートとして未整形を検知する。

stdlib のみ使用。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (  # noqa: E402
    get_file_path,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)

# ruff format の対象ディレクトリ（この配下の .py のみ）
_TARGET_SUBDIR = "projects/py/tidd_tools/"


def _git_toplevel() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _main() -> int:
    payload = read_hook_input(hook_name="PostToolUse")  # Issue #1364

    tool_name = get_tool_name(payload)
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return 0

    file_path = get_file_path(payload)
    if not file_path:
        return 0

    # .py ファイルのみ対象
    if not file_path.endswith(".py"):
        return 0

    git_root = _git_toplevel()
    if not git_root:
        return 0

    # projects/py/tidd_tools/ 配下のみ対象（Path.is_relative_to で厳密に判定）
    target_dir = Path(git_root) / _TARGET_SUBDIR
    abs_file_path = Path(file_path).resolve()
    try:
        abs_file_path.relative_to(target_dir)
    except ValueError:
        return 0
    abs_file = str(abs_file_path)

    # ファイルが存在しない場合（Write で新規作成前など）はスキップ
    if not Path(abs_file).exists():
        return 0

    # Issue #1890: ruff を git_root/.venv/bin/ruff で直接実行してネットワーク不要にする。
    # `uv run --with ruff` / `uv run --project ... --extra dev` はどちらも
    # cold cache + ネットワーク断で ruff 取得に失敗し silent 失敗する（根本原因）。
    # `.venv/bin/ruff` は workspace venv（`uv sync` 後）にインストール済みのため
    # ネットワーク不要で最速。不在の場合は WARN + exit 0 でスキップし、
    # `require-ruff-format.py` の PR 作成前ゲートに委ねる。
    venv_ruff = Path(git_root) / ".venv" / "bin" / "ruff"
    if not venv_ruff.is_file():
        print(
            "WARN: auto-ruff-format: .venv/bin/ruff が見つかりません。"
            " `uv sync --all-extras` を実行して workspace venv を作成してください。"
            " docs/reference/hooks.md#auto-ruff-formatpy 参照",
            file=sys.stderr,
        )
        return 0

    try:
        result = subprocess.run(
            [str(venv_ruff), "format", abs_file],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(
            f"WARN: auto-ruff-format: ruff の実行に失敗しました: {e}."
            " docs/reference/hooks.md#auto-ruff-formatpy 参照",
            file=sys.stderr,
        )
        return 0

    if result.returncode != 0:
        print(
            "WARN: auto-ruff-format: ruff format が失敗しました。"
            " auto-ruff-format hook が silent 失敗している可能性 →"
            " docs/reference/hooks.md#auto-ruff-formatpy 参照\n"
            f"{result.stderr}",
            file=sys.stderr,
        )
    else:
        # Issue #1752 副次改善: 実行できたことを stderr に 1 行残す（silent skip 事故の観測性改善）。
        # 実際に ruff が起動したかどうかを常に log で追えるようにする（fail-loud はしない）。
        print(f"auto-ruff-format: formatted {abs_file}", file=sys.stderr)

    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("auto-ruff-format"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
