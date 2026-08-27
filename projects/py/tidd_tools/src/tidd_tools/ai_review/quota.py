"""クォータ超過グローバルキャッシュ（旧 ai-review.sh ``_record_quota_exceeded`` / ``_is_quota_skip_active``）.

``~/.cache/ai-reviewer/quota-exceeded.json`` に各バックエンドが直近にクォータ超過した
タイムスタンプを保存し、リセット時刻まで次回のフォールバックチェーンでスキップする。

JSON フォーマット（Issue #2745: リセット時間対応後）:
    新形式（reset_seconds あり）:
        {"agy-gemini": {"timestamp": "2026-06-30T12:00:00Z", "reset_seconds": 172800}, "agy-sonnet": null}
    旧形式（後方互換）:
        {"agy-gemini": "2026-06-30T12:00:00Z", "agy-sonnet": null}

Issue #2745: Gemini クォータのリセット時間が固定の QUOTA_WINDOW_SECONDS（24時間）より
長い場合（最大 48時間超）、スキップ解除後に再試行してもまだクォータ枯渇しており
「agy-empty-output:agy-gemini」ループエラーが繰り返し発生していた。

``extract_reset_seconds()`` でクォータエラーメッセージの "Resets in XhYmZs." を解析し、
``record_quota_exceeded()`` に ``reset_seconds`` を渡すことで実際のリセット時刻まで
正確にスキップする。パターン解析失敗時は従来の ``QUOTA_WINDOW_SECONDS``（24時間）を維持。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tidd_tools.shared.paths import cache_dir as _cache_dir

logger = logging.getLogger(__name__)

QUOTA_CACHE_FILENAME = "quota-exceeded.json"
DEFAULT_QUOTA_CACHE_PATH = _cache_dir() / "ai-reviewer" / QUOTA_CACHE_FILENAME
QUOTA_WINDOW_SECONDS = 24 * 60 * 60  # 24 時間（fallback）

QUOTA_PATTERN = re.compile(r"individual quota reached|quota limit exceeded", re.IGNORECASE)

# Issue #2745: "Resets in 20h33m18s." パターンを解析するための正規表現
_RESET_PATTERN = re.compile(r"Resets in (\d+)h(\d+)m(\d+)s", re.IGNORECASE)


def is_quota_exceeded(output: str) -> bool:
    """agy / codex 出力にクォータ超過メッセージが含まれるか."""
    if not output:
        return False
    return bool(QUOTA_PATTERN.search(output))


def extract_reset_seconds(output: str) -> int | None:
    """agy クォータエラーメッセージから リセット時間（秒）を抽出する（Issue #2745）.

    "Resets in XhYmZs." パターンを解析して秒数に変換する。
    パターンが見つからない場合は ``None`` を返す（呼び出し元は QUOTA_WINDOW_SECONDS を使う）。

    Examples::

        >>> extract_reset_seconds("Individual quota reached. Resets in 20h33m18s.")
        74798
        >>> extract_reset_seconds("Some other error.")
        None
    """
    if not output:
        return None
    m = _RESET_PATTERN.search(output)
    if not m:
        return None
    hours, minutes, seconds = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return hours * 3600 + minutes * 60 + seconds


def _load(cache_path: Path) -> dict[str, Any]:
    if not cache_path.is_file():
        return {}
    try:
        data: Any = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save(cache_path: Path, payload: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def record_quota_exceeded(
    backend_key: str,
    *,
    cache_path: Path | None = None,
    now: datetime | None = None,
    reset_seconds: int | None = None,
) -> None:
    """指定バックエンドのクォータ超過時刻を記録する.

    Issue #2745: ``reset_seconds`` を指定することでリセット時刻を正確に記録できる。
    未指定の場合は従来通り ``is_quota_skip_active`` が ``QUOTA_WINDOW_SECONDS``（24時間）を使う。
    """
    cache = cache_path or DEFAULT_QUOTA_CACHE_PATH
    ts = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = _load(cache)
    payload.setdefault("agy-gemini", None)
    payload.setdefault("agy-sonnet", None)
    if backend_key in {"agy-gemini", "agy-sonnet"}:
        if reset_seconds is not None:
            # 新形式: タイムスタンプとリセット時間を辞書で保存
            payload[backend_key] = {"timestamp": ts, "reset_seconds": reset_seconds}
        else:
            # 旧形式: タイムスタンプのみ（後方互換）
            payload[backend_key] = ts
    _save(cache, payload)
    logger.info("quota cache recorded: %s @ %s (reset_seconds=%s)", backend_key, ts, reset_seconds)


def _parse_cached_entry(recorded: Any) -> tuple[str | None, int]:
    """キャッシュエントリを解析して（タイムスタンプ文字列、スキップウィンドウ秒数）を返す.

    新形式（辞書）と旧形式（文字列）の両方に対応する（後方互換）。

    Returns:
        (timestamp_str, window_seconds) のタプル。
        解析失敗時は (None, QUOTA_WINDOW_SECONDS) を返す。
    """
    if isinstance(recorded, str):
        # 旧形式: 文字列タイムスタンプ → デフォルトの 24時間スキップ
        return recorded, QUOTA_WINDOW_SECONDS
    if isinstance(recorded, dict):
        ts = recorded.get("timestamp")
        reset_secs = recorded.get("reset_seconds")
        if isinstance(ts, str) and isinstance(reset_secs, int) and reset_secs > 0:
            return ts, reset_secs
        if isinstance(ts, str):
            return ts, QUOTA_WINDOW_SECONDS
    return None, QUOTA_WINDOW_SECONDS


def is_quota_skip_active(
    backend_key: str,
    *,
    cache_path: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """指定バックエンドのクォータスキップが有効かどうかを返す.

    Issue #2745: ``reset_seconds`` が記録されている場合はその時間でスキップを判定する。
    旧形式（文字列タイムスタンプ）の場合は従来通り ``QUOTA_WINDOW_SECONDS``（24時間）を使う。
    """
    cache = cache_path or DEFAULT_QUOTA_CACHE_PATH
    payload = _load(cache)
    recorded = payload.get(backend_key)
    if not recorded:
        return False

    ts_str, window_seconds = _parse_cached_entry(recorded)
    if ts_str is None:
        return False

    try:
        # "YYYY-MM-DDTHH:MM:SSZ" フォーマット（fromisoformat の Z 対応）
        normalized = ts_str.replace("Z", "+00:00")
        recorded_dt = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    if recorded_dt.tzinfo is None:
        recorded_dt = recorded_dt.replace(tzinfo=UTC)
    now_dt = now or datetime.now(UTC)
    elapsed = (now_dt - recorded_dt).total_seconds()
    return elapsed < window_seconds
