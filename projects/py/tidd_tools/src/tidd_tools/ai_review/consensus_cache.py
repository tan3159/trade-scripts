"""ai-review commit SHA 不変時キャッシュ（Issue #2081）.

``~/.cache/ai-dev-handbook/ai-reviewer/pr-<N>/consensus.json`` に直前 APPROVE 実行時の
commit SHA・verdict を保存する。次回実行時に現在の HEAD SHA が一致し verdict が
APPROVE なら、pytest・ruff・mypy・backend レビュー呼び出しをスキップして
Issue やること gate のみ再評価できるようにする。

PR #2080 の実例: Issue やること未消化チェックボックスを直しただけ（コード差分なし・
commit SHA 不変）にもかかわらず ai-review を最初から全再実行する必要があった。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

CONSENSUS_FILENAME = "consensus.json"


def read_consensus(state_dir: Path) -> dict[str, str] | None:
    """``consensus.json`` を読み込む. 存在しない・壊れている場合は None を返す."""
    path = state_dir / CONSENSUS_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.debug("consensus.json の解析に失敗: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_consensus(state_dir: Path, sha: str, verdict: str) -> None:
    """直前実行の commit SHA・verdict を ``consensus.json`` に保存する.

    旧 timing.json 等と同様、書き込み失敗はレビューフローを阻害しないよう握りつぶす。
    """
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("state_dir mkdir 失敗: %s", exc)
        return
    record = {
        "sha": sha,
        "verdict": verdict,
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        (state_dir / CONSENSUS_FILENAME).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.debug("consensus.json 書き込み失敗: %s", exc)


def delete_consensus(state_dir: Path) -> None:
    """``consensus.json`` を削除してキャッシュを無効化する.

    commit status POST 失敗などによるデッドロック状態（Issue #2131）を解除するために使う。
    ファイルが存在しない・削除に失敗してもエラーを送出しない。
    """
    path = state_dir / CONSENSUS_FILENAME
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("consensus.json 削除失敗: %s", exc)


def is_cache_hit(state_dir: Path, head_sha: str) -> bool:
    """直前実行が同一 commit SHA で APPROVE 済みなら True を返す.

    ``head_sha`` が空文字（SHA 取得失敗）の場合は安全側で常に False（フル実行）とする。
    """
    if not head_sha:
        return False
    cached = read_consensus(state_dir)
    if not cached:
        return False
    return cached.get("verdict") == "APPROVE" and cached.get("sha") == head_sha
