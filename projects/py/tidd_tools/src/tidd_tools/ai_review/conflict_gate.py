"""ai-review マージコンフリクト gate（Issue #2989）.

backend review 呼び出し前に PR のマージ可否（``mergeable``）を確認し、
コンフリクトがある（``CONFLICTING``）ときはレビューを中断する。

PR #2988 で、ai-review 2/2 APPROVE + CI 全緑まで完了していたにもかかわらず
マージコンフリクトが残っていたため、コンフリクト解消後の再 push で新 SHA が乗り
レビュー結果が丸ごと無駄になった。この無駄を backend 呼び出し前に検出して防ぐ。

``test_status_gate.py::check_test_statuses()`` と同構成。

**使い方:**

ai-review の main フローで、backend review 実行前に ``check_conflict_state()`` を呼ぶ。

stdlib のみ使用（``gh_client`` 経由で gh CLI を呼ぶ）。
"""

from __future__ import annotations

import sys

from tidd_tools.shared.errors import GhCommandError, SubprocessTimeoutError
from tidd_tools.shared.gh_client import pr_view

_CONFLICTING = "CONFLICTING"


def check_conflict_state(
    pr_num: str,
    repo: str,
    token: str = "",
) -> tuple[bool, str]:
    """PR のマージ可否（``mergeable``）を検査する.

    GitHub のマージ可否判定は非同期計算のため、push 直後は ``UNKNOWN``
    （未確定）を返すことがある。未確定時は検出漏れの最悪ケースが「無駄な
    レビュー1回」に留まるため、フェイルセーフで通過させる。

    Args:
        pr_num: PR 番号
        repo: リポジトリ (owner/name)
        token: GitHub トークン（省略可）

    Returns:
        (passed, mergeable_state):
          - passed=True: コンフリクトなし（``MERGEABLE``）、または判定未確定
            （``UNKNOWN``。フェイルセーフ）、または取得失敗（フェイルセーフ）
          - passed=False: コンフリクトあり（``CONFLICTING``）
          - mergeable_state: gh から取得した ``mergeable`` の値（大文字）。
            取得失敗時は ``"UNKNOWN"``
    """
    try:
        data = pr_view(pr_num, repo=repo, fields=("mergeable",), token=token)
    except (GhCommandError, SubprocessTimeoutError) as exc:
        sys.stderr.write(f"WARN: conflict_gate: PR 情報取得に失敗したため skip します: {exc}\n")
        return True, "UNKNOWN"

    mergeable_state = str(data.get("mergeable") or "UNKNOWN").upper()
    if mergeable_state == _CONFLICTING:
        sys.stderr.write(
            f"ERROR: conflict_gate: PR #{pr_num} にマージコンフリクトがあります。コンフリクトを解消してください。\n"
        )
        return False, mergeable_state

    return True, mergeable_state
