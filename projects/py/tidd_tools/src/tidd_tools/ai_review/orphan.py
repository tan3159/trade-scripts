"""AI review 中断検知（Issue #1232）.

`/issue-next` が「PR 既存スキップ」を行う際に、AI review のキャッシュを見て
「前セッションの ai-review が verdict を書く前に死んでいないか」を判定する。

キャッシュディレクトリの状態は以下 3 パターンに分類する:

- ``NO_CACHE``      cache dir が存在しない or 空 → 通常の初回レビューフロー
- ``INTERRUPTED``   test-plan-status 等はあるが verdict の観測手段が無い
                    → 中断された前回セッションのゴミが残っている。同 PR で
                    ``tidd ai-review <N> <試行回数>`` を同期再実行して回収する
- ``COMPLETED``     ``verdict`` センチネルファイル or ``timing.json`` に verdict が記録済み
                    → 前回レビューは投稿まで完了している。現行の verdict ハンドリングに従う

verdict の観測は以下 2 経路で行う（どちらか一方でも見つかれば COMPLETED）:

1. ``verdict`` センチネルファイルの存在（``save_timing`` が付随書き込みする）
2. ``timing.json`` (JSON Lines) のいずれかの行に ``"verdict"`` フィールドあり

この関数は副作用を持たない純関数として設計し pytest しやすくする。
実際の再実行判断・コマンド起動は ``/issue-next`` スキル側が担う。
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path


class OrphanReviewState(Enum):
    """AI review キャッシュの状態."""

    NO_CACHE = "no_cache"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"


def _timing_has_verdict(timing_path: Path) -> bool:
    """timing.json (JSON Lines) のいずれかの行に verdict エントリが含まれるか判定する.

    壊れた JSON 行は無視して他の行を評価する。1 行も verdict を含む有効な JSON が
    見つからなければ False を返す。
    """
    try:
        text = timing_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and "verdict" in record:
            return True
    return False


def _unified_has_verdict(pr_num: str) -> bool:
    """統一イベントログに `ai-review-verdict` イベントがあるか判定する（#3295）."""
    from tidd_tools import timing_log

    try:
        events = timing_log.read_events(f"pr-{pr_num}")
    except (OSError, ValueError):
        return False
    return any(event.get("step") == "ai-review-verdict" for event in events)


def detect_orphan_review(state_dir: Path) -> OrphanReviewState:
    """AI review キャッシュディレクトリを見て中断状態を判定する.

    Args:
        state_dir: ``cache_dir() / "ai-reviewer" / f"pr-{N}"`` を想定した
            キャッシュディレクトリのパス。存在しなくてよい。

    Returns:
        - ``NO_CACHE``     cache dir が存在しない or 中身が空
        - ``INTERRUPTED``  何らかのキャッシュファイルはあるが verdict が観測できない
        - ``COMPLETED``    verdict センチネルファイル or timing.json に verdict あり
    """
    if not state_dir.is_dir():
        return OrphanReviewState.NO_CACHE
    # ディレクトリはあっても中身が空なら NO_CACHE として扱う（開始前 mkdir 相当）
    try:
        entries = list(state_dir.iterdir())
    except OSError:
        return OrphanReviewState.NO_CACHE
    if not entries:
        return OrphanReviewState.NO_CACHE

    # verdict センチネルファイルを優先（save_timing が書き込む）
    verdict_path = state_dir / "verdict"
    if verdict_path.is_file():
        try:
            content = verdict_path.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        if content:
            return OrphanReviewState.COMPLETED

    # #3295: 統一イベントログの verdict イベント（save_timing 二重書き込み）を参照
    if state_dir.name.startswith("pr-"):
        pr_key = state_dir.name[len("pr-") :]
        if _unified_has_verdict(pr_key):
            return OrphanReviewState.COMPLETED

    # timing.json の JSON Lines を走査するフォールバック（移行前履歴）
    timing_path = state_dir / "timing.json"
    if timing_path.is_file() and _timing_has_verdict(timing_path):
        return OrphanReviewState.COMPLETED
    return OrphanReviewState.INTERRUPTED
