"""pytest 一時ディレクトリの配置と肥大化防止（Issue #2834）.

`/tmp` は tmpfs（RAM）で uv キャッシュとは別ファイルシステムのため、uv が
`.venv` を作るときハードリンクを張れず全バイトをコピーする。consumer venv を作る
テストが 1 個あたり 100MB 級を RAM に実コピーし、フルスイート数回で tmpfs を
使い切ってテスト全体が原因不明の大量失敗・タイムアウトに至っていた。

basetemp を uv キャッシュと同一ファイルシステム上へ移し、掃除漏れに備えて
pytest 起動前の総容量ガードを提供する。
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import TextIO

from tidd_tools.shared.paths import cache_dir

TEMPROOT_ENV = "PYTEST_DEBUG_TEMPROOT"
THRESHOLD_ENV = "TIDD_PYTEST_TMP_THRESHOLD_BYTES"
DEFAULT_THRESHOLD_BYTES = 2 * 1024**3

_GENERATION_RE = re.compile(r"^pytest-\d+$")
_CURRENT_LINK = "pytest-current"


def temproot(*, create: bool = False) -> Path:
    """pytest の basetemp 親ディレクトリを返す（uv キャッシュと同一ファイルシステム）."""
    root = cache_dir() / "pytest-tmp"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def apply_temproot(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """`env` に basetemp 親ディレクトリを設定して返す（既に設定済みなら尊重する）."""
    if not env.get(TEMPROOT_ENV):
        env[TEMPROOT_ENV] = str(temproot(create=True))
    return env


def threshold_bytes() -> int:
    """掃除を発動する総容量の閾値を返す."""
    raw = os.environ.get(THRESHOLD_ENV)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value >= 1:
            return value
    return DEFAULT_THRESHOLD_BYTES


def _basedir(root: Path) -> Path:
    return root / f"pytest-of-{getpass.getuser()}"


def _dir_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            total += entry.stat().st_size
        except OSError:
            continue
    return total


def _format_size(num_bytes: int) -> str:
    mib = num_bytes / 1024**2
    return f"{mib:.0f}MiB" if mib < 1024 else f"{mib / 1024:.1f}GiB"


def cleanup(root: Path | None = None, *, threshold: int | None = None, stderr: TextIO | None = None) -> int:
    """総容量が閾値を超えていれば古い世代を削除し、回収したバイト数を返す.

    実行中の pytest を壊さないため `pytest-current` が指す世代は削除しない。
    削除に失敗した世代は読み飛ばす（呼び出し元の処理は継続させる）。
    """
    out = sys.stderr if stderr is None else stderr
    limit = threshold_bytes() if threshold is None else threshold
    basedir = _basedir(temproot() if root is None else root)
    if not basedir.is_dir():
        return 0

    sizes: dict[Path, int] = {}
    for entry in sorted(basedir.iterdir()):
        if entry.is_symlink() or not entry.is_dir() or not _GENERATION_RE.match(entry.name):
            continue
        sizes[entry] = _dir_size(entry)

    total = sum(sizes.values())
    if total <= limit:
        return 0

    current: Path | None = None
    link = basedir / _CURRENT_LINK
    if link.is_symlink():
        try:
            current = link.resolve()
        except OSError:
            current = None

    generations = sorted(sizes, key=lambda p: int(p.name.rsplit("-", 1)[1]))
    removed = 0
    reclaimed = 0
    for generation in generations:
        if total <= limit:
            break
        if current is not None and generation == current:
            continue
        try:
            shutil.rmtree(generation)
        except OSError as exc:
            print(f"WARN: pytest 一時ディレクトリの削除に失敗しました: {generation} ({exc})", file=out)
            continue
        removed += 1
        reclaimed += sizes[generation]
        total -= sizes[generation]

    if removed:
        print(
            f"==> pytest 一時ディレクトリを掃除しました: {removed} 世代削除・{_format_size(reclaimed)} 回収"
            f"（{basedir}）",
            file=out,
        )
    return reclaimed
