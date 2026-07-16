#!/usr/bin/env python3
"""Stop hook: 週次で analyze-loop-errors を自動実行する.

旧 analyze-loop-on-stop.sh を 1:1 で踏襲する（Phase 4 / #1057 で Python 化）。
Issue #827・#1055。

動作:
  - 前回実行から N 日（デフォルト 7）以上経過し、かつ ~/.cache/loop-error-log/ に
    *.jsonl ファイルが存在する場合のみ `tidd analyze-loop-errors --create-issues`
    をバックグラウンド実行する
  - 実行後は ~/.cache/loop-analysis-last-run のタイムスタンプを更新する
  - hook 自体は常に exit 0（セッション終了をブロックしない）

環境変数:
  ANALYZE_SCRIPT       テスト用スタブ実行可能パス（未設定時は uv 経由で
                       python -m tidd_tools analyze-loop-errors を呼ぶ）
  LAST_RUN_FILE        前回実行タイムスタンプの保存先（デフォルト ~/.cache/loop-analysis-last-run）
  LOG_DIR              ログディレクトリ（デフォルト ~/.cache/loop-error-log）
  LOOP_ANALYSIS_INTERVAL_DAYS  実行間隔（デフォルト 7 日）
  LOOP_ANALYSIS_FOREGROUND     1 なら同期実行（テスト用）

stdlib のみ使用。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import is_hook_enabled  # noqa: E402


def _read_stdin_drain() -> None:
    """stdin から payload を読み Stop schema で validate する（Issue #1364）.

    従来 1 行だけ読み捨てていたが、payload 全体を読んで schema mismatch を検出できるようにした。
    副作用処理を伴う Stop hook のため、schema mismatch は WARN log 化して exit 2 は避ける。
    """
    if sys.stdin.isatty():
        return
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return
    raw = raw.strip()
    if not raw:
        return
    try:
        import json as _json

        data = _json.loads(raw)
    except _json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    # Issue #1364: Stop schema で validate（不一致は WARN log のみ）
    _lib_dir = Path(__file__).resolve().parent / "_lib"
    if str(_lib_dir) not in sys.path:
        sys.path.insert(0, str(_lib_dir))
    try:
        from validate_payload import (  # type: ignore[import-not-found]
            PayloadValidationError,
            validate_payload,
        )

        validate_payload(data, "Stop")
    except ImportError:
        pass
    except PayloadValidationError as exc:
        sys.stderr.write(f"analyze-loop-on-stop.py: WARN: Stop schema mismatch: {exc}\n")


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
    out = result.stdout.strip()
    return out or None


_NUM_RE = re.compile(r"^[1-9][0-9]*$")
_DIGITS_RE = re.compile(r"^[0-9]+$")


def main() -> int:
    # hook 機能別 on/off（Issue #2167）
    if not is_hook_enabled("analyze-loop-on-stop"):
        return 0

    _read_stdin_drain()

    home = Path.home()
    last_run_file = Path(
        os.environ.get("LAST_RUN_FILE") or str(home / ".cache" / "loop-analysis-last-run")
    )
    log_dir = Path(os.environ.get("LOG_DIR") or str(home / ".cache" / "loop-error-log"))

    interval_raw = os.environ.get("LOOP_ANALYSIS_INTERVAL_DAYS", "7")
    interval_days = int(interval_raw) if _NUM_RE.match(interval_raw) else 7

    analyze_script = os.environ.get("ANALYZE_SCRIPT", "").strip()
    use_py_module = False
    repo_root: str | None = None
    if analyze_script:
        use_py_module = False
    else:
        repo_root = _git_toplevel()
        if not repo_root:
            return 0
        use_py_module = True

    if not log_dir.is_dir():
        return 0

    # *.jsonl が 1 件以上あるか
    try:
        log_count = sum(1 for _ in log_dir.glob("*.jsonl"))
    except OSError:
        log_count = 0
    if log_count == 0:
        return 0

    now = int(time.time())

    last_run = 0
    if last_run_file.is_file():
        try:
            raw = last_run_file.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        if _DIGITS_RE.match(raw):
            last_run = int(raw)

    interval_seconds = interval_days * 86400
    if (now - last_run) < interval_seconds:
        return 0

    # ANALYZE_SCRIPT 指定時はファイル存在確認
    if not use_py_module:
        script_path = Path(analyze_script)
        if not (script_path.exists()):
            return 0

    # 二重実行防止のため先にタイムスタンプを更新
    try:
        last_run_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        last_run_file.write_text(f"{now}\n", encoding="utf-8")
    except OSError:
        pass

    if use_py_module and repo_root:
        # entry point 経由（Phase 5 の tidd_tools git+https 配布で `tidd` が PATH に入る consumer 用）
        # を優先し、無ければ ai-dev-handbook 本体で使う `uv run --project` にフォールバックする。
        tidd_bin = shutil.which("tidd")
        tidd_tools_project = Path(repo_root) / "projects" / "py" / "tidd_tools"
        if tidd_bin:
            cmd: list[str] = [
                tidd_bin,
                "analyze-loop-errors",
                "--create-issues",
                "--days",
                str(interval_days),
            ]
        elif tidd_tools_project.is_dir():
            cmd = [
                "uv",
                "run",
                "--project",
                str(tidd_tools_project),
                "python",
                "-m",
                "tidd_tools",
                "analyze-loop-errors",
                "--create-issues",
                "--days",
                str(interval_days),
            ]
        else:
            # tidd が入っていない consumer 環境ではスキップ（hook はセッションをブロックしない）
            return 0
    else:
        cmd = [
            "bash",
            analyze_script,
            "--create-issues",
            "--days",
            str(interval_days),
        ]

    foreground = os.environ.get("LOOP_ANALYSIS_FOREGROUND", "0") == "1"
    try:
        if foreground:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                check=False,
                timeout=600,
            )
        else:
            # nohup 相当: 親と切り離してバックグラウンド起動
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
