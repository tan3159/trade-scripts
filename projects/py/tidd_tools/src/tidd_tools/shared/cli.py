"""共通 CLI フラグ.

各サブコマンドの `register(subparsers)` から呼んで共通フラグを追加する。
"""

from __future__ import annotations

import argparse
import sys


def add_common_flags(
    parser: argparse.ArgumentParser,
    *,
    dry_run_supported: bool = True,
) -> None:
    """全サブコマンド共通の `--verbose / --dry-run / --json` を追加する.

    Args:
        parser: 追加先のパーサ。
        dry_run_supported: このサブコマンドが `--dry-run` を実装しているかどうか。
            ``False`` を指定すると ``args._dry_run_supported = False`` がデフォルト
            に設定され、``check_dry_run_not_implemented()`` が呼び出し時に
            stderr に未対応メッセージを出力して ``True``（エラーあり）を返す。
            実装済みのサブコマンドには ``True``（デフォルト）を指定する。
    """
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="ログレベルを上げる（-v=INFO / -vv=DEBUG）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="副作用を伴う処理（PR ボディ更新等）をスキップしてプレビューする",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="機械可読 JSON 出力モード",
    )
    parser.set_defaults(_dry_run_supported=dry_run_supported)


def check_dry_run_not_implemented(
    args: argparse.Namespace,
    *,
    subcmd: str = "",
) -> bool:
    """``--dry-run`` が未実装のサブコマンドへの誤指定を検出する（Issue #2791）.

    Args:
        args: parse 済み引数。``args.dry_run`` と ``args._dry_run_supported`` を参照する。
        subcmd: エラーメッセージに含めるサブコマンド名（省略可）。

    Returns:
        ``True`` の場合はエラーあり（呼び出し元は ``exit 2`` 等で終了すること）、
        ``False`` の場合は問題なし。
    """
    if not getattr(args, "dry_run", False):
        return False
    if getattr(args, "_dry_run_supported", True):
        return False
    name = f"tidd {subcmd}" if subcmd else "このサブコマンド"
    sys.stderr.write(
        f"{name}: --dry-run は未対応です。"
        " このサブコマンドは副作用を持たないか、--dry-run の実装が不要と判断されています。"
        " --dry-run を指定せずに実行してください。\n"
    )
    return True
