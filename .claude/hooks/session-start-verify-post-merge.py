#!/usr/bin/env python3
"""SessionStart hook: 未消化 post-merge 検証を期限付きで現在のセッションへ渡す.

機能キー ``session-start-verify-post-merge`` は default OFF。前回の正常走査から
24 時間以上経過したときだけ、未消化 ``[AI確認-post-merge]`` 項目を持つマージ済み
PR を列挙し、stdout へ ``/verify-post-merge <PR>`` の実行指示を出す。

同じ間隔ゲートで ``tidd merge-summary sweep``（#3517）も additionally 起動する
（Issue #4158）。sweep 本体は既存だが唯一の起動経路が「別 PR の auto-merge 成功時に
scheduled される ``/verify-post-merge`` cron の STEP 0」に限られており、human-merge
経路（``needs-human-merge`` → 手動 ``gh pr merge``）が連続すると auto-merge 成功が
一度も起きずその cron 自体がスケジュールされないため、投稿漏れサマリの事後投稿機会が
失われ続けていた。SessionStart は auto-merge の成否と無関係に定期発火するため、ここから
additionally 起動することで欠落機会を埋める。

hook は候補検出・sweep 起動を担当するが、AI セッションや subagent を起動しない。
GitHub API エラー時は最終正常走査時刻を更新せず、WARN を出して常に exit 0 で終了する。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (
    find_repo_root_for_hooks,
    get_tidd_cache_dir,
    is_hook_enabled,
    read_hook_input,
)

_HOOK_NAME = "session-start-verify-post-merge"
_DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60
_MERGE_SUMMARY_SWEEP_TIMEOUT_SECONDS = 60
_PENDING_ITEM_RE = re.compile(
    r"^[ \t]*-[ \t]*\[[ \t]\][ \t]+\[AI確認-post-merge\]",
    re.MULTILINE,
)


def _last_scan_path() -> Path:
    override = os.environ.get("SESSION_START_VERIFY_POST_MERGE_LAST_SCAN_FILE")
    if override:
        return Path(override)
    return get_tidd_cache_dir() / _HOOK_NAME / "last-success"


def _read_last_success(path: Path) -> float:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return float(raw)
    except (OSError, ValueError):
        return 0.0


def _scan_is_due(path: Path, now: float) -> bool:
    return now - _read_last_success(path) >= _DEFAULT_INTERVAL_SECONDS


def _list_candidates() -> list[int] | None:
    """未消化項目を持つマージ済み PR 番号を返す。取得失敗時は None."""
    command = [
        "gh",
        "pr",
        "list",
        "--state",
        "merged",
        "--search",
        '"[AI確認-post-merge]" in:body',
        "--limit",
        "1000",
        "--json",
        "number,body,mergedAt",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data: Any = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None

    candidates: list[int] = []
    for pr in data:
        if not isinstance(pr, dict):
            continue
        number = pr.get("number")
        body = pr.get("body") or ""
        if (
            isinstance(number, int)
            and isinstance(body, str)
            and _PENDING_ITEM_RE.search(body)
        ):
            candidates.append(number)
    return candidates


def _merge_summary_sweep_command(repo_root: Path) -> list[str] | None:
    """`tidd merge-summary sweep` 実行コマンドを解決する（Issue #4158）.

    `uv run --project <repo>/projects/py/tidd_tools tidd` に一本化する
    （`sweep-merged-branches.py::_sweep_command` と同じ方針・#3979/#3984）。
    `uv` が PATH に無い・`projects/py/tidd_tools` が存在しない場合は None。
    """
    if shutil.which("uv") is None:
        return None
    tidd_project = repo_root / "projects" / "py" / "tidd_tools"
    if not tidd_project.is_dir():
        return None
    return [
        "uv",
        "run",
        "--project",
        str(tidd_project),
        "tidd",
        "merge-summary",
        "sweep",
    ]


def _run_merge_summary_sweep() -> None:
    """`tidd merge-summary sweep` を additionally 起動する（Issue #4158）.

    repo root・`projects/py/tidd_tools` が解決できない場合は無言で skip する
    （未 vendor の consumer 等）。失敗してもセッションはブロックしない
    （常に非ブロッキング・fail-open）。
    """
    repo_root = find_repo_root_for_hooks()
    if repo_root is None:
        return
    cmd = _merge_summary_sweep_command(repo_root)
    if cmd is None:
        return
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_MERGE_SUMMARY_SWEEP_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(
            f"{_HOOK_NAME}: WARN: merge-summary sweep 実行に失敗しました: {exc}\n"
        )
        return
    if result.returncode != 0:
        sys.stderr.write(
            f"{_HOOK_NAME}: WARN: merge-summary sweep が exit {result.returncode}（非ブロッキング・影響なし）\n"
        )


def _record_success(path: Path, now: float) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{now}\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _enabled() -> bool:
    """明示 OFF 時も無出力にするため、設定 helper の診断出力を局所的に抑止する."""
    with contextlib.redirect_stderr(io.StringIO()):
        return is_hook_enabled(_HOOK_NAME)


def main() -> int:
    if not _enabled():
        return 0

    read_hook_input(hook_name="SessionStart")
    now = time.time()
    last_scan = _last_scan_path()
    if not _scan_is_due(last_scan, now):
        return 0

    _run_merge_summary_sweep()

    candidates = _list_candidates()
    if candidates is None:
        sys.stderr.write(
            f"{_HOOK_NAME}: WARN: post-merge 候補の取得に失敗しました。"
            "最終正常走査時刻は更新せず、次回 SessionStart で再試行します。\n"
        )
        return 0

    if not _record_success(last_scan, now):
        sys.stderr.write(
            f"{_HOOK_NAME}: WARN: 最終正常走査時刻を保存できませんでした。"
            "次回 SessionStart で再走査します。\n"
        )

    for number in candidates:
        sys.stdout.write(
            f"{_HOOK_NAME}: 未消化 post-merge 項目を検出しました。"
            f"現在のセッションで /verify-post-merge {number} を実行してください。\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
