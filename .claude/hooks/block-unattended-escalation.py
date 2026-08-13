#!/usr/bin/env python3
"""PreToolUse hook: unattended モード中の AskUserQuestion をブロックする（Issue #3633）.

`issue-next-state init --unattended` で状態ファイル（`<root>/cache/issue-next-state/`
配下）に永続化された unattended モード実行中、人間へのエスカレーション
（AskUserQuestion）を呼ぶとセッションが待機して unattended 自走が止まるため、
PreToolUse で AskUserQuestion を exit 2 でブロックする。LLM は
`unattended-park-and-continue.md` の手順で park し、次 Issue へ継続する。

判定ロジック:
  - `tool_name` が `AskUserQuestion` のときのみ検査する（他の tool は素通し）
  - 状態ディレクトリ内の `issue-*.json` を走査し、いずれか 1 件でも
    `unattended: true` かつ liveness が TTL（`ISSUE_NEXT_LIVENESS_TTL_SECONDS`・
    デフォルト 1800 秒）内なら exit 2 でブロックする
  - フェイルセーフ: state 不在・JSON 破損・TTL 超過・`unattended` キー不在
    （旧形式・後方互換）はブロックしない
  - escape hatch: `SKIP_UNATTENDED_ESCALATION_GATE=1` で無効化できる

状態ディレクトリの解決は `tidd_tools.issue_next_state._state_dir()` と同方針
（`ISSUE_NEXT_STATE_ROOT` 環境変数 → プロセス CWD のリポジトリ root）。

stdlib のみ使用。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.git_helpers import git_toplevel
from _lib.hook_io import get_tool_name, is_hook_enabled, read_hook_input

DETAIL = "詳細: docs/reference/hooks.md#block-unattended-escalationpy\n"

DEFAULT_LIVENESS_TTL_SECONDS = (
    1800  # issue_next_state.DEFAULT_LIVENESS_TTL_SECONDS と同値
)
ESCAPE_HATCH_ENV = "SKIP_UNATTENDED_ESCALATION_GATE"


def _resolve_state_dir() -> Path | None:
    """状態ファイルを置く per-issue ディレクトリ（`<root>/cache/issue-next-state/`）を返す.

    `tidd_tools.issue_next_state._state_dir()` と同方針で解決する:
    - `ISSUE_NEXT_STATE_ROOT` 環境変数が設定されていれば `<root>/cache` を state ベースにする
    - 未設定ならプロセス CWD のリポジトリ root を使う
    解決できない場合は None（ブロックしない）。
    """
    root_override = os.environ.get("ISSUE_NEXT_STATE_ROOT")
    if root_override:
        root = Path(root_override)
    else:
        toplevel = git_toplevel()
        if not toplevel:
            return None
        root = Path(toplevel)
    return root / "cache" / "issue-next-state"


def _resolve_ttl() -> int:
    """`ISSUE_NEXT_LIVENESS_TTL_SECONDS` から TTL を取得する（非数値・負値はデフォルトへ）."""
    raw = os.environ.get("ISSUE_NEXT_LIVENESS_TTL_SECONDS", "")
    try:
        ttl = int(raw)
        if ttl <= 0:
            raise ValueError("TTL must be positive")
    except (ValueError, TypeError):
        ttl = DEFAULT_LIVENESS_TTL_SECONDS
    return ttl


def _parse_timestamp(raw: object) -> datetime | None:
    """ISO8601 文字列を aware datetime に変換する（不正値は None・naive は UTC 扱い）."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _latest_liveness(payload: dict) -> datetime | None:
    """`liveness_at` と `last_active` のうち新しい方の datetime を返す（#2819 と同型）."""
    candidates = []
    for raw in (payload.get("liveness_at"), payload.get("last_active")):
        parsed = _parse_timestamp(raw)
        if parsed is not None:
            candidates.append(parsed)
    return max(candidates) if candidates else None


def _is_active_unattended(payload: dict, ttl: int) -> bool:
    """1 件の payload が「TTL 内の unattended」か判定する."""
    if payload.get("unattended") is not True:
        return False
    latest = _latest_liveness(payload)
    if latest is None:
        return False  # liveness 解釈不能 → フェイルセーフで非アクティブ扱い
    now = datetime.now(UTC)
    try:
        return latest >= now - timedelta(seconds=ttl)
    except TypeError:
        return False  # 比較不能 → フェイルセーフで非アクティブ扱い


def _has_active_unattended_state(state_dir: Path, ttl: int) -> bool:
    """状態ディレクトリ内に「TTL 内の unattended」を持つ state が 1 件でもあるか判定する."""
    if not state_dir.is_dir():
        return False
    for state_file in sorted(state_dir.glob("issue-*.json")):
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # 破損・読み込み不可はスキップ（安全側）
        if not isinstance(payload, dict):
            continue
        if _is_active_unattended(payload, ttl):
            return True
    return False


def _block() -> int:
    sys.stderr.write(
        "BLOCK: unattended モード実行中です。"
        "`unattended-park-and-continue.md` の手順で park して次 Issue へ継続してください。"
        "（AskUserQuestion による人間待機は unattended 自走を止めます）\n"
    )
    sys.stderr.write(DETAIL)
    return 2


def _main() -> int:
    if os.environ.get(ESCAPE_HATCH_ENV) == "1":
        return 0  # escape hatch: 人間待機を明示的に許容する
    payload = read_hook_input(hook_name="PreToolUse")
    if get_tool_name(payload) != "AskUserQuestion":
        return 0  # AskUserQuestion 以外の tool は素通し

    state_dir = _resolve_state_dir()
    if state_dir is None:
        return 0  # リポジトリ外・状態ディレクトリ解決不能 → ブロックしない
    if _has_active_unattended_state(state_dir, _resolve_ttl()):
        return _block()
    return 0


def main() -> int:
    if not is_hook_enabled("block-unattended-escalation"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
