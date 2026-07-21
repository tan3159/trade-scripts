#!/usr/bin/env python3
"""Stop hook: `/issue-next <N>`（引数ありモード）の途中終了を機械的に防ぐ (Issue #2321)。

**背景:** `/issue-next <N>` は STEP 0（品質チェック）後に turn が終了しても機械的な
継続保証がなかった。既存の `on-stop.py` 孤児検出（Issue #1698）は `current_issue` に
対応する worktree・open PR の有無で判定するため、worktree 作成前（STEP 0 〜 STEP 1 の
間）の離脱を検出できない。本 hook は `cache/issue-next-state.json` の `current_issue`
が設定されている「間」を単純にブロック対象とすることでこのギャップを埋める。

**動作:**

1. `cache/issue-next-state.json` を読む。ファイル無し・JSON 不正・`current_issue` が
   null の場合は何もしない（exit 0）。
2. `current_issue` が設定されていても、`last_active`（無ければ `started_at`）から
   `REQUIRE_ISSUE_NEXT_COMPLETION_TTL_SECONDS`（デフォルト 7200 秒 = 2 時間）以上
   経過していればブロックしない（フェイルセーフ。state 消し忘れによる永久ブロック防止）。
3. それ以外（in-progress かつ TTL 内）は **exit 2** で stop をブロックし、stderr に
   継続指示を注入する（Claude Code の Stop hook exit 2 は stderr をコンテキストに
   注入する仕様を利用する）。

`hooks-config.json` で default OFF（`is_hook_enabled()` の非安全系 hook デフォルト）。
stdlib のみ使用。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import is_hook_enabled  # noqa: E402

_DEFAULT_TTL_SECONDS = 7200  # 2 時間
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _drain_stdin() -> str:
    if sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.readline()
    except (OSError, ValueError):
        return ""


def _resolve_ttl_seconds() -> int:
    raw = os.environ.get("REQUIRE_ISSUE_NEXT_COMPLETION_TTL_SECONDS", "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TTL_SECONDS
    if value <= 0:
        return _DEFAULT_TTL_SECONDS
    return value


def _check_in_progress(repo_root: str, ttl_seconds: int) -> int:
    """in-progress かつ TTL 内なら 2、それ以外は 0 を返す（例外は常に 0 にフォールバック）."""
    state_path = Path(repo_root) / "cache" / "issue-next-state.json"
    if not state_path.is_file():
        return 0

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0

    current = data.get("current_issue")
    if current is None:
        return 0
    try:
        issue_num = int(current)
    except (TypeError, ValueError):
        return 0

    timestamp_raw = data.get("last_active") or data.get("started_at")
    if not isinstance(timestamp_raw, str) or not timestamp_raw:
        return 0
    try:
        last_active = datetime.strptime(timestamp_raw, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return 0

    elapsed = (datetime.now(UTC) - last_active).total_seconds()
    if elapsed > ttl_seconds:
        return 0

    sys.stderr.write(
        f"require-issue-next-completion: Issue #{issue_num} はまだ未完了です "
        f"(cache/issue-next-state.json の current_issue が設定済み・"
        f"last_active から {int(elapsed)}s 経過 < TTL {ttl_seconds}s)。\n"
        f"マージ・後処理（worktree 削除・ブランチ削除・main sync）が完了するまで "
        f"turn を終了せず /issue-next #{issue_num} を継続してください。\n"
    )
    return 2


def main() -> int:
    _drain_stdin()

    if not is_hook_enabled("require-issue-next-completion"):
        return 0

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    if result.returncode != 0:
        return 0
    repo_root = result.stdout.strip()
    if not repo_root:
        return 0

    try:
        return _check_in_progress(repo_root, _resolve_ttl_seconds())
    except Exception:  # noqa: BLE001 — Stop hook はフェイルセーフで exit 0
        return 0


if __name__ == "__main__":
    sys.exit(main())
