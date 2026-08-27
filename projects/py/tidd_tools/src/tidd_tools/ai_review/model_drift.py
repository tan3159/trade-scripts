"""agy invalid model selection ドリフト検知 + 自動 Issue 起票（Issue #2380）.

``backends.py`` の ``_try_agy_step`` は agy 呼び出しの非ゼロ終了を「次バックエンドへ
フォールバック」として静かに処理するため、``agy_model`` のハードコード値（例:
``"Claude Sonnet 4.6 (Thinking)"``）が agy CLI 側のモデルカタログ改定で無効化されても
人間が気づく手段がない。

本モジュールは ``quota.py`` の cooldown キャッシュパターン（``is_quota_skip_active`` /
``record_quota_exceeded``）を踏襲し、agy 出力に ``Error: invalid model selection``
パターンが含まれる場合に検知し、``gh issue create`` を直接 subprocess 実行して
GitHub Issue を自動起票する。24時間以内の同一モデル名の再検知は重複起票しない。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.gh_client import issue_create
from tidd_tools.shared.paths import cache_dir as _cache_dir

logger = logging.getLogger(__name__)

MODEL_DRIFT_CACHE_FILENAME = "model-drift.json"
DEFAULT_MODEL_DRIFT_CACHE_PATH = _cache_dir() / "ai-reviewer" / MODEL_DRIFT_CACHE_FILENAME
MODEL_DRIFT_WINDOW_SECONDS = 24 * 60 * 60  # 24 時間

MODEL_DRIFT_PATTERN = re.compile(r'Error:\s*invalid model selection \(--model "', re.IGNORECASE)
_MODEL_NAME_PATTERN = re.compile(r'--model "([^"]*)"')

ISSUE_LABELS = ["type: fix", "priority: high", "source: ci"]


def is_model_drift_error(output: str) -> bool:
    """agy 出力（stdout+stderr）に invalid model selection エラーが含まれるか."""
    if not output:
        return False
    return bool(MODEL_DRIFT_PATTERN.search(output))


def _extract_model_name(output: str) -> str:
    """エラー文字列（``--model "<model>"``）からモデル名を抽出する. 抽出できなければ ``"unknown"``."""
    match = _MODEL_NAME_PATTERN.search(output)
    return match.group(1) if match else "unknown"


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


def record_model_drift(
    model_name: str,
    *,
    cache_path: Path | None = None,
    now: datetime | None = None,
) -> None:
    """指定モデル名の drift 検知時刻を記録する."""
    cache = cache_path or DEFAULT_MODEL_DRIFT_CACHE_PATH
    ts = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = _load(cache)
    payload[model_name] = ts
    _save(cache, payload)
    logger.info("model drift cache recorded: %s @ %s", model_name, ts)


def is_model_drift_skip_active(
    model_name: str,
    *,
    cache_path: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """指定モデル名が 24 時間以内に drift 検知・起票済みであれば True."""
    cache = cache_path or DEFAULT_MODEL_DRIFT_CACHE_PATH
    payload = _load(cache)
    recorded = payload.get(model_name)
    if not recorded or not isinstance(recorded, str):
        return False
    try:
        normalized = recorded.replace("Z", "+00:00")
        recorded_dt = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    if recorded_dt.tzinfo is None:
        recorded_dt = recorded_dt.replace(tzinfo=UTC)
    now_dt = now or datetime.now(UTC)
    elapsed = (now_dt - recorded_dt).total_seconds()
    return elapsed < MODEL_DRIFT_WINDOW_SECONDS


def _issue_body(*, model_name: str, error_output: str, pr_num: str) -> str:
    return f"""## 背景

agy レビュー実行時に、モデル `{model_name}` が agy CLI に「known model として認識されない」
エラー（invalid model selection）を返しました。PR #{pr_num} の `tidd ai-review` 実行中に
検知されました。

`backends.py` の `_try_agy_step` はこの失敗を非ゼロ終了として次バックエンドへ静かに
フォールバックするため、モデル指定が無効化されても人間が気づく手段がありませんでした。
本 Issue は `tidd_tools.ai_review.model_drift` が `run_agy_review` の出力を正規表現で
検知し自動起票したものです（Issue #2380）。

### agy エラー全文

```
{error_output}
```

## やること

- [ ] `projects/py/tidd_tools/src/tidd_tools/ai_review/backends.py` の該当モデル指定
      （`agy_model` ハードコード箇所）を agy の最新モデルカタログに合わせて更新する
- [ ] エラーメッセージの `Available models` 一覧、または `agy --help` で正しいモデル名を確認する

## 振る舞い

```gherkin
Feature: agy モデル指定の修正

  Scenario: 修正後に agy レビューが成功する
    Given backends.py のモデル指定が agy の最新カタログに合わせて修正されている
    When PR #{pr_num} 相当の agy レビューを再実行する
    Then agy が exit code 0 でレビュー結果を返す
```

---
*この Issue は agy invalid model selection 検知（`tidd_tools.ai_review.model_drift`）によって自動作成されました。*"""


def detect_and_report_model_drift(
    output: str,
    pr_num: str,
    repo: str,
    *,
    cache_path: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """agy 出力を検査し、invalid model selection 検知時に Issue を自動起票する.

    Returns:
        True — Issue を新規起票した
        False — drift 未検知、または 24時間以内の重複検知でスキップした（起票失敗時も False）
    """
    if not is_model_drift_error(output):
        return False

    model_name = _extract_model_name(output)
    if is_model_drift_skip_active(model_name, cache_path=cache_path, now=now):
        print(
            f"==> agy invalid model selection を検知しましたが、モデル `{model_name}` は24時間以内に"
            "起票済みのため重複起票をスキップします。",
            file=sys.stderr,
        )
        return False

    title = f"fix: agy モデル `{model_name}` が invalid model selection エラーを返す（自動検知）"
    body = _issue_body(model_name=model_name, error_output=output, pr_num=pr_num)
    try:
        issue_create(title, body, ISSUE_LABELS, repo)
    except GhCommandError as exc:
        print(f"WARN: model drift Issue の起票に失敗しました: {exc}", file=sys.stderr)
        return False

    record_model_drift(model_name, cache_path=cache_path, now=now)
    print(
        f"==> agy invalid model selection を検知し Issue を起票しました（モデル: {model_name}）。",
        file=sys.stderr,
    )
    return True
