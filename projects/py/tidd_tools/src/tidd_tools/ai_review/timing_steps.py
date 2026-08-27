"""ai-review ステップ別 timing 計測（Issue #2100）.

ai-review の各ステップ（commit status check・PR context 収集・backend 選択・
backend subprocess 実行・verdict 抽出・yaru evidence tick・Issue 消化 gate・
auto-merge）の開始・終了時刻を統一イベントログ（``tidd_tools.timing_log``）へ
記録する。

``~/.config/tidd_tools/config.json`` の ``"ai-review-timing"`` が
``true`` のときのみ記録する（デフォルト off・常時 on によるオーバーヘッド・
ログ肥大化を避けるため）。``tidd configure --set ai-review-timing=on/off`` で
切り替える。

#2936 で旧 ``~/.cache/ai-review-timing/<PR番号>.jsonl`` への書き込みを撤去した。
#3340 で ``mark_boundary``（旧 ``ai-review-timing/<key>.jsonl`` 書き込み）も撤去した。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

from tidd_tools import timing_log

_CONFIG_KEY = "ai-review-timing"


def _hooks_config_path(env: dict[str, str]) -> str | None:
    """env dict の HOME / XDG_CONFIG_HOME / APPDATA から config.json パスを解決する.

    ``yaru_auto_tick._hooks_config_path_from_env`` と同じ解決規則だが、
    渡された env dict のみを参照する（テストの決定性維持）。
    """
    if sys.platform == "win32":
        appdata = env.get("APPDATA") or ""
        if appdata:
            return os.path.join(appdata, "tidd_tools", "config.json")
    else:
        xdg = env.get("XDG_CONFIG_HOME") or ""
        if xdg:
            return os.path.join(xdg, "tidd_tools", "config.json")
    home = env.get("HOME") or ""
    if home:
        return os.path.join(home, ".config", "tidd_tools", "config.json")
    return None


def is_enabled(env: dict[str, str] | None = None) -> bool:
    """config.json の ``"ai-review-timing"`` 値を返す（デフォルト off）.

    設定ファイルなし・キーなし・不正 JSON・非 bool 値は全て False（安全側 default）。
    """
    e = env if env is not None else dict(os.environ)
    path = _hooks_config_path(e)
    if path is None:
        return False
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(config, dict):
        return False
    value = config.get(_CONFIG_KEY)
    return value is True


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# pre_flight.py は PR 作成前に呼ばれるため、この prefix には ``pr_num`` として
# ブランチ名（PR 番号ではない）を渡す（"統一日誌・ブランチ名キー"・Issue #2101）。
# ``preflight.*`` 系 step は merge_summary.py の ``f"pr-{pr_num}"`` 検索対象ではなく
# `ai_review_timing_report.py --report` の横断集計（`read_all_events()`）でのみ
# 参照されるため、ここでは prefix を付けずブランチ名キーのまま記録する（Issue #3620）。
_NO_PR_PREFIX_STEP_PREFIX = "preflight."


def append_step_timing(pr_num: str, step: str, started_at: str, ended_at: str) -> None:
    """1 ステップ分の timing レコードを統一イベントログへ追記する.

    ``measure_step`` の唯一の書き込み先関数。書き込み失敗はレビューフローを
    阻害しないよう握りつぶす（best-effort・#3126）。

    PR 単位の step（``test-plan-gate``・``yaru-evidence-tick`` 等）は
    ``pr_num`` に ``pr-`` prefix を付けたキーで書き込む（``merge_summary.py`` の
    ``load_test_plan_phase_event()``・``load_yaru_phase_event()`` 等の読み取り側が
    ``f"pr-{pr_num}"`` 形式でキーを検索するため・Issue #3620）。
    ``preflight.*`` step（PR 作成前に実行され ``pr_num`` にブランチ名が渡される）は
    prefix を付けない（上記 ``_NO_PR_PREFIX_STEP_PREFIX`` 参照）。
    Issue 番号への解決は行わない（レビュー本流に ``gh`` 呼び出しを持ち込まないため）。

    #2936 で旧 ``~/.cache/ai-review-timing/<PR番号>.jsonl`` への書き込みを撤去した。
    """
    key = pr_num if step.startswith(_NO_PR_PREFIX_STEP_PREFIX) else f"pr-{pr_num}"
    timing_log.record_event_safe(
        key,
        step,
        "span",
        "ai-review-timing",
        meta={"started_at": started_at, "ended_at": ended_at},
    )


@contextlib.contextmanager
def measure_step(pr_num: str | None, step: str, *, enabled: bool | None = None) -> Iterator[None]:
    """``with`` ブロックの実行時間を計測し ``ai-review-timing`` が on のとき記録する.

    off（デフォルト）のときは時刻取得・ファイル I/O を一切行わずそのまま実行する。
    ``pr_num`` が ``None``（識別子未確定・Issue #2101: pre-flight でブランチ名を
    解決できない場合等）のときも同様に記録をスキップする。
    ``PYTEST_CURRENT_TEST`` 環境変数が設定されているとき（pytest 実行中）は
    書き込みをスキップして本番キャッシュへの汚染を防ぐ（Issue #2185）。
    ``measure_step`` の書き込み動作そのものをテストしたい場合は、テスト内で
    ``monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)`` を呼んでバイパスすること。
    """
    if pr_num is None:
        yield
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        yield
        return
    is_on = is_enabled() if enabled is None else enabled
    if not is_on:
        yield
        return
    started_at = _now_iso()
    try:
        yield
    finally:
        ended_at = _now_iso()
        append_step_timing(pr_num, step, started_at, ended_at)
