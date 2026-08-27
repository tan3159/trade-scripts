"""``~/.config/tidd_tools/config.json`` 読み書き + ``hook-groups.yaml`` 解析の単一ソース（Issue #2947）.

``config.py``・``configure.py``・``ai_review/backends.py``・``ai_review/yaru_auto_tick.py`` の
4 箇所に独立実装されていた以下のロジックを集約する:

- config.json のパス解決（win32: ``APPDATA``、POSIX: ``XDG_CONFIG_HOME``、
  どちらも未設定なら ``~/.config``）
- config.json の読み込み / 書き込み（存在しない・不正 JSON は空 dict 扱い）
- 旧 ``hooks-config.json`` → ``config.json`` への migration（冪等）
- ``.claude/rules/hook-groups.yaml`` の手動パース（stdlib のみ、yaml ライブラリ非依存）
- リポジトリルート探索（cwd 優先、モジュール起点フォールバック。
  ``shared/paths.py`` の ``resolve_repo_root()`` に委譲）
- リポジトリ root の ``.tidd/config.json`` 読み書き（Issue #3569: マシン設定への
  ローカル限定オーバーライドレイヤ。優先順位はリポジトリ > マシン > default）

**``shared/paths.py`` の ``config_dir()`` と統合しなかった理由:** ``config_dir()`` は
``platformdirs`` 経由で appname="ai-dev-handbook" のディレクトリ（例:
``~/.config/ai-dev-handbook``）を返すのに対し、本モジュールが扱う config.json は
appname="tidd_tools" 固定（``~/.config/tidd_tools/config.json``）で、旧 3 実装・
既存ユーザーのオンディスク設定ファイル・大量の既存テストがこのパスに依存している。
アプリ名が異なる別ディレクトリのため置き換えは互換性を壊す。リポジトリルート探索
（``find_repo_root()``）のみ ``shared/paths.py`` の ``resolve_repo_root()`` に委譲する。

**hooks 側との関係:** ``.claude/hooks/_lib/hook_io.py`` にも同一のパス解決ロジックが
あるが、hooks は stdlib のみで動く制約（`tidd_tools` パッケージに依存できない）があり
対象外。挙動の同期は契約テスト（``tests/test_hook_io.py`` 等）で保証する。

**``resolve_config_path()`` と ``config_path()`` の違い（Issue #1994 由来の決定性要件）:**
``yaru_auto_tick.read_mode()`` は ``env={}`` のような明示的な空 dict を渡すテストが
多数あり、その場合に実システムの ``$HOME`` へフォールバックしてしまうと
テストの決定性が壊れる（実行環境の実ファイルを誤って読みに行く）。そのため
明示的に env dict を渡す用途向けに ``resolve_config_path(env)``
（システムフォールバックなし・解決不能なら ``None``）を用意し、
``os.environ`` を暗黙に使う用途向けに ``config_path()``
（``Path.home()`` へ必ずフォールバックし常に ``Path`` を返す）を分けている。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tidd_tools.shared.paths import resolve_repo_root

# ── config.json パス解決 ──────────────────────────────────────────────────────


def resolve_config_path(env: Mapping[str, str]) -> Path | None:
    """渡された ``env`` mapping のみから config.json のパスを解決する.

    ``Path.home()`` 等の実システムへのフォールバックを行わない。``env`` に
    パス解決に必要なキー（``APPDATA``/``XDG_CONFIG_HOME``/``HOME``）が
    存在しない場合は ``None`` を返す（テストで空 dict を渡した際に実システムの
    設定ファイルを誤って参照しないための決定性保証）。
    """
    if sys.platform == "win32":
        appdata = env.get("APPDATA") or ""
        if appdata:
            return Path(appdata) / "tidd_tools" / "config.json"
        home = env.get("HOME") or ""
        return Path(home) / ".config" / "tidd_tools" / "config.json" if home else None

    xdg = env.get("XDG_CONFIG_HOME") or ""
    if xdg:
        return Path(xdg) / "tidd_tools" / "config.json"
    home = env.get("HOME") or ""
    return Path(home) / ".config" / "tidd_tools" / "config.json" if home else None


def config_path() -> Path:
    """OS ネイティブ config ディレクトリの config.json パスを返す（``os.environ`` 使用）.

    ``APPDATA``/``XDG_CONFIG_HOME``/``HOME`` のいずれも未設定でも ``Path.home()`` へ
    フォールバックし、必ず ``Path`` を返す（従来 3 実装の動作を踏襲）。
    """
    resolved = resolve_config_path(os.environ)
    if resolved is not None:
        return resolved
    return Path.home() / ".config" / "tidd_tools" / "config.json"


def _legacy_config_path(cfg_path: Path) -> Path:
    """旧 hooks-config.json のパスを返す（migration 用）."""
    return cfg_path.parent / "hooks-config.json"


# ── config.json 読み書き ──────────────────────────────────────────────────────


def migrate_if_needed() -> None:
    """hooks-config.json → config.json への migration（存在するときのみ・冪等）.

    Issue #2359: 初回実行時に旧ファイルを新パスへ自動 mv する。
    config.json がすでに存在する場合は mv しない（冪等）。
    """
    cfg_path = config_path()
    legacy_path = _legacy_config_path(cfg_path)

    if legacy_path.is_file() and not cfg_path.is_file():
        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.rename(cfg_path)
            sys.stderr.write(f"migration: {legacy_path} → {cfg_path}\n")
        except OSError as e:
            sys.stderr.write(f"WARN: hooks-config.json の migration に失敗しました: {e}\n")


def read_config() -> dict[str, Any]:
    """config.json を読み込んで dict として返す（存在しない・不正 JSON は空 dict）.

    読み込み前に :func:`migrate_if_needed` を実行する。
    """
    migrate_if_needed()
    cfg_path = config_path()
    if not cfg_path.is_file():
        return {}
    try:
        result: dict[str, Any] = json.loads(cfg_path.read_text(encoding="utf-8"))
        return result
    except (json.JSONDecodeError, OSError):
        return {}


def write_config(config: dict[str, Any]) -> None:
    """config.json に dict を書き込む（親ディレクトリを自動作成）."""
    cfg_path = config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ── hook-groups.yaml 解析 ─────────────────────────────────────────────────────


def find_repo_root() -> Path | None:
    """リポジトリルートを検索して返す（見つからない場合は None）.

    ``shared/paths.py`` の ``resolve_repo_root()``（cwd 優先・モジュール起点フォールバック）
    に委譲する。
    """
    try:
        return resolve_repo_root()
    except FileNotFoundError:
        return None


def _parse_hook_groups_yaml(content: str) -> list[dict[str, Any]]:
    """hook-groups.yaml を手動パースしてグループリストを返す（stdlib のみ）.

    yaml ライブラリの依存を追加しないため、固定フォーマットに依存した
    単純なステートマシン方式でパースする。
    """
    groups: list[dict[str, Any]] = []
    current_group: dict[str, Any] | None = None
    in_hooks = False

    for raw_line in content.splitlines():
        line = raw_line.rstrip()

        if not line.strip() or line.strip().startswith("#"):
            continue

        if line.strip().startswith("- name:"):
            if current_group:
                groups.append(current_group)
            current_group = {
                "name": line.split(":", 1)[1].strip(),
                "label": "",
                "description": "",
                "hooks": [],
            }
            in_hooks = False
            continue

        if current_group is None:
            continue

        stripped = line.strip()

        if stripped.startswith("label:"):
            current_group["label"] = stripped.split(":", 1)[1].strip().strip('"')
            in_hooks = False
        elif stripped.startswith("description:"):
            current_group["description"] = stripped.split(":", 1)[1].strip().strip('"')
            in_hooks = False
        elif stripped == "hooks:":
            in_hooks = True
        elif in_hooks and stripped.startswith("- "):
            hook_name = stripped[2:].strip()
            current_group["hooks"].append(hook_name)

    if current_group:
        groups.append(current_group)

    return groups


def load_hook_groups() -> list[dict[str, Any]]:
    """``.claude/rules/hook-groups.yaml`` を読み込んでグループリストを返す.

    リポジトリルートまたは hook-groups.yaml が見つからない場合は空リストを返す。
    """
    repo_root = find_repo_root()
    if repo_root is None:
        return []

    yaml_path = repo_root / ".claude" / "rules" / "hook-groups.yaml"
    if not yaml_path.is_file():
        return []

    return _parse_hook_groups_yaml(yaml_path.read_text(encoding="utf-8"))


def get_all_hook_names() -> list[str]:
    """hook-groups.yaml に登録されているすべての hook 名を返す."""
    groups = load_hook_groups()
    names: list[str] = []
    for group in groups:
        names.extend(group.get("hooks", []))
    return names


# ── リポジトリ単位オーバーライド（Issue #3569） ────────────────────────────────
#
# `~/.config/tidd_tools/config.json`（マシン単位）に対して、リポジトリ root の
# `.tidd/config.json` を上書きレイヤとして重ねる。`.tidd/` は既にリポジトリ全体が
# `.gitignore`（#2311）で git 管理対象外になっており、ローカル限定（チーム共有なし）
# の設定として使う。優先順位: リポジトリ > マシン > default。


def repo_config_path(repo_root: Path | None = None) -> Path | None:
    """リポジトリ root の ``.tidd/config.json`` パスを返す（Issue #3569）.

    Args:
        repo_root: 明示指定時はこのパスを使う。未指定時は :func:`find_repo_root` で解決する。

    Returns:
        リポジトリルートが見つからない場合は ``None``。
    """
    root = repo_root if repo_root is not None else find_repo_root()
    if root is None:
        return None
    return root / ".tidd" / "config.json"


def read_repo_config(repo_root: Path | None = None) -> dict[str, Any]:
    """リポジトリ ``.tidd/config.json`` を読み込んで dict として返す（Issue #3569）.

    ファイルなし・リポジトリルート未検出は空 dict を返す。不正 JSON の場合は
    stderr に ``WARN`` とファイルパスを出力して空 dict を返す（呼び出し側は
    マシン設定へフォールバックする）。
    """
    cfg_path = repo_config_path(repo_root)
    if cfg_path is None or not cfg_path.is_file():
        return {}
    try:
        raw = cfg_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write(
            f"WARN: {cfg_path} のパースに失敗しました（不正な JSON）。マシン設定へフォールバックします。\n"
        )
        return {}
    if not isinstance(result, dict):
        return {}
    return result


def write_repo_config(config: dict[str, Any], repo_root: Path | None = None) -> Path:
    """リポジトリ ``.tidd/config.json`` に dict を書き込む（Issue #3569）.

    Raises:
        RuntimeError: リポジトリルートが見つからない場合。
    """
    cfg_path = repo_config_path(repo_root)
    if cfg_path is None:
        raise RuntimeError("リポジトリルートが見つかりません（.git が見つかりません）")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return cfg_path


def delete_repo_config(repo_root: Path | None = None) -> bool:
    """リポジトリ ``.tidd/config.json`` を削除する（Issue #3569）.

    Returns:
        削除した場合 ``True``。ファイルが存在しなかった場合 ``False``。
    """
    cfg_path = repo_config_path(repo_root)
    if cfg_path is None or not cfg_path.is_file():
        return False
    cfg_path.unlink()
    return True


def read_effective_config(repo_root: Path | None = None) -> dict[str, Any]:
    """マシン設定にリポジトリ設定を重ねた実効設定を返す（Issue #3569）.

    優先順位: リポジトリ > マシン > default。``tidd config show``（``config.py``）の
    出所判定と同じマージ規則（``{**machine_config, **repo_config}``）を、
    実行時の設定読み込み（``ai_review/backends.py``・``propose_step.py`` 等）でも
    共通で使えるようにするヘルパー。
    """
    return {**read_config(), **read_repo_config(repo_root)}
