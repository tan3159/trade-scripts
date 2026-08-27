"""TiDD ワークフローの開発インフラ CLI パッケージ.

サブコマンドは `[project.entry-points."tidd_tools.commands"]` で登録され、
`tidd_tools.__main__` の argparse ディスパッチャから discover される。

Phase 1 (#1050): test-plan サブコマンドを実装する。
"""

__version__ = "0.1.0"
