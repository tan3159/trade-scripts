#!/usr/bin/env python3
"""PreToolUse hook: .sh / .bats ファイルの新規追加をブロックする.

親 #1090（sh / bats 完全廃止プロジェクト）の Phase 1 完了後ガード（#1091）。

- 対象: Edit / Write ツールが扱う `*.sh` / `*.bats` ファイル
- 動作:
    - **既存 (git ls-files に存在)** の編集は通す（Python 化作業のため）
    - **新規** (git 管理下にない) の作成は exit 2 でブロック
- バイパス: 環境変数 `ALLOW_SH=1` が設定されているときは exit 0
- 実装方針: stdlib のみで起動を < 50ms に抑える（uv run は使わない・`tidd_tools` も import しない）
- worktree 対応 (#1054 で改修): `git ls-files` を対象ファイルの親ディレクトリで実行することで、
  複数 worktree をまたいで動作する hook が正しく既存ファイル判定できるようにする。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# stdlib のみ。argparse すら使わず stdin の JSON を読む。

EXTENSIONS_BLOCKED = (".sh", ".bats")
BYPASS_ENV = "ALLOW_SH"


def _read_input() -> dict:
    """Claude Code が hook に渡す JSON を stdin から読み取る（Issue #1364: schema 検証付き）."""
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    # Issue #1364: schema validation via _lib/validate_payload
    _lib_dir = Path(__file__).resolve().parent / "_lib"
    if str(_lib_dir) not in sys.path:
        sys.path.insert(0, str(_lib_dir))
    try:
        from validate_payload import (  # type: ignore[import-not-found]
            PayloadValidationError,
            validate_payload,
        )

        validate_payload(data, "PreToolUse")
    except ImportError:
        pass  # validate_payload absent → skip（前方互換）
    except PayloadValidationError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.exit(2)
    return data


def _git_toplevel(start_dir: Path | None = None) -> Path | None:
    """`git rev-parse --show-toplevel` の Path を返す。取得失敗時は None.

    `start_dir` が指定されたときはそのディレクトリを cwd にして git rev-parse を実行する。
    worktree が複数ある場合に「対象ファイル所在の worktree のルート」を取得する用途で使う。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start_dir) if start_dir else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return Path(out) if out else None


def _is_tracked_in_git(path: Path) -> bool:
    """`git ls-files --error-unmatch <basename>` で git 管理下か判定する.

    対象ファイルの親ディレクトリを `cwd` にして git ls-files を実行する。これにより
    複数 worktree がある場合でも、対象ファイル所在の worktree に対して問い合わせできる
    （#1091 後の改修・#1054 で追加）。

    `path` の親ディレクトリが存在しない場合は **`False`（未追跡扱い）** を返してブロック側に
    倒す（安全側）。
    """
    target_dir = path.parent if path.is_absolute() else Path.cwd()
    if not target_dir.exists():
        return False
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path.name],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _resolve_file_path(raw: str, repo_root: Path) -> Path:
    """tool_input.file_path が相対パスでも repo_root 起点に解決する."""
    p = Path(raw)
    if not p.is_absolute():
        p = (repo_root / p).resolve()
    return p


def _main() -> int:
    if os.environ.get(BYPASS_ENV) == "1":
        # 安全弁。バイパスを使った事実は stderr に残す。
        sys.stderr.write(
            f"ban-shell-files: {BYPASS_ENV}=1 によりバイパスされました。\n"
        )
        return 0

    payload = _read_input()
    tool_name = str(payload.get("tool_name", ""))
    if tool_name not in {"Edit", "Write"}:
        return 0

    tool_input = payload.get("tool_input", {}) or {}
    raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
    if not raw_path:
        return 0

    suffix = Path(raw_path).suffix.lower()
    if suffix not in EXTENSIONS_BLOCKED:
        return 0

    repo_root = _git_toplevel()
    if repo_root is None:
        # git 外なら判定不能 → 通す（誤検知を避ける）
        return 0

    target = _resolve_file_path(raw_path, repo_root)
    if _is_tracked_in_git(target):
        # 既存ファイルの編集 / 削除前のコミット作業は許可する。
        return 0

    sys.stderr.write(
        "sh / bats ファイルの新規追加は禁止です。Python (.py) で実装してください。\n"
        f"対象: {raw_path}\n"
        "親 #1090（sh / bats 完全廃止プロジェクト）の方針です。\n"
        "やむを得ない一時バイパス: 環境変数 ALLOW_SH=1 を設定して再実行してください\n"
        "（CI ゲート `uv run python -m tidd_tools check-no-shell-files` でも検知されます）。\n"
        "詳細: docs/reference/hooks.md#ban-shell-filespy\n"
    )
    return 2


def main() -> int:
    # Issue #1633: hook 機能別 on/off（_lib を明示的に sys.path へ追加）
    _lib_dir = Path(__file__).resolve().parent / "_lib"
    if str(_lib_dir) not in sys.path:
        sys.path.insert(0, str(_lib_dir))
    try:
        from hook_io import is_hook_enabled as _is_hook_enabled  # type: ignore[import-not-found]

        if not _is_hook_enabled("ban-shell-files"):
            return 0
    except ImportError:
        pass  # hook_io が存在しない場合は無視（前方互換）

    return _main()


if __name__ == "__main__":
    sys.exit(main())
