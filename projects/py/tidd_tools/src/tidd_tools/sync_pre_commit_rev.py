"""`tidd sync-pre-commit-rev` サブコマンド (Issue #4051).

`pre-commit autoupdate`（cron workflow・Issue #5）はリポジトリ root の
`.pre-commit-config.yaml` しか更新しないため、copier で配布される
`templates/workflow/.pre-commit-config.yaml.jinja` の hook rev は誰も更新せず
古いまま残り続ける（#4024）。

`.jinja` は jinja 制御構文（`{% if primary_language == 'Python' %}` 等）を含むため
YAML としてパースできない。root を正として `- repo: <URL>` 直下の `rev:` 行のみを
正規表現で置換することで、jinja 構文を壊さずに書き換える（#4024 Phase 1 採用案）。

stdlib + pyyaml のみ使用（root 側の `.pre-commit-config.yaml` だけ YAML パースする）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

DEFAULT_ROOT_CONFIG = ".pre-commit-config.yaml"
DEFAULT_TEMPLATE_CONFIG = "templates/workflow/.pre-commit-config.yaml.jinja"

#: `templates/workflow` はテンプレート提供リポジトリ（本リポジトリ）にしか存在しない。
#: consumer 文脈ではこのメッセージを stderr に出力し exit 0 で no-op する
#: （`check_template_drift._MAINTAINER_ONLY_MESSAGE` / `sync_template._MAINTAINER_ONLY_MESSAGE` と同型・#3982）。
_MAINTAINER_ONLY_MESSAGE = (
    "==> tidd sync-pre-commit-rev: 本サブコマンドはテンプレート提供リポジトリ専用です"
    "（templates/workflow/ が見つからないため対象外・consumer では no-op）。"
)

# `- repo: <URL>` 行の直後の `rev: <VALUE>` 行にマッチする。
# root/`.jinja` いずれも「repo: の次行が rev:」というレイアウトで統一されているため、
# jinja 制御構文（`{% if %}` 等）を挟まずに直接一致させられる。
_REPO_REV_RE = re.compile(r"(?m)^(?P<prefix>[ \t]*-\s*repo:\s*(?P<url>\S+)\s*\n[ \t]*rev:\s*)(?P<rev>\S+)")


def _find_repo_root(start: Path) -> Path | None:
    for c in [start, *start.parents]:
        if (c / ".claude").is_dir() and (c / "templates" / "workflow").is_dir():
            return c
    return None


def parse_root_revs(root_config_text: str) -> dict[str, str]:
    """root `.pre-commit-config.yaml` の repo -> rev マッピングを返す.

    `repo: local` 等 rev を持たないエントリはスキップする。

    Raises:
        yaml.YAMLError: `root_config_text` が YAML としてパースできない場合。
    """
    data = yaml.safe_load(root_config_text)
    revs: dict[str, str] = {}
    for entry in (data or {}).get("repos") or []:
        url = entry.get("repo")
        rev = entry.get("rev")
        if url and rev:
            revs[url] = rev
    return revs


def sync_jinja_text(jinja_text: str, root_revs: dict[str, str]) -> tuple[str, list[tuple[str, str, str]]]:
    """`jinja_text` 内の `rev:` 行を `root_revs` の値へ同期する.

    `root_revs` に無い repo（`.jinja` にしか存在しないエントリ）はスキップし、
    rev が既に一致しているエントリも変更対象から除外する。

    Returns:
        `(更新後の jinja テキスト, 変更リスト [(repo_url, old_rev, new_rev), ...])`
    """
    changes: list[tuple[str, str, str]] = []

    def _replace(m: re.Match[str]) -> str:
        url = m.group("url")
        old_rev = m.group("rev")
        new_rev = root_revs.get(url)
        if new_rev is None or new_rev == old_rev:
            return m.group(0)
        changes.append((url, old_rev, new_rev))
        return m.group("prefix") + new_rev

    new_text = _REPO_REV_RE.sub(_replace, jinja_text)
    return new_text, changes


def run(*, repo_root: Path | None = None) -> int:
    root = repo_root if repo_root is not None else _find_repo_root(Path.cwd())
    if root is None:
        print(_MAINTAINER_ONLY_MESSAGE, file=sys.stderr)
        return 0

    template_path = root / DEFAULT_TEMPLATE_CONFIG
    if not template_path.is_file():
        print(_MAINTAINER_ONLY_MESSAGE, file=sys.stderr)
        return 0

    root_config_path = root / DEFAULT_ROOT_CONFIG
    try:
        root_config_text = root_config_path.read_text(encoding="utf-8")
        root_revs = parse_root_revs(root_config_text)
    except (OSError, yaml.YAMLError) as exc:
        print(f"ERROR: {root_config_path} のパースに失敗しました: {exc}", file=sys.stderr)
        return 1

    jinja_text = template_path.read_text(encoding="utf-8")
    new_text, changes = sync_jinja_text(jinja_text, root_revs)

    if not changes:
        print(f"==> tidd sync-pre-commit-rev: no changes（{template_path} は root と同期済みです）")
        return 0

    template_path.write_text(new_text, encoding="utf-8")
    for url, old_rev, new_rev in changes:
        print(f"    updated: {url} {old_rev} -> {new_rev}")
    print(f"==> tidd sync-pre-commit-rev: {len(changes)} 件の rev を更新しました（{template_path}）")
    return 0


def _handle(_args: argparse.Namespace) -> int:
    return run()


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "sync-pre-commit-rev",
        help=(
            "root .pre-commit-config.yaml の hook rev を "
            "templates/workflow/.pre-commit-config.yaml.jinja へ反映する (Issue #4051)。"
            "テンプレート提供リポジトリ専用。templates/workflow が無い場合は no-op で exit 0"
        ),
    )
    parser.set_defaults(func=_handle)
