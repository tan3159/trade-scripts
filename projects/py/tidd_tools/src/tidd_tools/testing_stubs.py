"""テスト用の実行可能 stub ファイル生成ヘルパー（Issue #3893・#4216）.

テストが外部コマンド（``gh``・``uv`` 等）をモックする際、拡張子なしのファイルに
``#!/usr/bin/env python3`` の shebang を書いて実行ビットを立て PATH 先頭に置く
手法を使ってきた。しかし Windows ネイティブの ``CreateProcess`` は ``PATHEXT``
（``.EXE;.BAT;.CMD`` 等）に載る拡張子でしか実行ファイルを解決せず、拡張子なし
ファイルは無視し shebang も解釈しない。そのため stub が使われず PATH 上の実
バイナリへ無言でフォールスルーし、テストが実ネットワークへアクセスしたり
無関係な失敗を起こしたりする。

``write_stub()`` は Windows では同名の ``.cmd`` ラッパーを追加生成し、
``PATHEXT`` 解決対象にすることで stub を実バイナリより優先起動させる。
``.cmd`` ラッパーの書き込みに失敗した場合は例外がそのまま伝播し、無言の
フォールスルーとして検知漏れにならないようにする。

Issue #4216: 元は ``tests/stub_helpers.py`` に置かれていたが、
``templates/workflow/_copier/vendor_tidd_tools.py`` が ``tests/`` を vendor 配布
対象外としているため consumer 側へ配布されなかった（mn-scripts#1530）。
``stat``・``sys``・``pathlib`` の stdlib のみに依存する独立した汎用ユーティリティ
であり consumer 側の外部コマンド stub 生成にも有用なため、``src/`` 配下（既存の
「``src/`` は丸ごとコピー」ルールに乗る）へ移設した。
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path


def write_stub(stub_dir: Path, name: str, script: str) -> None:
    """``stub_dir/name`` に ``script``（python shebang 付き）の実行可能 stub を書く.

    ``sys.platform == "win32"`` のときは ``stub_dir/{name}.cmd`` を追加生成し、
    現在の Python インタプリタ（``sys.executable``）経由で ``script`` を起動する
    ラッパーにする。``.cmd`` はコマンド名のみで呼び出したときに ``PATHEXT`` 経由で
    優先的に解決されるため、拡張子なしファイルを無視する Windows でも stub が
    実バイナリより先に起動される。
    """
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub_path = stub_dir / name
    stub_path.write_text(script, encoding="utf-8")
    stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    if sys.platform == "win32":
        cmd_path = stub_dir / f"{name}.cmd"
        cmd_path.write_text(
            f'@echo off\r\n"{sys.executable}" "{stub_path}" %*\r\n',
            encoding="utf-8",
        )
