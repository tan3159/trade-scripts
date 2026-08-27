"""テストランナー出力の quiet capture ユーティリティ（Issue #3426）.

`tidd test-plan` は jest / pytest / bats の実行時に子プロセスの出力を
`stdout=sys.stderr` でそのまま素通ししていたため、成功時でも数百〜数千行の
実行ログが Claude Code のコンテキストに入りトークンを浪費していた。

本モジュールはランナーごとの出力をログファイルへキャプチャし、既定（quiet）では
成功時に 1 行サマリのみ、失敗時は末尾 `TAIL_LINES` 行 + ログファイルパスのみを
stderr へ出力する。LLM の判断に必要な「成功/失敗 + 失敗時のエラー詳細」は保持しつつ、
トークン消費を抑える。

escape hatch: 環境変数 ``TIDD_TEST_OUTPUT_VERBOSE=1`` で従来どおりの素通し動作に戻せる。
ログの保存先は既定 ``~/.cache/tidd-test-logs`` で、テスト分離のため
``TIDD_TEST_LOG_DIR`` で上書きできる。
"""

from __future__ import annotations

import itertools
import os
import re
import sys
from datetime import datetime
from pathlib import Path

TAIL_LINES = 50

# pytest の終端サマリ行（例: "==== 62 passed in 12.34s ===="）から件数・所要秒の
# テキスト部分だけを抜き出す。
_PYTEST_SUMMARY_RE = re.compile(r"={3,}\s*(.+?passed.*?)\s*={3,}")
# jest の "Tests:       12 passed, 12 total" 行。
_JEST_TESTS_LINE_RE = re.compile(r"^Tests:\s*(.+)$", re.MULTILINE)
# bats --tap の "ok <n> ..." 行数を件数として数える。
_BATS_OK_RE = re.compile(r"^ok \d+", re.MULTILINE)

_seq_counter = itertools.count()


def verbose_enabled() -> bool:
    """``TIDD_TEST_OUTPUT_VERBOSE=1`` が設定されていれば True を返す（Issue #3426）."""
    return os.environ.get("TIDD_TEST_OUTPUT_VERBOSE") == "1"


def log_dir() -> Path:
    """テストランナー出力ログの保存先ディレクトリを返す（既定 ``~/.cache/tidd-test-logs``）.

    テスト分離のため ``TIDD_TEST_LOG_DIR`` で上書きできる。
    """
    override = os.environ.get("TIDD_TEST_LOG_DIR")
    return Path(override) if override else Path.home() / ".cache" / "tidd-test-logs"


def reserve_log_path(runner: str) -> Path:
    """``<timestamp>-<seq>-<runner>.log`` のログファイルパスを生成しディレクトリを作成する.

    マイクロ秒精度のタイムスタンプに加えプロセス内カウンタを付与し、同一秒内に
    複数ランナー（bats の複数ファイル実行等）が呼び出されてもファイル名が衝突しない
    ようにする。
    """
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    seq = next(_seq_counter)
    return directory / f"{ts}-{seq}-{runner}.log"


def _summarize(runner: str, output: str, elapsed: float) -> str:
    """成功時の 1 行サマリを組み立てる（既知フォーマットを抽出できなければ汎用文言）."""
    if runner == "pytest":
        match = _PYTEST_SUMMARY_RE.search(output)
        if match:
            return f"==> pytest: {match.group(1)}"
    elif runner == "jest":
        match = _JEST_TESTS_LINE_RE.search(output)
        if match:
            return f"==> jest: {match.group(1).strip()}（{elapsed:.1f}s）"
    elif runner == "bats":
        passed = len(_BATS_OK_RE.findall(output))
        return f"==> bats: {passed} passed（{elapsed:.1f}s）"
    return f"==> {runner}: passed（{elapsed:.1f}s）"


def emit_result(*, runner: str, log_path: Path, returncode: int, elapsed: float) -> None:
    """ログファイルの内容から成功時サマリ or 失敗時 tail を stderr に出力する（Issue #3426）.

    ``returncode`` が 0 のときのみ成功として 1 行サマリを表示する。timeout 経路など
    非 0 を明示的に渡すケースは失敗表示（末尾 ``TAIL_LINES`` 行 + ログパス）に統一する。
    """
    try:
        output = log_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        output = ""
    if returncode == 0:
        print(_summarize(runner, output, elapsed), file=sys.stderr)
        return
    lines = output.splitlines()
    tail = lines[-TAIL_LINES:]
    if tail:
        print("\n".join(tail), file=sys.stderr)
    print(f"==> {runner} 全出力ログ: {log_path}", file=sys.stderr)
