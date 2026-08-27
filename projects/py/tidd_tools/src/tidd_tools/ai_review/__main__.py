"""``python -m tidd_tools.ai_review`` エントリ.

通常はトップレベルの ``python -m tidd_tools ai-review <PR> <ATTEMPT>`` を使うが、
パッケージ単独実行 (``python -m tidd_tools.ai_review``) もサポートする。
"""

from __future__ import annotations

import sys

from tidd_tools.__main__ import main as tidd_main


def main(argv: list[str] | None = None) -> int:
    args = ["ai-review", *(argv if argv is not None else sys.argv[1:])]
    return tidd_main(args)


if __name__ == "__main__":
    sys.exit(main())
