"""`tidd collect-env-snapshot` サブコマンド（旧 `scripts/collect-env-snapshot.sh` の Python 移植）.

環境スナップショット（dotfiles・ツールバージョン・chezmoi 状態）を収集して
`~/env-snapshot-<hostname>-<date>.tar.gz` に固める。生成した tarball を別環境に
持ち込めば、同サブコマンドの `--diff` モードで差分比較できる。

収集対象 / 除外対象:
- 含む: ~/.bashrc / ~/.bash_profile / ~/.profile / ~/.gitconfig / ~/.gitignore_global
        ~/.gnupg/gpg-agent.conf / ~/.config/mise/config.toml
        ~/.claude/settings.json / ~/.claude/CLAUDE.md
        ~/.password-store/.gpg-id（GPG-ID のみ・秘密情報なし）
- 含まない（セキュリティ）:
  - ~/.password-store/ 本体（GPG 暗号化パスワード）
  - GPG 秘密鍵
  - ~/.config/gh/（OAuth トークン）
  - ~/.claude/settings.local.json（トークン）
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from shutil import which

from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import SubprocessTimeoutError
from tidd_tools.shared.subprocess_runner import run as run_subprocess
from tidd_tools.shared.subprocess_timeouts import STANDARD_TIMEOUT_SEC, TOOL_VERSION_TIMEOUT_SEC

# 収集対象のファイル一覧（HOME 相対パス）
DOTFILES: tuple[str, ...] = (
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".gitconfig",
    ".gitignore_global",
)
GPG_PASS_FILES: tuple[str, ...] = (".gnupg/gpg-agent.conf",)
MISE_FILES: tuple[str, ...] = (".config/mise/config.toml",)

# `versions.txt` で出力するコマンド一覧
VERSION_COMMANDS: tuple[str, ...] = (
    "bash",
    "git",
    "gh",
    "jq",
    "agy",
    "claude",
    "mise",
    "uv",
    "volta",
    "pwsh",
    "bats",
    "shellcheck",
    "python3",
    "node",
    "npm",
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "collect-env-snapshot",
        help="環境スナップショットを収集 / 比較する（旧 scripts/collect-env-snapshot.sh）",
        description=__doc__,
    )
    add_common_flags(parser)
    parser.add_argument(
        "--diff",
        nargs="+",
        metavar="TARBALL",
        help="2 つの tarball を比較する（B 省略時はカレント環境とライブ比較）",
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    if args.diff:
        if len(args.diff) > 2:
            print("ERROR: --diff には tarball を最大 2 つまで指定してください", file=sys.stderr)
            return 1
        tarball_a = Path(args.diff[0])
        tarball_b = Path(args.diff[1]) if len(args.diff) == 2 else None
        return diff_snapshots(tarball_a=tarball_a, tarball_b=tarball_b)
    return collect_snapshot()


# ── 収集モード ──────────────────────────────────────────────────────────────


def collect_snapshot() -> int:
    home = Path(os.environ.get("HOME", str(Path.home())))
    hostname_short = _hostname_short()
    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    # ADR 013 (Windows first-class support): dir=None で OS デフォルトの
    # tempfile.gettempdir() に委ねる（Windows: %TEMP% / Linux: /tmp）。
    snap_dir = Path(tempfile.mkdtemp(prefix=f"env-snapshot-{hostname_short}-{date_str}-"))
    output_tarball = home / f"env-snapshot-{hostname_short}-{date_str}.tar.gz"

    print(f"==> スナップショット収集開始: {hostname_short} @ {date_str}")
    print(f"==> 作業ディレクトリ: {snap_dir}")

    files_dir = snap_dir / "files"

    print()
    print("── dotfiles ──")
    for rel in DOTFILES:
        _copy_file_to_snap(home_path=home / rel, snap_files=files_dir, home=home)

    print()
    print("── GPG / pass ──")
    for rel in GPG_PASS_FILES:
        _copy_file_to_snap(home_path=home / rel, snap_files=files_dir, home=home)
    pass_gpg_id = home / ".password-store" / ".gpg-id"
    if pass_gpg_id.is_file():
        dst = files_dir / ".password-store" / ".gpg-id"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pass_gpg_id, dst)
        print("  ✓ ~/.password-store/.gpg-id (GPG-ID のみ、パスワード本体は収集しない)")
    else:
        print("  - ~/.password-store/.gpg-id (なし)")

    print()
    print("── mise ──")
    for rel in MISE_FILES:
        _copy_file_to_snap(home_path=home / rel, snap_files=files_dir, home=home)

    print()
    print("── Claude Code ──")
    # settings.json
    _copy_file_to_snap(home_path=home / ".claude" / "settings.json", snap_files=files_dir, home=home)
    print("  - ~/.claude/settings.local.json (セキュリティ上スキップ)")
    _copy_file_to_snap(home_path=home / ".claude" / "CLAUDE.md", snap_files=files_dir, home=home)

    # ── ツールバージョン ────────────────────────────────────────────────
    print()
    print("── ツールバージョン収集 ──")
    versions_file = snap_dir / "versions.txt"
    versions_file.write_text(
        _build_versions_text(hostname_short=hostname_short, date_str=date_str, home=home),
        encoding="utf-8",
    )
    print("  ✓ versions.txt")

    # ── chezmoi 状態 ───────────────────────────────────────────────────
    print()
    print("── chezmoi ──")
    chezmoi_file = snap_dir / "chezmoi-status.txt"
    chezmoi_file.write_text(
        _build_chezmoi_status_text(hostname_short=hostname_short, date_str=date_str),
        encoding="utf-8",
    )
    print("  ✓ chezmoi-status.txt")

    # ── tarball 作成 ────────────────────────────────────────────────────
    print()
    print(f"==> tarball 作成中: {output_tarball}")
    with tarfile.open(output_tarball, "w:gz") as tar:
        tar.add(snap_dir, arcname=snap_dir.name)
    shutil.rmtree(snap_dir, ignore_errors=True)

    size = _du_sh(output_tarball)
    print(f"==> 完了: {output_tarball} ({size})")
    print()
    print("次のステップ:")
    print("  1. この tarball をもう一方の環境でも同様に生成する")
    print("  2. 両方の tarball をこの環境に持ち込む")
    print("  3. 比較: tidd collect-env-snapshot --diff <tarball-A> <tarball-B>")
    return 0


def _copy_file_to_snap(*, home_path: Path, snap_files: Path, home: Path) -> None:
    rel = home_path.relative_to(home).as_posix()
    dst = snap_files / rel
    if home_path.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(home_path, dst)
        print(f"  ✓ ~/{rel}")
    else:
        print(f"  - ~/{rel} (なし)")


def _hostname_short() -> str:
    """`hostname -s` 相当。失敗時は full hostname。"""
    try:
        return socket.gethostname().split(".")[0]
    except OSError:
        return "unknown"


def _build_versions_text(*, hostname_short: str, date_str: str, home: Path) -> str:
    out: list[str] = []
    out.append(f"# env-snapshot versions @ {hostname_short} {date_str}")
    out.append("")
    out.append("## OS")
    out.append(_run_capture(["uname", "-a"], fallback="(uname 失敗)"))
    os_release = Path("/etc/os-release")
    if os_release.is_file():
        try:
            for line in os_release.read_text(encoding="utf-8").splitlines():
                if line.startswith("NAME=") or line.startswith("VERSION="):
                    out.append(line)
        except OSError:
            pass
    out.append("")
    out.append("## Shell tools")
    for cmd in VERSION_COMMANDS:
        if which(cmd) is None:
            out.append(f"{cmd}: (未インストール)")
            continue
        try:
            result = run_subprocess([cmd, "--version"], timeout=TOOL_VERSION_TIMEOUT_SEC)
        except SubprocessTimeoutError:
            out.append(f"{cmd}: (バージョン取得タイムアウト)")
            continue
        if result.returncode != 0:
            out.append(f"{cmd}: (バージョン取得失敗 exit={result.returncode})")
            continue
        version_lines = (result.stdout or result.stderr).splitlines()
        ver = version_lines[0] if version_lines else "(バージョン取得失敗)"
        out.append(f"{cmd}: {ver}")
    out.append("")
    out.append("## mise ls (インストール済みツール)")
    if which("mise") is not None:
        try:
            result = run_subprocess(["mise", "ls"], timeout=STANDARD_TIMEOUT_SEC)
        except SubprocessTimeoutError:
            out.append("(mise ls タイムアウト)")
            # タイムアウト時も後続セクション（pass 等）の収集を継続する（#3278）
        else:
            out.append(result.stdout.rstrip() if result.returncode == 0 else "(mise ls 失敗)")
    else:
        out.append("(mise 未インストール)")
    out.append("")
    out.append("## pass (GPG-ID のみ)")
    pass_gpg_id = home / ".password-store" / ".gpg-id"
    if pass_gpg_id.is_file():
        try:
            out.append(pass_gpg_id.read_text(encoding="utf-8").rstrip())
        except OSError:
            out.append("(~/.password-store/.gpg-id なし)")
    else:
        out.append("(~/.password-store/.gpg-id なし)")
    return "\n".join(out) + "\n"


def _build_chezmoi_status_text(*, hostname_short: str, date_str: str) -> str:
    out: list[str] = []
    out.append(f"# chezmoi status @ {hostname_short} {date_str}")
    out.append("")
    if which("chezmoi") is None:
        out.append("chezmoi: 未インストール")
        return "\n".join(out) + "\n"
    out.append("## chezmoi --version")
    out.append(_run_capture(["chezmoi", "--version"], fallback="(失敗)"))
    out.append("")
    out.append("## chezmoi source-path")
    out.append(_run_capture(["chezmoi", "source-path"], fallback="(未初期化)"))
    out.append("")
    out.append("## chezmoi status")
    out.append(_run_capture(["chezmoi", "status"], fallback="(未初期化またはエラー)"))
    return "\n".join(out) + "\n"


def _run_capture(args: list[str], *, fallback: str) -> str:
    try:
        result = run_subprocess(args, timeout=TOOL_VERSION_TIMEOUT_SEC)
    except SubprocessTimeoutError:
        return fallback
    except FileNotFoundError:
        return fallback
    if result.returncode != 0:
        return fallback
    return result.stdout.rstrip() or fallback


def _du_sh(path: Path) -> str:
    """`du -sh <path> | cut -f1` 相当."""
    try:
        result = run_subprocess(["du", "-sh", str(path)], timeout=TOOL_VERSION_TIMEOUT_SEC)
        if result.returncode == 0 and result.stdout:
            return result.stdout.split()[0]
    except FileNotFoundError:
        pass
    # フォールバック: バイト数
    try:
        size = path.stat().st_size
        return f"{size}B"
    except OSError:
        return "?"


# ── diff モード ──────────────────────────────────────────────────────────


def diff_snapshots(*, tarball_a: Path, tarball_b: Path | None) -> int:
    # ADR 013 (Windows first-class support): dir=None で OS デフォルトの
    # tempfile.gettempdir() に委ねる（Windows: %TEMP% / Linux: /tmp）。
    work_dir = Path(tempfile.mkdtemp(prefix="env-diff-"))
    try:
        a_dir = work_dir / "A"
        b_dir = work_dir / "B"
        a_dir.mkdir()
        b_dir.mkdir()

        print(f"==> 展開中: {tarball_a}")
        _extract_strip1(tarball_a, a_dir)

        if tarball_b is not None:
            print(f"==> 展開中: {tarball_b}")
            _extract_strip1(tarball_b, b_dir)
            label_a = tarball_a.name.removesuffix(".tar.gz")
            label_b = tarball_b.name.removesuffix(".tar.gz")
        else:
            print(f"==> tarball-B 未指定: カレント環境 ({_hostname_short()}) とライブ比較します")
            # カレント環境スナップショットを生成
            collect_snapshot()
            home = Path(os.environ.get("HOME", str(Path.home())))
            live_candidates = sorted(
                home.glob("env-snapshot-*.tar.gz"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not live_candidates:
                print("ERROR: ライブ tarball の生成に失敗しました", file=sys.stderr)
                return 1
            live_tar = live_candidates[0]
            _extract_strip1(live_tar, b_dir)
            label_a = tarball_a.name.removesuffix(".tar.gz")
            label_b = f"live-{_hostname_short()}"

        print()
        print("══════════════════════════════════════════════════")
        print(f"  比較: {label_a}  vs  {label_b}")
        print("══════════════════════════════════════════════════")

        # ファイル差分
        files_a = _collect_relative_files(a_dir / "files")
        files_b = _collect_relative_files(b_dir / "files")
        all_files = sorted(set(files_a) | set(files_b))

        print()
        print("── ファイル存在確認 ──")
        print(f"{'ファイル':<40}  {'A':<6}  {'B':<6}")
        print(f"{'--------':<40}  {'--':<6}  {'--':<6}")
        for f in all_files:
            has_a = "あり" if f in files_a else "なし"
            has_b = "あり" if f in files_b else "なし"
            marker = "  ← 片方のみ" if has_a != has_b else ""
            print(f"{'~/' + f:<40}  {has_a:<6}  {has_b:<6}{marker}")

        print()
        print("── ファイル内容の差分 ──")
        has_diff = False
        for f in all_files:
            fa = a_dir / "files" / f
            fb = b_dir / "files" / f
            if not fa.is_file() or not fb.is_file():
                continue
            if _files_differ(fa, fb):
                has_diff = True
                print()
                print(f"▼ ~/{f}")
                _print_diff(
                    fa,
                    fb,
                    label_a=f"{label_a}/~/{f}",
                    label_b=f"{label_b}/~/{f}",
                )
        if not has_diff:
            print("  (差分なし)")

        print()
        print("── バージョン比較 ──")
        _print_diff(
            a_dir / "versions.txt",
            b_dir / "versions.txt",
            label_a=f"{label_a}/versions.txt",
            label_b=f"{label_b}/versions.txt",
        )

        print()
        print("── chezmoi 状態比較 ──")
        _print_diff(
            a_dir / "chezmoi-status.txt",
            b_dir / "chezmoi-status.txt",
            label_a=f"{label_a}/chezmoi-status.txt",
            label_b=f"{label_b}/chezmoi-status.txt",
        )

        print()
        print("==> 比較完了")
        return 0
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _extract_strip1(tarball: Path, dest: Path) -> None:
    """`tar -xzf <tarball> --strip-components=1` 相当."""
    with tarfile.open(tarball, "r:gz") as tar:
        for member in tar.getmembers():
            # 先頭コンポーネントを剥がす
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            stripped = parts[1]
            member.name = stripped
            try:
                tar.extract(member, path=dest, filter="data")
            except TypeError:  # Python < 3.12 fallback
                tar.extract(member, path=dest)  # noqa: S202


def _collect_relative_files(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    out: list[str] = []
    for p in root.rglob("*"):
        if p.is_file():
            out.append(p.relative_to(root).as_posix())
    out.sort()
    return out


def _files_differ(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() != b.read_bytes()
    except OSError:
        return True


def _print_diff(a: Path, b: Path, *, label_a: str, label_b: str) -> None:
    """`diff -u --label <A> --label <B> <a> <b>` 相当の unified diff を出力する."""
    if not a.is_file() and not b.is_file():
        return
    args = [
        "diff",
        "-u",
        "--label",
        label_a,
        "--label",
        label_b,
        str(a) if a.is_file() else "/dev/null",
        str(b) if b.is_file() else "/dev/null",
    ]
    try:
        result = run_subprocess(args, timeout=TOOL_VERSION_TIMEOUT_SEC)
        # diff は exit 0=同一 / 1=差分あり（両方正常系）/ 2 以上=エラー
        if result.returncode >= 2:
            print(f"WARN: diff コマンドがエラー (exit={result.returncode}): {result.stderr}", file=sys.stderr)
        if result.stdout:
            print(result.stdout, end="")
    except FileNotFoundError:
        # diff コマンドが無い環境向けに Python フォールバック
        import difflib

        a_lines = a.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if a.is_file() else []
        b_lines = b.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if b.is_file() else []
        diff = difflib.unified_diff(a_lines, b_lines, fromfile=label_a, tofile=label_b)
        sys.stdout.write("".join(diff))
