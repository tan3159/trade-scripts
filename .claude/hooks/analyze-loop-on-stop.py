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
  - projects/py/tidd_tools を含む本体リポジトリでは `uv run --project` を使い、
    consumer 環境では uvx ゼロインストール実行方式（`_lib/tidd_uvx.build_uvx_tidd_cmd`）
    にフォールバックする（Issue #3087）

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
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.git_helpers import git_toplevel
from _lib.hook_io import is_hook_enabled, read_stop_hook_input
from _lib.tidd_uvx import build_uvx_tidd_cmd

_NUM_RE = re.compile(r"^[1-9][0-9]*$")
_DIGITS_RE = re.compile(r"^[0-9]+$")


def main() -> int:
    # hook 機能別 on/off（Issue #2167）
    if not is_hook_enabled("analyze-loop-on-stop"):
        return 0

    # Issue #2957: stdin 読み取りを hook_io.read_stop_hook_input へ集約
    # （isatty 対応・Stop schema 不一致は WARN のみで exit しない）。
    # payload の内容自体は使わないため drain のみ。
    read_stop_hook_input()

    home = Path.home()
    last_run_file = Path(
        os.environ.get("LAST_RUN_FILE")
        or str(home / ".cache" / "loop-analysis-last-run")
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
        repo_root = git_toplevel(timeout=5)
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

    # uvx フォールバック経路（consumer 環境）を踏んだかどうかを覚えておく。
    # Scenario 2（#3087）の Then 句「標準エラーに "uvx" または "network" を含む
    # エラーメッセージを出力する」を通常 hook 実行パスでも満たすため、uvx 経路の
    # 場合は stderr を親プロセスへパススルーして失敗を可視化する必要がある。
    used_uvx_fallback = False

    if use_py_module and repo_root:
        # 上流リポジトリ本体（projects/py/tidd_tools が存在する）では引き続き
        # `uv run --project` を使い consumer 化しない（Issue #3087 やること4）。
        # 本体以外（copier 配布された consumer 環境）では uvx ゼロインストール実行方式に
        # フォールバックする（永続インストール状態を持たない）。
        tidd_tools_project = Path(repo_root) / "projects" / "py" / "tidd_tools"
        if tidd_tools_project.is_dir():
            cmd: list[str] = [
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
            uvx_cmd = build_uvx_tidd_cmd(
                "analyze-loop-errors", "--create-issues", "--days", str(interval_days)
            )
            if uvx_cmd is None:
                # uvx が入っていない consumer 環境ではスキップ（hook はセッションをブロックしない）
                return 0
            cmd = uvx_cmd
            used_uvx_fallback = True
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
            # 同期実行時（テスト用）は stderr を捕捉し失敗時に転送する。
            # consumer 環境で uvx 経由の tidd 解決がネットワーク到達不可等で失敗した場合、
            # 標準エラーにエラーメッセージを出力する（Issue #3087 Scenario 2）。
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                check=False,
                timeout=600,
                text=True,
            )
            if result.returncode != 0 and result.stderr:
                sys.stderr.write(result.stderr)
        elif used_uvx_fallback:
            # consumer 環境（uvx フォールバック経路）はネットワーク到達不可時の可視化が
            # マージゲート（Issue #3087 Scenario 2）となるため、バックグラウンド起動でも
            # stderr を親プロセスに継承させて失敗メッセージを流す。
            # stdout は静音化を維持（週次実行の成功出力でセッションを汚さない）。
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=None,  # 親プロセスの stderr を継承
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            # nohup 相当: 親と切り離してバックグラウンド起動（本体 uv run --project 経路）
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
