"""OS 別のキャッシュ / 設定ディレクトリ解決（`platformdirs` ベース）.

- Linux / WSL: `~/.cache/tidd`
- macOS: `~/Library/Caches/tidd`
- Windows: `%LOCALAPPDATA%/tidd`

旧 bash 実装では `~/.cache/ai-reviewer` 等のハードコードがあったが、Windows ネイティブで
動かすため `platformdirs` で抽象化する。

Issue #3683: app 名を中立な `tidd` へ変更した（旧 `ai-dev-handbook`）。
旧 app 名のキャッシュディレクトリが存在し新パスが無い場合は、`cache_dir()` が
1 回だけ旧ディレクトリを新パスへ移行する（`_migrate_legacy_cache_dir`）。

**テストでの注意:** `_DIRS` はモジュール読み込み時に 1 度だけ生成される。
そのため `monkeypatch.setenv("XDG_CACHE_HOME", ...)` で環境変数を書き換えても
モジュール再 import まで反映されない。OS 別パスをテストで差し替えたい場合は
`monkeypatch.setattr(paths, "_DIRS", <stub>)` で `_DIRS` 自体を置換する
（`test_shared_paths.py::_StubDirs` がその実装例）。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Literal

from platformdirs import PlatformDirs

APP_NAME = "tidd"
# `appauthor=False` にすることで Windows の `%LOCALAPPDATA%/<author>/<name>/` 階層を
# 使わず `%LOCALAPPDATA%/tidd/` になる（上流 org 名がパスに露出しない・#3683）。
# mypy strict が platformdirs の引数型（`str | Literal[False] | None`）を検証するため
# `bool` ではなく `Literal[False]` で宣言する。
APP_AUTHOR: Literal[False] = False

_DIRS = PlatformDirs(appname=APP_NAME, appauthor=APP_AUTHOR, ensure_exists=False)


def _legacy_cache_dir() -> Path | None:
    """旧 app 名（ai-dev-handbook）のキャッシュディレクトリを返す.

    #3683 の既存キャッシュ移行用。新パス（`_DIRS.user_cache_dir`）の親ディレクトリに
    旧 app 名を並置して導出する（`.claude/hooks/_lib/hook_io.py` の旧
    `get_ai_dev_handbook_cache_dir()` と同一の場所を指す）。

    `Path.home()` ではなく `_DIRS` から導出する理由（#3683）: テストが `_DIRS` を
    差し替えた場合も新パスと同一 base になるため、実ユーザーの `~/.cache` へ
    移行処理が触れない。実環境では `_DIRS.user_cache_dir` が `Path.home()` 由来
    （または `$XDG_CACHE_HOME` 由来）のため、旧実装と同一パスを指す。
    Windows のみ旧実装どおり author 階層（`being-gaia-plan/`）を含む。
    """
    new_dir = Path(_DIRS.user_cache_dir)
    if sys.platform == "win32":
        return new_dir.parent / "being-gaia-plan" / "ai-dev-handbook"
    return new_dir.parent / "ai-dev-handbook"


def _copy_missing_only(src: Path, dst: Path) -> None:
    """src ディレクトリの中身を dst へ再帰コピーする（既存ファイルは上書きしない・#3689）.

    `shutil.copytree(..., dirs_exist_ok=True)` は既存ファイルを上書きしてしまうため
    （旧キャッシュの `timing-events/timing.db` が新キャッシュの統一事象ログを毎回
    復元する原因・#3689）、欠損ファイルのみコピーする独自マージを使う。
    ディレクトリは必要に応じて作成し、ファイルは dst 側に同名が無い場合のみコピーする。
    """
    if not dst.exists():
        dst.mkdir(parents=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _copy_missing_only(item, target)
        elif not target.exists():
            shutil.copy2(item, target)


def _migrate_legacy_cache_dir(new_dir: Path) -> None:
    """旧キャッシュディレクトリの中身を新パスへ移行する（冪等・#3683）.

    hook（bypass_audit 等）が先に新パスを作成している可能性があるため、
    新パスが無い場合は rename、既に存在する場合は copy-merge で欠損ファイルを
    移す。移行完了後は旧ディレクトリを削除し、以降の `cache_dir()` 呼び出しでは
    移行が再実行されないようにする（#3689・「1 回だけ移行」の意図どおり）。
    失敗しても握りつぶす（fail-open・キャッシュ喪失は許容）。
    """
    legacy = _legacy_cache_dir()
    if legacy is None or not legacy.is_dir():
        return
    if legacy == new_dir:
        return
    try:
        if not new_dir.exists():
            legacy.rename(new_dir)
            return
        for item in legacy.iterdir():
            target = new_dir / item.name
            if item.is_dir():
                _copy_missing_only(item, target)
            elif not target.exists():
                shutil.copy2(item, target)
        shutil.rmtree(legacy, ignore_errors=True)
    except OSError:
        pass


def cache_dir(*, create: bool = False) -> Path:
    """OS 別のキャッシュディレクトリを返す."""
    path = Path(_DIRS.user_cache_dir)
    _migrate_legacy_cache_dir(path)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def state_dir(*, create: bool = False) -> Path:
    """OS 別の状態ディレクトリを返す（ログ・PR 状態の永続化用）."""
    path = Path(_DIRS.user_state_dir)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir(*, create: bool = False) -> Path:
    """OS 別の設定ディレクトリを返す."""
    path = Path(_DIRS.user_config_dir)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir(*, create: bool = False) -> Path:
    """OS 別のログディレクトリを返す."""
    path = Path(_DIRS.user_log_dir)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def find_repo_root(start: Path) -> Path:
    """`start` から親方向に `.git` を探索してリポジトリルートを返す（Issue #1379）.

    `Path(__file__).resolve().parents[N]` はディレクトリ階層数を固定前提とする
    脆い実装で、以下のケースで壊れる:

    - mutmut isolated 実行（mutants/ 挿入で階層 +1）
    - テストファイルの物理配置変更
    - monorepo 化・ワークスペース再編

    このヘルパーは `.git` ディレクトリ（または `.git` ファイル: worktree の場合）を
    親方向に探索することで、階層数に依存せずに repo_root を解決する。

    Args:
        start: 探索の起点となるファイルまたはディレクトリ。

    Returns:
        `.git` を含む最も近い祖先ディレクトリ。

    Raises:
        FileNotFoundError: 親方向に `.git` が見つからない場合。
    """
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise FileNotFoundError(f"No .git directory found in any parent of {start!r}")


def resolve_repo_root() -> Path:
    """CWD 優先でリポジトリルートを解決する（Issue #2224）.

    `find_repo_root(Path(__file__))` はモジュールファイル起点のため、
    `uv tool install`（site-packages 配下に配置）された consumer 環境では
    必ず `FileNotFoundError` になる。tidd は常に対象リポジトリ内で実行される
    前提なので CWD 起点を優先し、CWD が repo 外の場合のみ従来どおり
    モジュール起点にフォールバックする（editable install の後方互換）。

    Returns:
        CWD（優先）またはこのモジュールから見つかったリポジトリルート。

    Raises:
        FileNotFoundError: CWD・モジュールのどちらからも `.git` が見つからない場合。
    """
    try:
        return find_repo_root(Path.cwd())
    except FileNotFoundError:
        return find_repo_root(Path(__file__))
