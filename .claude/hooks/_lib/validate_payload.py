"""Hook JSON payload の schema 検証（Issue #1292）.

Claude Code が hook に stdin で渡す JSON payload の必須フィールド検証を行う。
stdlib のみで実装（jsonschema など外部依存を持たない）。

**利用者:**

全 hook スクリプトが冒頭で呼び出す共通 validation 関数。

**設計判断:**

- 完全な JSON Schema 実装ではなく、``required`` フィールドと基本型（``type``）のチェックに限定
- 外部依存を避けて stdlib の json + pathlib のみ
- schema ファイルは ``_lib/schemas/{HookName}.json`` に配置
- 不一致時は例外 ``PayloadValidationError`` を送出し、呼び出し側で exit 2 に変換する
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class PayloadValidationError(ValueError):
    """schema 検証失敗を表す例外."""


_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"


def _load_schema(hook_name: str) -> dict[str, Any] | None:
    """hook_name.json を _lib/schemas/ から読み込む."""
    schema_path = _SCHEMAS_DIR / f"{hook_name}.json"
    if not schema_path.is_file():
        return None
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _check_type(value: Any, expected_type: str) -> bool:
    """JSON Schema 基本型を stdlib で確認する（object/array/string/boolean/number/integer）."""
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True  # 未知の型は通す（前方互換性）


def validate_payload(payload: dict[str, Any], hook_name: str) -> None:
    """payload を hook_name.json schema で検証する.

    Args:
        payload: hook が受け取った JSON payload
        hook_name: PreToolUse / PostToolUse / SessionStart / Stop / UserPromptSubmit

    Raises:
        PayloadValidationError: schema 不一致（required フィールド欠落・型不一致）
    """
    schema = _load_schema(hook_name)
    if schema is None:
        # schema 未配置は fail-open（前方互換性のため exit 2 にしない）
        return

    # top-level type チェック
    if schema.get("type") == "object" and not isinstance(payload, dict):
        raise PayloadValidationError(
            f"Hook payload schema validation failed: expected object, got {type(payload).__name__}"
        )

    # required フィールドチェック
    for req in schema.get("required", []) or []:
        if req not in payload:
            raise PayloadValidationError(
                f"Hook payload schema validation failed: '{req}' is required"
            )

    # 各プロパティの型チェック（存在する場合のみ）
    properties = schema.get("properties", {}) or {}
    for key, prop_schema in properties.items():
        if key not in payload:
            continue
        expected_type = prop_schema.get("type")
        if isinstance(expected_type, str) and not _check_type(payload[key], expected_type):
            raise PayloadValidationError(
                f"Hook payload schema validation failed: '{key}' expected type {expected_type}, "
                f"got {type(payload[key]).__name__}"
            )
