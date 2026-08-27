"""`tidd screenshot-attach` サブコマンド（旧 `scripts/screenshot-attach.sh` の Python 移植）.

**対応 OS: WSL / Windows ネイティブのみ。** macOS・Linux ネイティブは未対応。

Windows / WSL 環境のスクリーンショットを WSL の Claude Code から参照・Markdown に添付する。

モード:
- `--latest`: 最新スクリーンショットのパスを stdout に出力する
- `--attach <MD>`: 指定 MD ファイルと同じディレクトリに最新スクリーンショットを webp で
  配置し、`<MD>` に `![<ALT>](./<basename>.webp)` を追記する

`--screenshot-dir` または `SCREENSHOT_DIR` 環境変数を指定すれば任意の OS でも動作する。
`cmd.exe` での Windows ユーザー名取得は WSL 限定（cmd.exe が PATH にある場合のみ）。
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
from pathlib import Path
from shutil import which

from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)

# 探索する画像拡張子（旧 sh と同じ大文字小文字無視）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "screenshot-attach",
        help="WSL/macOS/Windows のスクリーンショットを参照・MD に添付する（旧 scripts/screenshot-attach.sh）",
        description=__doc__,
    )
    add_common_flags(parser)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--latest",
        action="store_true",
        help="最新スクリーンショットのパスを stdout に出力する",
    )
    group.add_argument(
        "--attach",
        metavar="MD_FILE",
        help="指定 MD ファイルに最新スクリーンショットを添付する",
    )
    parser.add_argument(
        "--screenshot",
        metavar="IMG_FILE",
        help="添付するスクリーンショットを明示指定する（省略時は最新）",
    )
    parser.add_argument(
        "--screenshot-dir",
        metavar="DIR",
        dest="screenshot_dir",
        help="スクリーンショット保存先ディレクトリ（省略時は SCREENSHOT_DIR か自動推定）",
    )
    parser.add_argument(
        "--alt",
        default="スクリーンショット",
        help="MD 内の代替テキスト（省略時: スクリーンショット）",
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    screenshot_dir = resolve_screenshot_dir(arg_dir=args.screenshot_dir)

    if not screenshot_dir and not args.screenshot:
        # 未対応 OS の場合はその旨を明示する
        if not _is_supported_os():
            _print_unsupported_os_error()
        else:
            _print_screenshot_dir_error()
        return 1

    if args.latest:
        return _cmd_latest(screenshot_dir)
    if args.attach:
        dry_run = getattr(args, "dry_run", False)
        return _cmd_attach(
            md_file=Path(args.attach),
            screenshot_arg=args.screenshot,
            screenshot_dir=screenshot_dir,
            alt_text=args.alt,
            dry_run=dry_run,
        )
    print("ERROR: --latest または --attach を指定してください。", file=sys.stderr)
    return 1


# ── --latest ──────────────────────────────────────────────────────────────


def _cmd_latest(screenshot_dir: Path | None) -> int:
    if screenshot_dir is None:
        _print_screenshot_dir_error()
        return 1
    latest = get_latest_screenshot(screenshot_dir)
    if latest is None:
        print(
            f"ERROR: スクリーンショットディレクトリに画像ファイルが見つかりません: {screenshot_dir}",
            file=sys.stderr,
        )
        return 1
    print(latest)
    return 0


# ── --attach ──────────────────────────────────────────────────────────────


def _cmd_attach(
    *,
    md_file: Path,
    screenshot_arg: str | None,
    screenshot_dir: Path | None,
    alt_text: str,
    dry_run: bool = False,
) -> int:
    if not md_file.is_file():
        print(f"ERROR: MD ファイルが見つかりません: {md_file}", file=sys.stderr)
        return 1

    if screenshot_arg:
        src = Path(screenshot_arg)
    else:
        if screenshot_dir is None:
            _print_screenshot_dir_error()
            return 1
        latest = get_latest_screenshot(screenshot_dir)
        if latest is None:
            print(
                f"ERROR: スクリーンショットディレクトリに画像ファイルが見つかりません: {screenshot_dir}",
                file=sys.stderr,
            )
            return 1
        src = latest

    if not src.is_file():
        print(f"ERROR: スクリーンショットファイルが見つかりません: {src}", file=sys.stderr)
        return 1

    md_dir = md_file.parent
    dst_stem = src.stem
    dst_path = md_dir / f"{dst_stem}.webp"

    # --dry-run 指定時はファイルへの書き込み（webp 変換・MD 追記）をスキップする（Issue #2791）
    if dry_run:
        print(f"==> [dry-run] スクリーンショットを webp に変換: {src} -> {dst_path}", file=sys.stderr)
        print(f"==> [dry-run] MD に画像を追記: {md_file}", file=sys.stderr)
        print("==> [dry-run] 実際のファイル書き込みはスキップしました。", file=sys.stderr)
        return 0

    print(f"==> スクリーンショットを webp に変換中: {src} -> {dst_path}", file=sys.stderr)
    convert_to_webp(src=src, dst=dst_path)

    print(f"==> MD に画像を追記中: {md_file}", file=sys.stderr)
    with md_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n![{alt_text}](./{dst_path.name})\n")

    print(f"==> 完了: {dst_path} を {md_file} に添付しました。", file=sys.stderr)
    return 0


# ── SCREENSHOT_DIR 解決 ────────────────────────────────────────────────────


def detect_dir() -> Path | None:
    """OS 別に screenshot ディレクトリの推定パスを返す（SCREENSHOT_DIR 環境変数は参照しない）.

    対応 OS:
    - WSL (Linux + /proc/version に "microsoft"): ``/mnt/c/Users/<user>/Pictures/Screenshots``
    - Windows ネイティブ: ``%USERPROFILE%\\Pictures\\Screenshots``
    - 未対応 OS (macOS・Linux ネイティブ等): ``None`` を返す

    Note: このメソッドはディレクトリの存在チェックを行わない。``resolve_screenshot_dir``
    が存在チェックを担当する。
    """
    sys_platform = _get_current_platform()
    if sys_platform == "win32":
        # Windows ネイティブ: USERPROFILE 環境変数から構築
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            return Path(userprofile) / "Pictures" / "Screenshots"
        # USERPROFILE が未設定の場合は Path.home() でフォールバック
        return Path.home() / "Pictures" / "Screenshots"
    if sys_platform == "linux":
        # WSL チェック: /proc/version に "microsoft" が含まれる
        if _is_wsl():
            return _auto_detect_wsl_screenshot_dir()
        # Linux ネイティブは未対応
        return None
    # macOS その他未対応
    return None


def _get_current_platform() -> str:
    """テスト用 OS シミュレーション環境変数 `_TIDD_SCREENSHOT_SIM_OS` を考慮してプラットフォームを返す.

    実プロダクションでは ``sys.platform`` と同じ値を返す。
    テスト時に ``_TIDD_SCREENSHOT_SIM_OS=darwin`` 等を設定することで OS 分岐をシミュレートできる。
    """
    sim = os.environ.get("_TIDD_SCREENSHOT_SIM_OS")
    if sim:
        return sim.lower()
    return sys.platform


def _is_wsl() -> bool:
    """/proc/version の内容に "microsoft" が含まれるかどうかを確認する."""
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        return "microsoft" in content.lower()
    except OSError:
        return False


def _is_supported_os() -> bool:
    """現在の OS が screenshot-attach 対応環境かどうかを返す.

    対応環境: WSL (Linux + microsoft カーネル) / Windows ネイティブ
    """
    sys_platform = _get_current_platform()
    if sys_platform == "win32":
        return True
    if sys_platform == "linux":
        return _is_wsl()
    return False


def resolve_screenshot_dir(*, arg_dir: str | None) -> Path | None:
    """1: 引数 → 2: 環境変数 → 3: OS 別の自動推定の順に解決する.

    自動推定は OS 別に分岐する（Issue #1638）:
    - Windows ネイティブ: ``%USERPROFILE%\\Pictures\\Screenshots``
    - WSL (Linux + /proc/version に "microsoft"): ``cmd.exe`` 経由で Windows 側のパスを解決
    - macOS・Linux ネイティブ (非 WSL): None（未対応）
    """
    if arg_dir:
        return Path(arg_dir)
    env_dir = os.environ.get("SCREENSHOT_DIR")
    if env_dir:
        return Path(env_dir)
    auto = detect_dir()
    if auto is not None and auto.is_dir():
        return auto
    return None


def _auto_detect_wsl_screenshot_dir() -> Path | None:
    """WSL: `cmd.exe /C echo %USERNAME%` でユーザー名を取得して default パスを返す.

    日本語 Windows 環境では UNC パス cwd から起動された cmd.exe が cp932 エンコードの
    警告を stdout/stderr に出力することがある。`errors="replace"` でデコードし、
    非 UTF-8 出力でも `UnicodeDecodeError` で crash しないようにする（Issue #2982）。
    デコード不能（置換文字混入）または空のユーザー名は取得失敗として `None` を返し、
    呼び出し元の従来のフォールバックに委ねる。

    `cmd.exe` の出力は UTF-8 ではなくロケール依存（cp932 等）のため、
    `subprocess_runner.run()` の既定 `encoding="utf-8"`（Issue #3872）は明示的に
    `encoding=None` で上書きし、従来通りロケール依存デコードを使う。
    """
    if which("cmd.exe") is None:
        return None
    result = run_subprocess(
        ["cmd.exe", "/C", "echo %USERNAME%"], capture=True, check=False, encoding=None, errors="replace"
    )
    if result.returncode != 0:
        return None
    user = result.stdout.strip().replace("\r", "").replace("\n", "")
    if not user or "�" in user:
        return None
    return Path(f"/mnt/c/Users/{user}/Pictures/Screenshots")


def _auto_detect_screenshot_dir() -> Path | None:
    """OS 別に screenshot ディレクトリのデフォルトパスを推定する（後方互換 API）.

    .. deprecated::
        Issue #1638 以降は :func:`detect_dir` を使用してください。
        macOS の扱いが異なります（このメソッドは macOS で ``~/Pictures/Screenshots`` を
        返しますが、``detect_dir()`` は None を返します）。

    Windows ネイティブは ``Path.home()`` が ``%USERPROFILE%`` を返すため
    ``~/Pictures/Screenshots`` で正しく解決される。Linux (WSL) では
    ``cmd.exe`` 経由でホスト Windows のユーザー名を取得して ``/mnt/c/`` パスを
    構築する（従来動作を維持）。
    """
    system = platform.system()
    if system in ("Windows", "Darwin"):
        return Path.home() / "Pictures" / "Screenshots"
    # Linux (WSL) fallback
    return _auto_detect_wsl_screenshot_dir()


def _print_unsupported_os_error() -> None:
    """未対応 OS 向けの明示的エラーメッセージを出力する（Issue #1638）."""
    print(
        "ERROR: screenshot-attach は WSL / Windows ネイティブのみ対応です。",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print(
        "macOS・Linux ネイティブ環境では --screenshot-dir オプションまたは",
        file=sys.stderr,
    )
    print(
        "SCREENSHOT_DIR 環境変数でスクリーンショットディレクトリを明示指定してください:",
        file=sys.stderr,
    )
    print(
        "  tidd screenshot-attach --screenshot-dir <DIR> --latest",
        file=sys.stderr,
    )


def _print_screenshot_dir_error() -> None:
    print("ERROR: スクリーンショットディレクトリが見つかりません。", file=sys.stderr)
    print("", file=sys.stderr)
    print("以下のいずれかで設定してください:", file=sys.stderr)
    if _get_current_platform() == "win32":
        print(
            r"  1. 環境変数: set SCREENSHOT_DIR=%USERPROFILE%\Pictures\Screenshots",
            file=sys.stderr,
        )
        print(r"  2. PowerShell プロファイルに追記: $env:SCREENSHOT_DIR = ...", file=sys.stderr)
    else:
        print(
            "  1. 環境変数: export SCREENSHOT_DIR=/mnt/c/Users/<ユーザー名>/Pictures/Screenshots",
            file=sys.stderr,
        )
        print("  2. ~/.bashrc に追記: export SCREENSHOT_DIR=/path/to/screenshots", file=sys.stderr)
    print(
        "  3. --screenshot-dir オプション: tidd screenshot-attach --screenshot-dir <DIR>",
        file=sys.stderr,
    )


# ── 最新ファイル取得 ────────────────────────────────────────────────────


def get_latest_screenshot(directory: Path) -> Path | None:
    """`directory` 直下の画像ファイルから mtime 降順で最新を返す。なければ None."""
    if not directory.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    try:
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in IMAGE_EXTS:
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, entry))
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# ── webp 変換 ───────────────────────────────────────────────────────────────


def convert_to_webp(*, src: Path, dst: Path) -> None:
    """ImageMagick (magick / convert) または ffmpeg で webp 変換する.

    どれも見つからない・変換失敗時は元ファイルをコピー（旧 sh と同じ挙動）。
    """
    magick_cmd = _find_magick_cmd()
    if magick_cmd is not None:
        result = run_subprocess([magick_cmd, str(src), str(dst)], capture=True, check=False)
        if result.returncode == 0:
            return
        print("WARN: webp 変換に失敗しました。元ファイルをコピーします。", file=sys.stderr)
        _copy_file(src, dst)
        return

    if which("ffmpeg") is not None:
        result = run_subprocess(["ffmpeg", "-y", "-i", str(src), str(dst)], capture=True, check=False)
        if result.returncode == 0:
            return
        print("WARN: webp 変換に失敗しました。元ファイルをコピーします。", file=sys.stderr)
        _copy_file(src, dst)
        return

    print(
        "WARN: convert/magick/ffmpeg が見つかりません。元ファイルをコピーします。",
        file=sys.stderr,
    )
    _copy_file(src, dst)


def _find_magick_cmd() -> str | None:
    """ImageMagick の実行パスを返す（v7 magick → mise shim → v6 convert → mise shim convert の順）."""
    home = Path(os.environ.get("HOME", str(Path.home())))
    if which("magick") is not None:
        return "magick"
    mise_magick = home / ".local" / "share" / "mise" / "shims" / "magick"
    if mise_magick.is_file() and os.access(mise_magick, os.X_OK):
        return str(mise_magick)
    if which("convert") is not None:
        return "convert"
    mise_convert = home / ".local" / "share" / "mise" / "shims" / "convert"
    if mise_convert.is_file() and os.access(mise_convert, os.X_OK):
        return str(mise_convert)
    return None


def _copy_file(src: Path, dst: Path) -> None:
    from shutil import copyfile

    copyfile(src, dst)
