"""パフォーマンス計測結果の統一イベントログ保存（旧 ai-review.sh ``save_timing``）.

#2936 で旧 ``${STATE_DIR}/timing.json`` への書き込みを撤去し、統一イベントログ
（``timing_log`` の ``ai-review-verdict`` レコード）と verdict センチネルファイル
（``/issue-next`` の中断検知用・#1232）のみを記録する。
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from tidd_tools import timing_log

logger = logging.getLogger(__name__)


def save_timing(
    pr_num: str,
    verdict: str,
    backend_name: str,
    *,
    state_dir: Path | None = None,
    pytest_executed: bool | None = None,
    started_at: datetime | None = None,
) -> None:
    """verdict 確定時に統一イベントログへ記録する.

    旧 sh と同様、エラーは握りつぶしてレビューフローを阻害しない。
    verdict 単独センチネルファイル（#1232）と統一イベントログ
    （``ai-review-verdict``・#2934/#3126）へ記録する。旧 ``timing.json`` への
    書き込みは #2936 で撤去した。
    ``pytest_executed``（Issue #2636）: ai-review の test-plan ステップで pytest を
    実際に実行したか（True）、pre-flight キャッシュ hit によりスキップしたか（False）を
    記録する。None（未指定）のときはフィールドを省略する。
    ``started_at``（Issue #3553）: backend レビュー実行の開始時刻。渡された場合は
    ``meta.started_at`` に記録し、渡されなかった場合はフィールド自体を省略する
    （``merge_summary`` 側が ``step5-airview`` mark へフォールバックする既存経路を使う。
    ``ended_at`` は本関数呼び出し時点の時刻を常に記録する）。
    """
    env_state = os.environ.get("STATE_DIR")
    state = state_dir or (Path(env_state) if env_state else None)
    if state is None:
        return
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("state_dir mkdir 失敗: %s", exc)
        return
    # verdict 確定イベントの meta 用に本関数呼び出し終了時刻を記録する（Issue #3126）。
    # 開始時刻は呼び出し元がレビュー開始時点の値を渡す（#3553）。渡されなければ
    # started_at は meta に含めない（mark 側フォールバックの合図・merge_summary.py:554）。
    ended_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # verdict 単独センチネルファイルも書き込む（Issue #1232: /issue-next の中断検知が
    # timing.json 未整備の環境でも動くようにするため）
    try:
        (state / "verdict").write_text(f"{verdict}\n", encoding="utf-8")
    except OSError as exc:
        logger.debug("verdict ファイル書き込み失敗: %s", exc)
    # 統一イベントログに verdict 確定イベントを記録する（Issue #2934 やること2項目目）。
    # save_timing は --stop-before-merge / 通常経路 / --continue-with-verdict の全 5 呼び出し箇所が
    # 経由する choke point（#2924）のため、ここに追加するだけで全経路をカバーできる。
    verdict_meta: dict[str, object] = {
        "verdict": verdict,
        "backend": backend_name or "unknown",
        "pr_number": pr_num,
        "ended_at": ended_at,
    }
    if started_at is not None:
        verdict_meta["started_at"] = started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    if pytest_executed is not None:
        verdict_meta["pytest_executed"] = pytest_executed
    timing_log.record_event_safe(
        f"pr-{pr_num}",
        "ai-review-verdict",
        "point",
        "ai-review",
        meta=verdict_meta,
    )
