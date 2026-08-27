"""一時領域（TMPDIR）の容量・inode 診断と repo-local フォールバック選択（Issue #4143）.

consumer の unattended issue-next 運用で、複数の pytest・Hugo・Codex・Claude セッションが
tmpfs の `/tmp` を食い潰し `OSError: [Errno 28] No space left on device` が発生した
（親 Issue #4142）。実装と無関係な一時的資源枯渇がテスト失敗として現れ
`classify-test-failure` が判定不能になるため、重いテストを起動する前に既定 TMPDIR の
空き容量・inode を診断し、容量不足の場合のみ repo-local/cache 配下へ自動的に
切り替える。

inode 不足は自動切替の対象外（診断のみ）: repo-local フォールバックが既定 TMPDIR と
同一ホストの inode テーブルを共有している可能性があり、切替の安全性を保証できない。
stale artifact の掃除・park を伴わない再選定は #4144・#4145 のスコープ。
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import TextIO

from tidd_tools.shared.paths import resolve_repo_root

TMPDIR_ENV = "TMPDIR"
MIN_BYTES_ENV = "TIDD_TMP_MIN_FREE_BYTES"
MIN_INODES_ENV = "TIDD_TMP_MIN_FREE_INODES"

DEFAULT_MIN_BYTES = 1 * 1024**3  # 1GiB
DEFAULT_MIN_INODES = 50_000


@dataclasses.dataclass(frozen=True)
class CapacityInfo:
    """1 ファイルシステムの空き容量・inode 情報."""

    filesystem: str
    free_bytes: int
    free_inodes: int | None  # None: プラットフォームが inode 情報を提供しない（Windows 等）


@dataclasses.dataclass(frozen=True)
class Diagnosis:
    """診断結果（選択された TMPDIR とその可否）."""

    default_dir: Path
    default_info: CapacityInfo
    selected_dir: Path
    ok: bool
    reason: str | None  # ok=False のときの理由


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value >= 1:
            return value
    return default


def min_bytes() -> int:
    """診断で要求する最低空き容量（bytes）を返す."""
    return _env_int(MIN_BYTES_ENV, DEFAULT_MIN_BYTES)


def min_inodes() -> int:
    """診断で要求する最低空き inode 数を返す."""
    return _env_int(MIN_INODES_ENV, DEFAULT_MIN_INODES)


def default_tmpdir() -> Path:
    """既定の TMPDIR（環境変数優先・未設定時は `/tmp`）を返す."""
    return Path(os.environ.get(TMPDIR_ENV) or "/tmp")


def fallback_tmpdir() -> Path:
    """repo-local/cache 配下のフォールバック TMPDIR 候補を返す（未作成でもよい）.

    `shared.paths.cache_dir()` は OS 別のユーザーホーム配下（`~/.cache/tidd` 等）を返す
    ため使わない。既定 TMPDIR と別ファイルシステム（home が別デバイス・NFS 等）を
    選んでしまう可能性があり、「repo-local」という受け入れ基準を満たさない（レビュー
    指摘: PR #4146）。`issue_next_state._state_dir()` 等と同じ `cache/` 配下の
    慣例に揃え、必ずリポジトリ内のファイルシステムを使う。
    """
    return resolve_repo_root() / "cache" / "tmp"


def _nearest_existing_ancestor(path: Path) -> Path:
    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            return probe
        probe = parent
    return probe


def capacity_info(path: Path) -> CapacityInfo:
    """`path` が存在するファイルシステムの空き bytes・inode を返す.

    `path` 自体が未作成でも、存在する最も近い親ディレクトリまで遡って調べる。
    """
    probe = _nearest_existing_ancestor(path)
    if sys.platform == "win32" or not hasattr(os, "statvfs"):
        # Windows 等 statvfs 非対応環境: bytes のみ `shutil.disk_usage` で判定する
        # （NTFS は POSIX の inode に相当する固定枯渇資源を持たないため対象外）。
        usage = shutil.disk_usage(probe)
        return CapacityInfo(filesystem=str(probe), free_bytes=usage.free, free_inodes=None)
    st = os.statvfs(probe)
    free_bytes = st.f_bavail * st.f_frsize
    return CapacityInfo(filesystem=str(probe), free_bytes=free_bytes, free_inodes=st.f_favail)


def _satisfies(info: CapacityInfo, *, need_bytes: int, need_inodes: int) -> bool:
    if info.free_bytes < need_bytes:
        return False
    return not (info.free_inodes is not None and info.free_inodes < need_inodes)


def _format_bytes(num_bytes: int) -> str:
    mib = num_bytes / 1024**2
    return f"{mib:.0f}MiB" if mib < 1024 else f"{mib / 1024:.1f}GiB"


def _format_inodes(count: int | None) -> str:
    return "N/A" if count is None else f"{count:,}"


def diagnose(
    *,
    default_dir: Path | None = None,
    fallback_dir: Path | None = None,
    need_bytes: int | None = None,
    need_inodes: int | None = None,
    stderr: TextIO | None = None,
    quiet: bool = False,
) -> Diagnosis:
    """既定 TMPDIR の容量・inode を診断し、容量不足なら repo-local フォールバックへ切り替える.

    inode 不足は切替を試みず診断のみ（モジュール docstring 参照）。

    `quiet=True` のとき、容量・inode が十分で TMPDIR を変更しない通常時の情報ログ
    （1 行目）を抑制する。pytest サブプロセスを起動するたびに毎回このログを出すと
    「実行成功時はサマリ 1 行のみ」という既存の出力契約（Issue #3426）を壊すため、
    `test_plan._build_pytest_subprocess_env` から呼ぶ際に使う。WARN/ERROR・
    切り替え発生時のログは `quiet` に関わらず常に出力する（見逃すと運用上のリスクが
    大きいため）。
    """
    out = sys.stderr if stderr is None else stderr
    default = default_dir if default_dir is not None else default_tmpdir()
    need_b = need_bytes if need_bytes is not None else min_bytes()
    need_i = need_inodes if need_inodes is not None else min_inodes()

    default_info = capacity_info(default)
    if not quiet:
        print(
            f"==> 一時領域診断: {default_info.filesystem} "
            f"空き{_format_bytes(default_info.free_bytes)}・空き inode {_format_inodes(default_info.free_inodes)}"
            f"・選択 TMPDIR {default}",
            file=out,
        )

    if default_info.free_inodes is not None and default_info.free_inodes < need_i:
        print(
            f"ERROR: 一時領域の inode が不足しています（空き inode {default_info.free_inodes} < "
            f"必要 {need_i}・{default_info.filesystem}）",
            file=out,
        )
        return Diagnosis(
            default_dir=default,
            default_info=default_info,
            selected_dir=default,
            ok=False,
            reason="inode不足",
        )

    if default_info.free_bytes >= need_b:
        return Diagnosis(
            default_dir=default,
            default_info=default_info,
            selected_dir=default,
            ok=True,
            reason=None,
        )

    fallback = fallback_dir if fallback_dir is not None else fallback_tmpdir()
    fallback_info = capacity_info(fallback)
    print(
        f"WARN: 既定 TMPDIR の空き容量が不足しています"
        f"（{_format_bytes(default_info.free_bytes)} < 必要 {_format_bytes(need_b)}・{default_info.filesystem}）。"
        f"repo-local/cache 配下への切り替えを試みます: {fallback}",
        file=out,
    )

    if not _satisfies(fallback_info, need_bytes=need_b, need_inodes=need_i):
        print(
            f"ERROR: repo-local フォールバック {fallback} も容量不足です"
            f"（空き{_format_bytes(fallback_info.free_bytes)}・空き inode "
            f"{_format_inodes(fallback_info.free_inodes)}）",
            file=out,
        )
        return Diagnosis(
            default_dir=default,
            default_info=default_info,
            selected_dir=default,
            ok=False,
            reason="容量不足（フォールバックも不足）",
        )

    fallback.mkdir(parents=True, exist_ok=True)
    print(f"==> 一時領域診断: TMPDIR を repo-local/cache 配下へ切り替えました: {fallback}", file=out)
    return Diagnosis(
        default_dir=default,
        default_info=default_info,
        selected_dir=fallback,
        ok=True,
        reason=None,
    )


def apply_env(diagnosis: Diagnosis, env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """診断が成功した場合のみ選択した TMPDIR を `env` へ書き込む."""
    if diagnosis.ok:
        env[TMPDIR_ENV] = str(diagnosis.selected_dir)
    return env
