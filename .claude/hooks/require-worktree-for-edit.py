#!/usr/bin/env python3
"""PreToolUse hook: メインチェックアウト（main ブランチ）での Edit/Write をブロックする.

Issue #3445: worktree 未使用の直接実装（メインチェックアウトの main ブランチ上で
ファイルを直接編集・コミット）を防ぐ。PR #3444（Issue #3402）で実証された通り、
worktree を切らずに main を直接編集すると `record-timing-boundaries.py` が記録する
計測境界（`step2-implementation` / `step2-branch-created`）が欠落し、
`merge_summary.py` の「実装」フェーズ行が「計測不可」にすらならず行ごと消失する。
直近マージ 20 PR の監査でも 5/17 件（29%）が worktree 未使用の直接実装だったため、
文書（AGENTS.md）に頼らず hook で機械強制する。

ブロック条件（すべて満たすとき exit 2）:
  1. Edit / Write / apply_patch tool でファイルを編集する
  2. 対象ファイルの git リポジトリルートが `git worktree list --porcelain` の
     先頭エントリ（メインチェックアウト）と一致する
  3. メインチェックアウトの現在ブランチが `main`（保護ブランチ）
  4. 対象ファイルの拡張子が `.md` / `.txt` 以外（この 2 種は worktree なしでも
     直接編集を許可する）

対象外:
  - worktree 内での編集（リポジトリルートがメインチェックアウトと一致しない）
  - メインチェックアウトでも `.md` / `.txt` 拡張子の編集
  - メインチェックアウトが `main` 以外のブランチに切り替わっている場合
  - git リポジトリ外・git コマンド失敗時（判定不能 → 許可・fail-open）
  - セッション CWD のリポジトリと異なるリポジトリのファイル編集（Issue #3717）

Issue #3717: 本 hook は「セッションを開いたリポジトリ」の worktree 強制を目的とする。
Claude Code セッションが repo A で開かれている状態で repo B（別 git リポジトリ）の
ファイルを編集する場合、repo A のルールを repo B の操作へ適用しない。
対象ファイルのリポジトリがセッション CWD のリポジトリと異なる場合は対象外（許可）とする。
同一リポジトリの別 worktree は「同じリポジトリ」として扱う（common git dir 一致判定）。

escape hatch: 環境変数 `SKIP_REQUIRE_WORKTREE_FOR_EDIT=1` でブロックを全スキップする。

default OFF（opt-in・#2166）。`~/.config/tidd_tools/config.json` で有効化する。
stdlib のみ使用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.git_helpers import git_toplevel as _git_toplevel_shared
from _lib.git_helpers import run_git as _run_git
from _lib.hook_io import (
    get_file_path,
    get_tool_name,
    is_file_edit_tool,
    is_hook_enabled,
    read_hook_input,
)

#: メインチェックアウトでも直接編集を許可する拡張子（Issue #3445 採用案・拡張子ベース除外）.
_EXCLUDED_SUFFIXES = frozenset({".md", ".txt"})
#: 保護ブランチ（メインチェックアウトがこのブランチのときのみブロック）.
_PROTECTED_BRANCH = "main"
#: escape hatch（Issue #3717）: 1 を設定すると本 hook のブロックを全スキップする.
_ESCAPE_HATCH_ENV = "SKIP_REQUIRE_WORKTREE_FOR_EDIT"


def _git_toplevel(start_dir: str | None = None) -> str | None:
    """git リポジトリルートの絶対パスを返す（なければ None）."""
    return _git_toplevel_shared(cwd=start_dir, timeout=5)


def _repo_identity(repo_root: str) -> str | None:
    """リポジトリの同一性識別子（common git dir の絶対パス）を返す（Issue #3717）.

    `git rev-parse --git-common-dir` は同一リポジトリの全 worktree で共通の gitdir を
    返すため、worktree / メインチェックアウトをまたいだ「同一リポジトリ」判定に使える。
    相対パス（メインチェックアウトでは `.git`）は ``repo_root`` 起点に解決する。
    取得失敗時は None。
    """
    result = _run_git("rev-parse", "--git-common-dir", cwd=repo_root, timeout=5)
    if result is None or result.returncode != 0:
        return None
    common = result.stdout.strip()
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.join(repo_root, common)
    return os.path.normpath(os.path.abspath(common))


def _is_outside_session_repo(git_root: str) -> bool:
    """対象ファイルのリポジトリがセッション CWD のリポジトリと異なるかを返す（Issue #3717）.

    セッション CWD（process CWD）の git リポジトリルートを基準に、対象ファイルの
    リポジトリ（``git_root``）が「同じリポジトリ」でない場合 True を返す
    （本 hook の対象外として skip する）。同一リポジトリの別 worktree は同じ扱い。

    セッション CWD が git リポジトリ外・common git dir が判定不能の場合は False
    （従来挙動を維持し、自リポジトリの worktree 強制を弱めない）。
    """
    session_root = _git_toplevel()
    if session_root is None:
        return False
    session_identity = _repo_identity(session_root)
    file_identity = _repo_identity(git_root)
    if session_identity is None or file_identity is None:
        return False
    return session_identity != file_identity


def _resolve_start_dir(payload: dict, raw_path: str) -> str | None:
    """Edit/Write payload から git 探索起点を決定する（Issue #2454 と同型）.

    file_path は worktree 内の絶対パスとして渡されるのが通常のため、
    その親ディレクトリを最優先で使う（プロセス CWD＝メイン checkout に依存しない）。
    相対パスの場合のみ payload の cwd フィールドにフォールバックする。
    """
    if os.path.isabs(raw_path):
        return os.path.dirname(raw_path)
    payload_cwd = payload.get("cwd")
    if isinstance(payload_cwd, str) and payload_cwd:
        return payload_cwd
    return None


def _get_main_checkout(git_root: str) -> str | None:
    """`git worktree list --porcelain` の先頭 worktree（メインチェックアウト）パスを返す.

    git の仕様でメインチェックアウトが必ず先頭に来る。git コマンド失敗時は
    None（判定不能 → 許可）。
    """
    result = _run_git("worktree", "list", "--porcelain", cwd=git_root, timeout=5)
    if result is None or result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree ") :].strip() or None
    return None


def _current_branch(git_root: str) -> str | None:
    """指定リポジトリの現在ブランチを返す（detached HEAD 時は None）."""
    result = _run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=git_root, timeout=5)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _is_excluded_extension(file_path: str) -> bool:
    """対象ファイルの拡張子が除外対象（.md / .txt）かどうかを返す."""
    suffix = Path(file_path).suffix.lower()
    return suffix in _EXCLUDED_SUFFIXES


def _main() -> int:
    # escape hatch（Issue #3717）: 設定時はブロックを全スキップする
    if os.environ.get(_ESCAPE_HATCH_ENV) == "1":
        sys.stderr.write(
            f"require-worktree-for-edit: {_ESCAPE_HATCH_ENV}=1 によりバイパスされました。\n"
        )
        return 0

    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)
    if not is_file_edit_tool(tool_name):
        return 0

    raw_path = get_file_path(payload)
    if not raw_path:
        return 0

    # Issue #2454 と同型: worktree 内のファイルを検査できるよう探索起点を解決する
    start_dir = _resolve_start_dir(payload, raw_path)
    git_root = _git_toplevel(start_dir) or _git_toplevel()
    if not git_root:
        # git リポジトリ外・判定不能 → fail-open（許可）
        return 0

    # Issue #3717: 対象ファイルのリポジトリがセッション CWD のリポジトリと異なる場合は
    # 本 hook（自リポジトリの worktree 強制）の対象外として許可する
    if _is_outside_session_repo(git_root):
        return 0

    # メインチェックアウト判定: 対象ファイルのリポジトリルートが先頭 worktree と一致するか
    main_checkout = _get_main_checkout(git_root)
    if main_checkout is None:
        return 0  # 判定不能 → 許可
    if os.path.normpath(git_root) != os.path.normpath(main_checkout):
        return 0  # worktree 内での編集 → 許可

    # 保護ブランチ判定: メインチェックアウトの現在ブランチが main のときのみブロック
    branch = _current_branch(git_root)
    if branch != _PROTECTED_BRANCH:
        return 0

    # 拡張子除外: .md / .txt はメインチェックアウトでも直接編集を許可
    if _is_excluded_extension(raw_path):
        return 0

    sys.stderr.write(
        f"Blocked: メインチェックアウト（main ブランチ）でのコード編集は禁止されています: {raw_path}\n"
        "\n"
        "理由: TiDD ワークフローでは専用 worktree を切ってから実装します。\n"
        "     メインチェックアウトの main ブランチを直接編集すると計測境界\n"
        "     （step2-implementation / step2-branch-created）が欠落し、\n"
        "     merge_summary の実装フェーズが計測不能になります（Issue #3445・#3444）。\n"
        "\n"
        "解決手順:\n"
        "  1. `git worktree add -b <type>/issue-<N>-<slug> ../<repo>-issue-<N>-<slug> origin/main`\n"
        "     で専用 worktree を切る\n"
        "  2. worktree 内で編集・コミットする\n"
        "\n"
        "対象外:\n"
        "  - worktree 内での編集\n"
        "  - .md / .txt 拡張子の編集（メインチェックアウトでも許可）\n"
        "  - メインチェックアウトが main 以外のブランチの場合\n"
        "\n"
        "詳細: docs/reference/hooks.md#require-worktree-for-editpy\n"
    )
    return 2


def main() -> int:
    # Issue #1633: hook 機能別 on/off（opt-in・#2166）
    if not is_hook_enabled("require-worktree-for-edit"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
