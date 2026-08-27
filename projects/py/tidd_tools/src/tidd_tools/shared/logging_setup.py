"""共通ロガー設定.

`--verbose` / `-v` フラグで WARNING → INFO → DEBUG を切り替える。
"""

from __future__ import annotations

import logging
import sys


def configure(verbosity: int) -> None:
    """ロガーを設定する.

    verbosity=0 → WARNING / 1 → INFO / 2+ → DEBUG.
    """
    level = logging.WARNING
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
