"""再入検知ガード（旧 `_AI_REVIEW_RUNNING + BATS_TMPDIR` の置換）.

`_TIDD_TOOLS_RECURSION_GUARD=1` が設定されているサブプロセスから
`tidd_tools` を呼び出した場合は即座にスキップして無限再帰を抑制する。
"""

from __future__ import annotations

import os

GUARD_ENV = "_TIDD_TOOLS_RECURSION_GUARD"


def is_recursive_call() -> bool:
    """再帰呼び出しかどうかを判定する."""
    return os.environ.get(GUARD_ENV) == "1"


def mark_recursive_subprocess(env: dict[str, str] | None = None) -> dict[str, str]:
    """サブプロセスに `_TIDD_TOOLS_RECURSION_GUARD=1` を伝播させた環境変数 dict を返す."""
    base = dict(env) if env is not None else dict(os.environ)
    base[GUARD_ENV] = "1"
    return base
