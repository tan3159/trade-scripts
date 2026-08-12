#!/usr/bin/env python3
"""PostToolUse hook: Write / Edit / MultiEdit 後に diff 累積行数を計測して閾値横断時に警告する.

実装中の編集のたびに `git diff --numstat origin/main` で累積 diff 行数を計測し、
500 行・1000 行の閾値を初めて横断したタイミングで stderr に警告を 1 回出力する。
exit code は常に 0（ブロックしない）。強制ブロックは #3081（pre-flight）の責務。

閾値横断後の dedup 状態は `.tidd/state/diff-size-warned.json` で管理する:
  {"warned": [500]}  # 500 行閾値の警告済み
diff が閾値未満に戻ったら該当閾値の記録をクリアし、再横断時に再警告する。

stdlib のみ使用。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.git_helpers import git_toplevel as _git_toplevel
from _lib.hook_io import (
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)
from _lib.session_detector import is_claude_code_session

# lock ファイル（#3081 と同一の除外パターン）
_LOCK_FILE_RE = re.compile(r"(^|/)(uv\.lock|package-lock\.json|[^/]+\.lock)$")

_THRESHOLD_500 = 500
_THRESHOLD_1000 = 1000

_STATE_REL_PATH = ".tidd/state/diff-size-warned.json"


def _git_diff_numstat_total(repo_root: str) -> int | None:
    """`git diff --numstat origin/main` の additions+deletions 合計を返す.

    バイナリファイルと lock ファイルはカウントから除外する（#3081 と同じ数え方）。
    git 実行失敗・タイムアウトの場合は None を返す（fail-open のため）。
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--numstat", "origin/main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None

    total = 0
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        additions_str, deletions_str, path = parts
        if additions_str == "-" or deletions_str == "-":
            continue  # バイナリファイル
        if _LOCK_FILE_RE.search(path):
            continue  # lock ファイル
        try:
            total += int(additions_str) + int(deletions_str)
        except ValueError:
            continue
    return total


def _read_warned_state(state_file: Path) -> list[int]:
    """`.tidd/state/diff-size-warned.json` から警告済み閾値リストを読む.

    ファイルが存在しない・読み取り失敗の場合は空リストを返す（fail-open）。
    """
    if not state_file.is_file():
        return []
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    warned = data.get("warned", [])
    if not isinstance(warned, list):
        return []
    return [x for x in warned if isinstance(x, int)]


def _write_warned_state(state_file: Path, warned: list[int]) -> None:
    """`.tidd/state/diff-size-warned.json` に警告済み閾値リストを書き込む.

    書き込み失敗は無視する（fail-open・本体の編集作業を一切妨げない）。
    """
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps({"warned": sorted(warned)}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _main() -> int:
    payload = read_hook_input(hook_name="PostToolUse")

    tool_name = get_tool_name(payload)
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return 0

    # Claude Code セッション外では no-op
    if not is_claude_code_session():
        return 0

    # git リポジトリルートを解決する（fail-open: 失敗時は exit 0）
    git_root = _git_toplevel()
    if not git_root:
        return 0

    repo_root_path = Path(git_root)
    state_file = repo_root_path / _STATE_REL_PATH

    # diff 行数を計測する
    diff_lines = _git_diff_numstat_total(git_root)
    if diff_lines is None:
        return 0  # git 失敗 → fail-open

    warned = _read_warned_state(state_file)

    # diff が閾値未満に戻ったとき、対応する警告済み記録をクリアする（再横断で再警告）
    updated_warned = [t for t in warned if diff_lines >= t]
    if updated_warned != warned:
        _write_warned_state(state_file, updated_warned)
    warned = updated_warned

    # 1000 行閾値のチェック（先に判定することで 1 回の実行で両方を踏んだときも正しく動く）
    if diff_lines > _THRESHOLD_1000 and _THRESHOLD_1000 not in warned:
        print(
            f"WARN: warn-diff-size: diff が {diff_lines} 行に達しました（{_THRESHOLD_1000} 行超）。"
            " pre-flight でブロックされます。PR を分割してください。"
            " 詳細: docs/reference/pr-splitting-guide.md"
            " escape hatch: <!-- allow-large-pr: <理由> -->",
            file=sys.stderr,
        )
        # 1000 行閾値と同時に 500 行閾値も横断済みのため、500 も一緒に記録する
        # （#3140: 500 が未記録のまま残ると次回編集で 500 行警告が遅延出力される）
        warned = sorted({*warned, _THRESHOLD_500, _THRESHOLD_1000})
        _write_warned_state(state_file, warned)
        return 0

    # 500 行閾値のチェック
    if diff_lines > _THRESHOLD_500 and _THRESHOLD_500 not in warned:
        print(
            f"WARN: warn-diff-size: diff が {diff_lines} 行に達しました（{_THRESHOLD_500} 行超）。"
            " 分割検討ラインに達しました。詳細: docs/reference/pr-splitting-guide.md",
            file=sys.stderr,
        )
        warned = sorted({*warned, _THRESHOLD_500})
        _write_warned_state(state_file, warned)

    return 0


def main() -> int:
    if not is_hook_enabled("warn-diff-size"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
