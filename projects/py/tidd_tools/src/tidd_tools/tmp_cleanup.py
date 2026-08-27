"""stale な一時 artifact のみを安全に掃除して一時領域の容量を回復する（Issue #4144）.

親 Issue #4142 の分割 2/3。分割 1/3（#4143・`tmp_capacity`）の TMPDIR 切り替えだけでは、
既定 TMPDIR・repo-local/cache 配下の両方とも空きが不足する場合に回復できない。一方で
一時ファイルの無差別削除は、並行実行中の別セッションの pytest・Hugo の作業ディレクトリを
壊す事故につながる。そのため、以下 4 条件すべて（AND）を満たす artifact のみを削除対象と
する:

  1. 現在のユーザーに所有されている（`_owned_by_current_user`）
  2. 本ツール群が生成する一時 artifact の命名規約に一致する（`_matches_managed_name`）
  3. 最終更新から `stale_age_seconds()`（既定 24 時間）以上経過している（`_is_stale`）
  4. 対応する稼働中プロセスが存在しない（`_has_active_process`。Linux は
     `/proc/<pid>/{cwd,exe,fd/*}`、`/proc` を持たない環境（macOS 等）は
     `lsof +D` へフォールバックしていずれからも参照されていないことを確認する）

広範なパス・所有者を確認できない artifact・期限内の artifact・稼働中プロセスが使用する
artifact は掃除対象から除外し、除外理由を診断ログ（stderr）へ出力する。判定は各 TMPDIR
候補（既定 TMPDIR・repo-local/cache フォールバック）直下のエントリ単位で行い、
配下を再帰的に走査しない（1 エントリ = 1 artifact として扱う）。
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

STALE_AGE_ENV = "TIDD_TMP_STALE_AGE_SECONDS"
DEFAULT_STALE_AGE_SECONDS = 24 * 60 * 60  # 24時間

# 本ツール群が生成する一時 artifact の命名規約（実装箇所は各 tempfile.mkdtemp/mkstemp の
# prefix 引数を参照）。未知の命名は掃除対象に含めない（広範なパスの掃除を防ぐため）。
_MANAGED_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^tidd-copier-(sandbox|tool|tmpl)-"),  # sandbox_copier_poc.py
    re.compile(r"^report-test-status-"),  # report_test_status.py
    re.compile(r"^env-snapshot-"),  # collect_env_snapshot.py
    re.compile(r"^env-diff-"),  # collect_env_snapshot.py
    re.compile(r"^agy-log-"),  # ai_review/backends.py
    re.compile(r"^pytest-of-"),  # pytest 標準の basetemp 親ディレクトリ
)


@dataclasses.dataclass(frozen=True)
class SweepResult:
    """掃除処理の結果（削除した artifact / 残した artifact のパス）."""

    removed: tuple[Path, ...]
    kept: tuple[Path, ...]


def stale_age_seconds() -> float:
    """stale 判定に使う経過時間のしきい値（秒）を返す."""
    raw = os.environ.get(STALE_AGE_ENV)
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    return float(DEFAULT_STALE_AGE_SECONDS)


def _matches_managed_name(name: str) -> bool:
    return any(pattern.match(name) for pattern in _MANAGED_NAME_PATTERNS)


def _owned_by_current_user(st: os.stat_result) -> bool:
    if not hasattr(os, "getuid"):
        return False  # Windows 等: 所有者を判定できないため保守的に「不明」扱いにする
    return st.st_uid == os.getuid()


def _is_stale(st: os.stat_result, *, now: float, ttl: float) -> bool:
    return (now - st.st_mtime) >= ttl


def _under_path(resolved: Path, target: Path) -> bool:
    if resolved == target:
        return True
    try:
        resolved.relative_to(target)
        return True
    except ValueError:
        return False


def _default_proc_root() -> Path:
    """既定の `/proc` パスを返す（テストからの差し替え用の薄いシーム）."""
    return Path("/proc")


def _has_active_process_via_proc(path: Path, proc_dir: Path) -> bool:
    """`proc_dir`（`/proc` 相当）を走査して `path` 配下を参照するプロセスの有無を調べる.

    `cwd`・`exe`・`fd/*` の各シンボリックリンクが `path` 配下を指しているプロセスが
    1 つでもあれば「使用中」と判定する。
    """
    try:
        target = path.resolve()
    except OSError:
        target = path

    for pid_dir in proc_dir.iterdir():
        if not pid_dir.name.isdigit():
            continue
        for link_name in ("cwd", "exe"):
            try:
                resolved = (pid_dir / link_name).resolve()
            except OSError:
                continue
            if _under_path(resolved, target):
                return True
        fd_dir = pid_dir / "fd"
        try:
            fd_entries = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd_entry in fd_entries:
            try:
                resolved = fd_entry.resolve()
            except OSError:
                continue
            if _under_path(resolved, target):
                return True
    return False


def _has_active_process_via_lsof(path: Path) -> bool:
    """`lsof +D <path>` を使って `path` 配下を参照するプロセスの有無を調べる（macOS 等）.

    `/proc` を持たない環境（macOS 等）向けのフォールバック。`lsof` 自体の実行に
    失敗した場合は判定できないため、安全側に倒して「稼働中プロセスあり」として扱う。
    """
    try:
        target = path.resolve()
    except OSError:
        target = path
    try:
        result = subprocess.run(
            ["lsof", "+D", str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return bool(result.stdout.strip())


def _has_active_process(path: Path, *, proc_root: Path | None = None) -> bool:
    """`path` 配下を参照する稼働中プロセスが存在するか調べる.

    Linux では `/proc` 走査（`_has_active_process_via_proc`）で判定する。`/proc` を
    持たない環境（macOS 等）では `lsof +D` （`_has_active_process_via_lsof`）に
    フォールバックする。`lsof` も使えない場合は判定できないため、安全側に倒して
    「稼働中プロセスあり」として扱い掃除対象から除外する。

    `proc_root` を明示指定した呼び出し（テスト用の `/proc` 差し替え）では、
    lsof フォールバックへは進まず、`/proc` 相当が無ければ従来どおり安全側の
    True を返す（Linux 環境固有の判定ロジックのみを検証するテストの挙動を保つため）。
    """
    proc_dir = _default_proc_root() if proc_root is None else proc_root
    if proc_dir.is_dir():
        return _has_active_process_via_proc(path, proc_dir)
    if proc_root is not None:
        return True
    if shutil.which("lsof"):
        return _has_active_process_via_lsof(path)
    return True


def sweep_stale_artifacts(
    root: Path,
    *,
    now: float | None = None,
    ttl_seconds: float | None = None,
    stderr: TextIO | None = None,
) -> SweepResult:
    """`root` 直下の管理対象 artifact のうち stale なものだけを安全に掃除する.

    4 条件（所有者・命名規約・期限超過・稼働中プロセスなし）の AND を満たす artifact
    のみを削除する。条件を満たさない・判定できない artifact は残し、除外理由を
    `stderr` へ出力する。`root` が存在しない場合は no-op（削除も除外ログも出さない）。
    このため常に成功として扱う（例外は送出しない）。
    """
    out = sys.stderr if stderr is None else stderr
    removed: list[Path] = []
    kept: list[Path] = []
    if not root.is_dir():
        return SweepResult(removed=(), kept=())

    now_ts = time.time() if now is None else now
    ttl = stale_age_seconds() if ttl_seconds is None else ttl_seconds

    for entry in sorted(root.iterdir()):
        if not _matches_managed_name(entry.name):
            continue  # 管理対象外の命名は無視（広範な掃除を防ぐ・診断ログにも出さない）
        try:
            st = entry.lstat()
        except OSError as exc:
            print(f"除外: stat に失敗したため掃除対象外にします: {entry} ({exc})", file=out)
            kept.append(entry)
            continue
        if not _owned_by_current_user(st):
            print(f"除外: 所有者を確認できないため掃除対象外にします: {entry}", file=out)
            kept.append(entry)
            continue
        if not _is_stale(st, now=now_ts, ttl=ttl):
            print(
                f"除外: 期限内（経過 {now_ts - st.st_mtime:.0f}秒 < {ttl:.0f}秒）のため掃除対象外にします: {entry}",
                file=out,
            )
            kept.append(entry)
            continue
        if _has_active_process(entry):
            print(f"除外: 稼働中プロセスが使用中のため掃除対象外にします: {entry}", file=out)
            kept.append(entry)
            continue
        print(f"==> stale artifact を掃除します: {entry}", file=out)
        try:
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            else:
                shutil.rmtree(entry)
        except OSError as exc:
            print(f"WARN: 削除に失敗しました: {entry} ({exc})", file=out)
            kept.append(entry)
            continue
        removed.append(entry)
    return SweepResult(removed=tuple(removed), kept=tuple(kept))


def recover_capacity(default_dir: Path, fallback_dir: Path, *, stderr: TextIO | None = None) -> None:
    """TMPDIR 切り替えだけで回復できない場合に、両候補ディレクトリを掃除する.

    `pre_flight._run_pytest_and_jest` から `tmp_capacity.diagnose()` が失敗したときのみ
    呼ばれる（Issue #4144）。掃除自体は常に安全（4 条件 AND を満たす artifact のみ削除）
    なため、呼び出し元は掃除後に `tmp_capacity.diagnose()` を再実行して回復を確認する。
    """
    sweep_stale_artifacts(default_dir, stderr=stderr)
    sweep_stale_artifacts(fallback_dir, stderr=stderr)
