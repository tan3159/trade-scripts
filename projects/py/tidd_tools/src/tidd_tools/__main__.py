"""tidd_tools の argparse ディスパッチャ.

サブコマンドは `[project.entry-points."tidd_tools.commands"]` で登録され、
ここで `importlib.metadata.entry_points(group=...)` から discover される。
各サブコマンドモジュールは `register(subparsers)` を実装する必要がある。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from importlib.metadata import EntryPoint, entry_points

from tidd_tools.shared.logging_setup import configure as configure_logging
from tidd_tools.shared.recursion import is_recursive_call
from tidd_tools.shared.venv_repair import check_and_repair_venv

ENTRY_POINT_GROUP = "tidd_tools.commands"

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tidd",
        description="TiDD ワークフローの開発インフラ CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    for ep in _iter_entry_points():
        try:
            register: Callable[[argparse._SubParsersAction[argparse.ArgumentParser]], None] = ep.load()
        except Exception as exc:
            logger.warning("entry_point %s の読み込みに失敗: %s", ep.name, exc)
            continue
        register(subparsers)

    return parser


def _iter_entry_points() -> list[EntryPoint]:
    """`tidd_tools.commands` group の entry points を名前順に返す.

    Python 3.10+ で entry_points(group=...) が正式に追加されたため、
    本パッケージの requires-python (>=3.11) では直接呼び出せる。
    """
    selected = entry_points(group=ENTRY_POINT_GROUP)
    return sorted(selected, key=lambda ep: ep.name)


def _ensure_utf8_streams() -> None:
    """標準出力・標準エラー出力を明示的に UTF-8 へ設定する（Issue #3603）.

    日本語版Windowsでは既定の標準出力エンコーディングが `cp932` であり、絵文字を含む
    argparse のヘルプ文字列（`🙋` 等）を出力しようとすると `UnicodeEncodeError` で
    クラッシュする。`PYTHONUTF8` 環境変数の設定有無・OS のロケールに関わらず常に
    UTF-8 で出力するよう、`reconfigure()` をサポートするストリームのみ変更する
    （pytest の capsys 等、`reconfigure` を持たない差し替え済みストリームは対象外）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """エントリポイント.

    `--help` / `-h` / 引数解析エラーは argparse のデフォルト挙動でそのまま処理する
    （`SystemExit` を送出）。再帰ガード判定はサブコマンドのハンドラ呼び出し直前に
    置くことで、help 表示や引数バリデーションは再帰実行中でも常に動作する。

    .venv の Python バージョンを検査し、3.11 系でなければ自動修復して再起動する（Issue #2178）。
    """
    _ensure_utf8_streams()
    check_and_repair_venv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_logging(getattr(args, "verbose", 0))

    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2

    if is_recursive_call():
        # サブコマンド実行直前の再帰ガード（旧 test-plan.sh の _AI_REVIEW_RUNNING + BATS_TMPDIR 後継）。
        # tidd_tools がサブプロセスとして bats / Jest / pytest を起動するときに
        # _TIDD_TOOLS_RECURSION_GUARD=1 を注入しているため、その子プロセス内から
        # python -m tidd_tools <subcommand> を呼んだ場合は副作用なしで 0 終了する。
        return 0

    func: Callable[[argparse.Namespace], int] | None = getattr(args, "func", None)
    if func is None:
        parser.error(f"サブコマンド {args.command} に func ハンドラが設定されていません")
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
