"""`tidd sync-template` サブコマンド (Issue #3417).

同じ hook / rule が root `.claude/**` と `templates/workflow/.claude/**` の
2 コピーで手動二重管理されており、片方だけ更新されると乖離が発生する
（#3401 の platformdirs 移行漏れが実害の例）。`check_template_drift.py` は
PR diff に含まれるファイルしか比較しないため、diff に含まれない過去の乖離は
永久に検知されない。

本モジュールは `templates/workflow/.claude/**` に既に存在する（=配布対象として
選定済みの）ファイル全件を走査し、root を正として template 側へ一方通行で
コピーする。意図的に内容が異なるファイル（consumer 向けに書き換え済みの rules
等）は `templates/workflow/_copier/sync-exempt.yaml` で除外する。

新規配布ファイルの追加（template 側にのみ存在するファイル）は本コマンドの対象外
（従来どおり人間・PR の判断）。

stdlib + pyyaml のみ使用。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

_TEMPLATE_CLAUDE_RELPATH = "templates/workflow/.claude"
_EXEMPT_LIST_RELPATH = "templates/workflow/_copier/sync-exempt.yaml"

#: `templates/workflow` はテンプレート提供リポジトリ（本リポジトリ）にしか存在しない。
#: consumer 文脈ではこのメッセージを stderr に出力し exit 0 で no-op する（Issue #3982）。
_MAINTAINER_ONLY_MESSAGE = (
    "==> tidd sync-template: 本サブコマンドはテンプレート提供リポジトリ専用です"
    "（templates/workflow/ が見つからないため対象外・consumer では no-op）。"
)


def _find_repo_root(start: Path) -> Path | None:
    for c in [start, *start.parents]:
        if (c / ".claude").is_dir() and (c / "templates" / "workflow").is_dir():
            return c
    return None


def _load_exempt_paths(repo_root: Path) -> set[str]:
    """sync-exempt.yaml に登録済みの `.claude/` 相対パス集合を返す.

    ファイル未存在・空・不正形式の場合は空集合を返す（fail-open。除外リストが
    読めなくても sync/check 自体は動作させる）。
    """
    exempt_path = repo_root / _EXEMPT_LIST_RELPATH
    if not exempt_path.is_file():
        return set()
    try:
        data = yaml.safe_load(exempt_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return set()
    if not isinstance(data, dict):
        return set()
    paths = data.get("exempt", [])
    if not isinstance(paths, list):
        return set()
    return {str(p) for p in paths}


def find_sync_targets(repo_root: Path) -> list[str]:
    """root と乖離している同期対象ファイルの `.claude/` 相対パス一覧を返す（ソート済み）.

    走査対象は `templates/workflow/.claude/**` に実在するファイルのみ
    （= 配布対象として選定済みのファイル）。以下は対象から除外する:

    - sync-exempt.yaml 登録済みパス（意図的な内容差分）
    - root 側に対応ファイルが存在しないパス（template 限定ファイル。
      新規配布ファイルの追加は本コマンドの対象外）
    - root と template で内容が一致しているパス（乖離なし）
    - `__pycache__/` 配下・`.pyc`/`.pyo`（gitignore 対象の Python バイトコード
      キャッシュ。ローカルでどのモジュールを import したかに応じて非決定的に
      内容が変わり、実ソースの乖離とは無関係な false positive を生む）
    """
    template_root = repo_root / _TEMPLATE_CLAUDE_RELPATH
    if not template_root.is_dir():
        return []
    exempt = _load_exempt_paths(repo_root)
    template_workflow_root = repo_root / "templates" / "workflow"

    targets: list[str] = []
    for template_file in template_root.rglob("*"):
        if not template_file.is_file():
            continue
        if "__pycache__" in template_file.parts or template_file.suffix in (".pyc", ".pyo"):
            continue
        claude_path = template_file.relative_to(template_workflow_root).as_posix()
        if claude_path in exempt:
            continue
        root_file = repo_root / claude_path
        if not root_file.is_file():
            continue
        if root_file.read_bytes() != template_file.read_bytes():
            targets.append(claude_path)
    return sorted(targets)


def sync(repo_root: Path) -> list[str]:
    """乖離している対象ファイルを root → template へ一方通行でコピーする.

    Returns:
        コピーした `.claude/` 相対パスの一覧（ソート済み）。
    """
    targets = find_sync_targets(repo_root)
    for claude_path in targets:
        root_file = repo_root / claude_path
        template_file = repo_root / "templates" / "workflow" / claude_path
        template_file.write_bytes(root_file.read_bytes())
    return targets


def run(*, check: bool, repo_root: Path | None = None) -> int:
    root = repo_root if repo_root is not None else _find_repo_root(Path.cwd())
    if root is None:
        print(_MAINTAINER_ONLY_MESSAGE, file=sys.stderr)
        return 0

    if check:
        targets = find_sync_targets(root)
        if not targets:
            print("==> tidd sync-template --check: 乖離なし", file=sys.stderr)
            return 0
        print(
            f"==> ERROR: templates/workflow/.claude/** が root と乖離しています ({len(targets)} 件)",
            file=sys.stderr,
        )
        for claude_path in targets:
            print(f"    drift: {claude_path}", file=sys.stderr)
        print(
            "\n    Fix: `tidd sync-template` を実行して templates/workflow 側を root に同期し、コミットしてください。",
            file=sys.stderr,
        )
        return 1

    synced = sync(root)
    if not synced:
        print("==> tidd sync-template: 同期対象なし（乖離なし）", file=sys.stderr)
    else:
        print(f"==> tidd sync-template: {len(synced)} 件のファイルを同期しました", file=sys.stderr)
        for claude_path in synced:
            print(f"    synced: {claude_path}", file=sys.stderr)
    return 0


def _handle(args: argparse.Namespace) -> int:
    return run(check=args.check)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "sync-template",
        help=(
            "root .claude/** の変更を templates/workflow/.claude/** へ一方通行で機械コピーする (Issue #3417)。"
            "テンプレート提供リポジトリ専用。templates/workflow が無いリポジトリでは no-op で exit 0 (Issue #3982)"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="コピーせず乖離のみ検出する（乖離があれば stderr に一覧を出力し exit 1）",
    )
    parser.set_defaults(func=_handle)
