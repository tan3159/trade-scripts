"""`tidd lint-hardcoded-repo` サブコマンド（旧 `scripts/lint-hardcoded-repo.sh` の Python 移植）.

リポジトリ名（`being-gaia-plan` / `ai-dev-handbook`）がコード内にハードコード
されていないかを検出する（Issue #336）。

Issue #2944: 検出ロジック（パターン・除外リスト・コメント行判定・
`.claude/rules/hardcoded-patterns.yaml` 追加パターン）は
`.claude/hooks/_lib/hardcoded_repo.py` を単一の真実源として動的 import で参照する
（`.claude/hooks/ban-hardcoded-repo.py` hook と共有）。以前は本モジュールが
独自に古い除外リストを再定義しており、hook と判定が乖離していた
（コメント行スキップ・YAML 追加パターン対応が欠落）。ドリフト防止のため、
tidd_tools 側でパターン・除外リストを再定義してはならない。

検索対象: `scripts/` / `agt/` / `.claude/` 配下のファイル

終了コード:
- 0: ハードコードなし（OK）
- 1: ハードコードを検出（NG）
- 2: 実行エラー（ディレクトリ不在・`_lib/hardcoded_repo.py` 読み込み失敗等）
"""

from __future__ import annotations

import argparse
import logging
import sys
import types
from pathlib import Path
from typing import cast

from tidd_tools.shared import git_client
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GitCommandError

logger = logging.getLogger(__name__)

SEARCH_DIRS = ("scripts", "agt", ".claude")


def _load_hardcoded_repo_lib() -> types.ModuleType:
    """`.claude/hooks/_lib/hardcoded_repo.py` を動的 import する（単一ソース化・Issue #2944）.

    tidd_tools は project 側なので、リポジトリルート起点の相対 import は使えない。
    `.claude/hooks/ban-hardcoded-repo.py` hook と同一の検出ロジック（パターン・
    除外リスト・コメント行判定・YAML 追加パターン）を共有するため、動的 import で
    橋渡しする（`tidd_tools.ai_review.verify_ai_confirm._load_session_detector` と同型）。
    """
    from tidd_tools.shared.paths import resolve_repo_root

    repo_root = resolve_repo_root()
    lib_dir = repo_root / ".claude" / "hooks" / "_lib"
    if not lib_dir.is_dir():
        raise FileNotFoundError(f"_lib ディレクトリが見つかりません: {lib_dir}")
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    import hardcoded_repo  # type: ignore[import-not-found]

    return cast(types.ModuleType, hardcoded_repo)


_hardcoded_repo_lib = _load_hardcoded_repo_lib()

# 後方互換のためモジュール属性としても公開する（既存呼び出し元・テスト互換）。
# `_lib/hardcoded_repo.py` が単一の真実源であり、値をここで再定義してはならない
# （再定義すると本 Issue #2944 が修正した乖離が再発する）。
PATTERNS = _hardcoded_repo_lib.PATTERNS
EXCLUDE_BASENAMES = _hardcoded_repo_lib.EXCLUDE_BASENAMES
EXCLUDE_PATH_FRAGMENTS = _hardcoded_repo_lib.EXCLUDE_PATH_FRAGMENTS
EXCLUDE_SUFFIXES = _hardcoded_repo_lib.EXCLUDE_SUFFIXES


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "lint-hardcoded-repo",
        help="scripts/ agt/ .claude/ 配下のリポジトリ名ハードコードを検出する",
        description=__doc__,
    )
    parser.add_argument(
        "target_dir",
        nargs="?",
        default=None,
        help="検索対象ディレクトリ（省略時はリポジトリルート）",
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    target = _resolve_target(args.target_dir)
    if target is None:
        return 2

    matches = scan(target)
    if not matches:
        return 0

    for m in matches:
        print(m, file=sys.stderr)
    print(
        f"\nERROR: リポジトリ名のハードコードを {len(matches)} 件検出しました。\n"
        "正しい書き方: `gh repo view --json nameWithOwner -q .nameWithOwner` "
        "で動的に取得してください。",
        file=sys.stderr,
    )
    return 1


def scan(target_dir: Path) -> list[str]:
    """ディレクトリを走査してパターンマッチ行を返す.

    除外判定（basename・path・コメント行）と `.claude/rules/hardcoded-patterns.yaml`
    の追加パターンは `_lib/hardcoded_repo.py` を単一ソースとして
    `ban-hardcoded-repo.py` hook と同一ロジックで適用する（Issue #2944）。

    Returns:
        `"<rel_path>:<line_no>:<line>"` 形式の文字列リスト。
    """
    lib = _hardcoded_repo_lib
    compiled_yaml = lib.compile_yaml_patterns(lib.load_yaml_patterns(str(target_dir)))

    out: list[str] = []
    for sub in SEARCH_DIRS:
        base = target_dir / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(target_dir))
            rel_with_sep = f"/{rel}"
            if lib.is_excluded_path(rel_with_sep):
                continue
            if lib.is_excluded_basename(path.name):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for lineno, line in enumerate(lines, start=1):
                if lib.line_contains_pattern(line) is None and lib.find_yaml_pattern_match(line, compiled_yaml) is None:
                    continue
                out.append(f"{rel}:{lineno}:{line.rstrip()}")
    return out


def _resolve_target(override: str | None) -> Path | None:
    if override:
        p = Path(override).resolve()
        if not p.is_dir():
            print(f"ERROR: ディレクトリが存在しません: {p}", file=sys.stderr)
            return None
        return p
    try:
        return git_client.rev_parse_show_toplevel()
    except GitCommandError as exc:
        print(f"ERROR: git rev-parse 失敗: {exc}", file=sys.stderr)
        return None
