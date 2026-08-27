"""`subprocess_runner.run` 呼び出しに共通で使う named timeout 定数（Issue #2965）.

`ai_review/backends.py` の ``BACKEND_SUBPROCESS_TIMEOUT_SEC`` / `gh_client.py` の
``DEFAULT_TIMEOUT_SECONDS`` と同じパターンで、散在していた ``timeout=<秒数>`` の
マジックナンバーを意味のある名前にまとめる（詳細: `docs/reference/ai-review-timeouts.md`）。

値は既存呼び出し箇所の実測・既存設定をそのまま踏襲し、挙動を変えない
（複数箇所で異なる値を使っていたものを統一するリスクを避けるため、
値ごとに 1 定数を割り当てる）。
"""

from __future__ import annotations

import os

#: `git rev-parse` 等、瞬時に終わるはずの軽量コマンド向け。
QUICK_TIMEOUT_SEC = 10.0

#: `bw list items` の既定値・`git add -A` 等、軽量コマンドよりやや余裕を持たせたいもの向け。
STANDARD_TIMEOUT_SEC = 30.0

#: バージョン確認・診断コマンド（`--version` / `ps` / `pstree` / `du -sh` 等）向け。
TOOL_VERSION_TIMEOUT_SEC = 60.0

#: pytest / jest フルスイート等、テスト実行コマンド向け。
TEST_SUITE_TIMEOUT_SEC = 1800.0


def test_suite_timeout_sec() -> float:
    """pytest/Jest の実行上限を返す（``TIDD_PYTEST_TIMEOUT_SECS`` で上書き可能）."""
    raw = os.environ.get("TIDD_PYTEST_TIMEOUT_SECS")
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value >= 1.0:
            return value
    return TEST_SUITE_TIMEOUT_SEC
