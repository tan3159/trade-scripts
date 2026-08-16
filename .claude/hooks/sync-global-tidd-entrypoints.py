#!/usr/bin/env python3
"""SessionStart hook: グローバル tidd tool の entry-points を自動同期する.

Issue #2475: `projects/py/tidd_tools/pyproject.toml` の
`[project.entry-points."tidd_tools.commands"]` とグローバル tidd tool
（`uv tool install --editable` 済み）の `entry_points.txt` を比較し、
差分がある場合のみ `uv tool upgrade tidd-tools` を自動実行する。

**動作条件:**
- 機能キー `sync-global-tidd-entrypoints` が config.json で有効化されている
- グローバル tidd tool が `~/.local/share/uv/tools/tidd-tools/` に存在する
- グローバル tidd tool が editable install（`direct_url.json` に `"editable": true`）である

上記のいずれかが満たされない場合は silent early return（exit 0）する。
consumer 環境等でグローバル tidd が存在しない場合も同様。

Issue #3768: 上記の editable install 同期に加え、consumer が uvx ラッパー方式
（`~/.local/bin/tidd` に固定 spec タグを埋め込んだシェルスクリプトを配置する運用・
`docs/setup/copier-workflow-adoption.md` §3.2）を使っている場合、ラッパーの spec タグと
`.copier-answers.yml` の `_commit` の不一致を検知して stderr に通知する
（`_lib/tidd_uvx.py` の `check_uvx_wrapper_tag_sync()` に判定ロジックを委譲。
このチェック自体はセッションをブロックしない・exit code に影響しない）。

stdlib のみ使用。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.git_helpers import git_toplevel
from _lib.hook_io import is_hook_enabled, read_hook_input
from _lib.tidd_uvx import check_uvx_wrapper_tag_sync

_DIST_INFO_GLOB = "tidd_tools-*.dist-info"


def _tool_env_root() -> Path:
    """グローバル tidd tool の dist-info が置かれるディレクトリを返す.

    Issue #3915: 以前はモジュールトップレベルで `Path.home()` を評価する定数
    （`_TOOL_ENV_ROOT`）だったため、import 時点（テストの HOME/USERPROFILE 隔離が
    適用される前）の実ユーザーのホームを捕捉してしまい、hook 関数を in-process で
    直接 import して呼ぶ契約テストで後続の隔離が反映されなくなる可能性があった
    （`health_check.py` の `_global_tool_env_root()` と同型の修正・#3894）。
    呼び出し時に遅延評価する関数へ変更する。
    """
    return Path.home() / ".local" / "share" / "uv" / "tools" / "tidd-tools"


def _find_global_entry_points_txt() -> Path | None:
    """グローバル tidd tool の `entry_points.txt` パスを返す.

    dist-info ディレクトリが存在しない場合、または editable install でない場合は None を返す。
    editable install 判定は `direct_url.json` の `dir_info.editable` で行う。
    """
    site_packages = _tool_env_root() / "lib"
    if not site_packages.is_dir():
        return None

    # `lib/pythonX.XX/site-packages/tidd_tools-*.dist-info/` を探す
    for python_dir in site_packages.iterdir():
        sp = python_dir / "site-packages"
        if not sp.is_dir():
            continue
        dist_info_dirs = list(sp.glob(_DIST_INFO_GLOB))
        if not dist_info_dirs:
            continue
        dist_info = dist_info_dirs[0]

        # editable install であることを確認（consumer 環境では editable でない）
        direct_url_path = dist_info / "direct_url.json"
        if direct_url_path.is_file():
            try:
                data = json.loads(direct_url_path.read_text(encoding="utf-8"))
                dir_info = data.get("dir_info", {})
                if not dir_info.get("editable", False):
                    # editable install でない → consumer 環境と見なして silent skip
                    return None
            except (json.JSONDecodeError, OSError):
                return None

        ep_txt = dist_info / "entry_points.txt"
        if ep_txt.is_file():
            return ep_txt

    return None


def _find_pyproject_toml() -> Path | None:
    """リポジトリ内の `projects/py/tidd_tools/pyproject.toml` を探す.

    hook の CWD から git root を解決し（Issue #2958: `_lib/git_helpers.py` の
    `git_toplevel()` に委譲）、その下の固定パスを返す。存在しない場合は None。
    """
    root_str = git_toplevel(timeout=5)
    if root_str is None:
        return None
    root = Path(root_str)
    candidate = root / "projects" / "py" / "tidd_tools" / "pyproject.toml"
    return candidate if candidate.is_file() else None


def _parse_pyproject_entry_points(pyproject_path: Path) -> dict[str, str]:
    """pyproject.toml の `[project.entry-points."tidd_tools.commands"]` セクションを解析し.

    「サブコマンド名 -> モジュールパス」の dict を返す。
    `tomllib` は Python 3.11+ 標準ライブラリを使用する。
    """
    try:
        import tomllib

        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except (ImportError, OSError, tomllib.TOMLDecodeError):
        return {}

    project = data.get("project", {})
    entry_points = project.get("entry-points", {})
    commands = entry_points.get("tidd_tools.commands", {})
    return dict(commands)


def _parse_entry_points_txt(ep_txt_path: Path) -> dict[str, str]:
    """グローバル tidd の `entry_points.txt` の `[tidd_tools.commands]` セクションを解析し.

    「サブコマンド名 -> モジュールパス」の dict を返す。INI 形式を手動で解析（stdlib
    configparser でも可だが importlib.metadata の ConfigParser と同じ方法で実装する）。
    """
    try:
        content = ep_txt_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    result: dict[str, str] = {}
    in_section = False
    for line in content.splitlines():
        line = line.strip()
        if line == "[tidd_tools.commands]":
            in_section = True
            continue
        if line.startswith("[") and line.endswith("]"):
            in_section = False
            continue
        if in_section and "=" in line:
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip()
            if name:
                result[name] = value
    return result


def _run_upgrade() -> int:
    """uv tool upgrade tidd-tools を実行して exit code を返す."""
    try:
        result = subprocess.run(
            ["uv", "tool", "upgrade", "tidd-tools"],
            capture_output=False,
            check=False,
        )
        return result.returncode
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(
            f"sync-global-tidd-entrypoints: upgrade 実行に失敗しました: {e}\n"
        )
        return 1


def _notify_uvx_wrapper_mismatch() -> None:
    """uvx ラッパーの spec タグ不一致を検知したら stderr に通知する（Issue #3768）.

    editable install の有無に関わらず（consumer 環境で最も必要になるため）実行する。
    判定不能・一致していれば何も出力しない。exit code には影響させない。
    """
    root_str = git_toplevel(timeout=5)
    if root_str is None:
        return
    message = check_uvx_wrapper_tag_sync(repo_root=Path(root_str))
    if message is not None:
        sys.stderr.write(message + "\n")


def main() -> int:
    """メインエントリポイント."""
    if not is_hook_enabled("sync-global-tidd-entrypoints"):
        return 0

    # Issue #3895 レビュー指摘: 本 hook は payload を使わないが、stdout/stderr を
    # UTF-8 へ reconfigure する処理（`_ensure_utf8_streams()`）が `read_hook_input()`
    # 経由でしか呼ばれないため、ここを経由しないと `_notify_uvx_wrapper_mismatch()` の
    # 日本語 stderr 出力が cp932 環境で文字化けしたまま出力されてしまう。
    read_hook_input(hook_name="SessionStart")

    _notify_uvx_wrapper_mismatch()

    ep_txt_path = _find_global_entry_points_txt()
    if ep_txt_path is None:
        # グローバル tidd が存在しない、または editable install でない → silent skip
        return 0

    pyproject_path = _find_pyproject_toml()
    if pyproject_path is None:
        # pyproject.toml が見つからない → silent skip
        return 0

    pyproject_eps = _parse_pyproject_entry_points(pyproject_path)
    ep_txt_eps = _parse_entry_points_txt(ep_txt_path)

    if pyproject_eps == ep_txt_eps:
        # 差分なし（サブコマンド名・モジュールパス両方一致）→ no-op
        return 0

    # 差分あり → uv tool upgrade tidd-tools を実行
    return _run_upgrade()


if __name__ == "__main__":
    sys.exit(main())
