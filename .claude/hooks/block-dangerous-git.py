#!/usr/bin/env python3
"""PreToolUse hook: 危険な git 操作をブロックする.

旧 block-dangerous-git.sh を 1:1 で踏襲する（Phase 4 / #1057 で Python 化）。

ブロック対象:
  - git push --force / -f （ただし --force-with-lease / --force-if-includes は許可）
  - git reset --hard
  - git clean -f
  - git checkout -b / git switch -c を main 以外から実行する
  - git branch -D （ただし squash merge 済みかつ単一ブランチ指定なら許可）

stdlib のみ使用。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.gh_cache import get_pr_by_branch as _get_pr_by_branch_cached
from _lib.git_helpers import run_git as _run_git
from _lib.hook_io import get_command, is_hook_enabled, read_hook_input
from _lib.shell_parse import split_shell_fragments as _split_shell_fragments
from _lib.shell_parse import strip_heredoc_bodies as _strip_heredoc_bodies

DETAIL = "詳細: docs/reference/hooks.md#block-dangerous-gitpy\n"


def _git(*args: str) -> str | None:
    """Issue #2958: subprocess 実行部は `_lib.git_helpers.run_git()` に委譲する."""
    result = _run_git(*args, timeout=10)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip()


def _gh_json(args: list[str]) -> str | None:
    """gh コマンドを実行して stdout（JSON or 値）を返す。失敗時は None."""
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=15
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


# Issue #2951: heredoc 本文の抽出・除去（2 パス方式・Issue #2608 のビットシフト/herestring
# 誤マッチ対策込み）は `_lib/shell_parse.py` へ集約した。`_strip_heredoc_bodies` は
# 同モジュールの `strip_heredoc_bodies` を re-export したもの（呼び出し元の互換維持）。


def _normalize(command: str) -> str:
    """旧 sh: tr -d '"\''  | tr '\t\n\r' '   '"""
    out = command.replace('"', "").replace("'", "")
    for ch in "\t\n\r":
        out = out.replace(ch, " ")
    return out


def _replace_safe_force(command: str) -> str:
    """--force-with-lease / --force-if-includes をプレースホルダーに置換."""
    out = command.replace("--force-with-lease", "__SAFE_FORCE_LEASE__")
    out = out.replace("--force-if-includes", "__SAFE_FORCE_IF_INCLUDES__")
    return out


# パターン定義（旧 sh の grep -qE を Python re.search に移植）
_PUSH_RE = re.compile(r"\bgit\b.*\bpush\b")
_PUSH_FORCE_RE = re.compile(r"\bgit\b.*\bpush\b.*(\s--force\b|\s-f\b)")
_RESET_HARD_RE = re.compile(r"\bgit\b.*\breset\b.*--hard\b")
_CLEAN_FORCE_RE = re.compile(
    r"\bgit(\s+-\S+)*\s+clean\b.*(-[a-zA-Z]*f[a-zA-Z]*|--force\b)"
)
# checkout -b / -B
_CHECKOUT_NEW_BRANCH_RE = re.compile(
    r"^(.*\s)?git(\s+-\S+)*\s+checkout(\s+\S+)*\s+-[a-zA-Z]*[bB][a-zA-Z]*(\s|$)"
)
# switch -c / -C / --create / --force-create
_SWITCH_NEW_BRANCH_RE = re.compile(
    r"^(.*\s)?git(\s+-\S+)*\s+switch(\s+\S+)*"
    r"(\s-[cC]\b|\s--create\b|\s--force-create\b)"
)
# branch -D / 強制削除
_BRANCH_DELETE_FORCE_RE = re.compile(
    r"\bgit(\s+-\S+)*\s+branch\b.*"
    r"(-[a-zA-Z]*D[a-zA-Z]*"
    r"|\s-d\b.*\s-f\b"
    r"|\s-[a-zA-Z]*d[a-zA-Z]*f"
    r"|\s--delete\b.*\s--force\b"
    r"|\s--force\b.*\s--delete\b)"
)
_COMMAND_CHAIN_RE = re.compile(r"(;|&&|\|\|)")
# -D を含むトークンを検出し、それ以降のトークン列を返すための正規表現
_DASH_D_TOKEN_RE = re.compile(r"-[a-zA-Z]*D[a-zA-Z]*")
# harness が付与する先頭の `cd <path> &&` prefix（Issue #2317）。
# Claude Code の Bash tool が別ディレクトリで実行する際にこの形式でラップするため、
# 危険なコマンド連結ではなく安全な前置として許容する。
_LEADING_CD_PREFIX_RE = re.compile(r"^cd\s+\S+\s*&&\s*")


def _strip_leading_cd_prefix(normalized: str) -> str:
    """harness が付与する先頭の `cd <path> &&` prefix を取り除く（Issue #2317）."""
    return _LEADING_CD_PREFIX_RE.sub("", normalized, count=1)


def _normalize_segment(segment: str) -> str:
    """1 セグメント分の引用符除去・タブ/CR の空白化（`_normalize` のセグメント版）."""
    out = segment.replace('"', "").replace("'", "")
    for ch in "\t\r":
        out = out.replace(ch, " ")
    return out


def _is_git_invocation(segment: str) -> bool:
    """引用符除去済みセグメントの先頭トークンが `git` であるかを判定する（Issue #2795）."""
    tokens = segment.split()
    return bool(tokens) and tokens[0] == "git"


def _filter_git_segments(command_for_check: str) -> str:
    """コマンド連結・パイプの各セグメントのうち、`git` を起動するものだけを残す（Issue #2795）.

    従来はコマンド行全体（連結・パイプを含む）を対象に危険パターンを `re.search`
    していたため、`git` を一切呼ばないセグメント（`gh issue list --search "branch -D"`
    等）が、検索キーワードやハイフン区切りファイル名（`block-dangerous-git`）に
    含まれる `git` 部分文字列だけで誤ブロックされていた。

    本関数はセグメント境界（`;` `&&` `||` `|` および改行）で分割し、引用符除去後の
    先頭トークンが `git` であるセグメントのみを残す。呼び出し元は heredoc 本文を
    `_strip_heredoc_bodies` で除去済みの文字列を渡すこと（本文中の `git` テキストを
    誤って git セグメントとして拾わないため）。

    分割は quote-aware な `_lib/shell_parse.split_shell_fragments` を使う
    （Issue #2966。naive な regex 分割はクォート内の区切り文字でも誤分割していた）。
    """
    raw_segments = _split_shell_fragments(command_for_check)
    git_segments = [
        _normalize_segment(seg)
        for seg in raw_segments
        if _is_git_invocation(_normalize_segment(seg))
    ]
    return " ".join(git_segments)


def _check_branch_delete_safe(normalized: str) -> bool:
    """git branch -D が安全に許可できる条件を判定する.

    旧 sh の条件:
      1. コマンド連結（; && ||）を含まない（harness の先頭 `cd <path> &&` prefix は除く）
      2. -D フラグ以降のトークン数が1（ブランチ1つだけ）
      3. そのブランチに紐付く PR が MERGED 状態
      4. ローカル HEAD = PR の headRefOid （squash merge 後の追加コミット無し）

    安全性判定ロジックの共有関数は `tidd_tools.cleanup_merged_branch.check_branch_delete_safe`
    に切り出した（#2370）。本関数は hook から呼ばれる薄いラッパーとして残す。
    `tidd` コマンドが PATH 上にある場合は `tidd cleanup-merged-branch --check-only` に
    委譲し、見つからない場合は内部実装にフォールバックする。
    """
    if _COMMAND_CHAIN_RE.search(_strip_leading_cd_prefix(normalized)):
        return False

    tokens = normalized.split()
    after_flag_tokens: list[str] = []
    for i, t in enumerate(tokens):
        if _DASH_D_TOKEN_RE.fullmatch(t):
            after_flag_tokens = tokens[i + 1 :]
            break

    if len(after_flag_tokens) != 1:
        return False

    branch_name = after_flag_tokens[0]

    # tidd cleanup-merged-branch --check-only に委譲する（#2370）。
    # tidd が見つからない場合は内部実装（キャッシュ優先）にフォールバック。
    try:
        result = subprocess.run(
            ["tidd", "cleanup-merged-branch", "--check-only", "--", branch_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # フォールバック: tidd が見つからない場合は内部実装を使う。
    # キャッシュ（gh-cache.db）から PR 情報を取得し、miss 時は gh subprocess にフォールバック。
    cached_pr = _get_pr_by_branch_cached(branch_name)
    if cached_pr is not None:
        pr_state = cached_pr.get("state")
        pr_head = cached_pr.get("headRefOid")
    else:
        # `--` を挟んで branch_name をオプションとして解釈されないようにする（引数インジェクション対策）。
        pr_state = _gh_json(
            ["pr", "view", "--json", "state", "-q", ".state", "--", branch_name]
        )
        pr_head = _gh_json(
            [
                "pr",
                "view",
                "--json",
                "headRefOid",
                "-q",
                ".headRefOid",
                "--",
                branch_name,
            ]
        )

    if pr_state != "MERGED":
        return False
    local_head = _git("rev-parse", f"refs/heads/{branch_name}")
    return bool(pr_head and local_head and pr_head == local_head)


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")  # Issue #1364
    command = get_command(payload)
    if not command:
        return 0

    # heredoc 本文を除去した判定用コピーを作成する（Issue #2576）。
    # 元の command は変更しない（git commit --amend チェックが元コマンドを参照するため）。
    command_for_check = _strip_heredoc_bodies(command)
    # `_check_branch_delete_safe` はコマンド全体の連結・cd prefix を自前で判定するため
    # フィルタ前の全体正規化文字列を必要とする（従来通り）。
    normalized = _normalize(command_for_check)
    # 危険パターン照合は「git を起動するセグメント」のみを対象にする（Issue #2795）。
    git_only = _filter_git_segments(command_for_check)
    safe = _replace_safe_force(git_only)

    # git push --force / -f
    if _PUSH_RE.search(safe) and _PUSH_FORCE_RE.search(safe):
        sys.stderr.write(
            "BLOCK: 危険なgit操作: git push --force（--force-with-leaseを使用してください）\n"
        )
        sys.stderr.write(DETAIL)
        return 2

    # git reset --hard
    if _RESET_HARD_RE.search(safe):
        sys.stderr.write(
            "BLOCK: 危険なgit操作: git reset --hard\n"
            "未コミットの変更をすべて取り消す操作です（元に戻せません）。\n"
            "特定ファイルだけ元に戻したい場合: git checkout -- <ファイル名>\n"
        )
        sys.stderr.write(DETAIL)
        return 2

    # git clean -f
    if _CLEAN_FORCE_RE.search(safe):
        sys.stderr.write(
            "BLOCK: 危険なgit操作: git clean -f\n"
            "git 管理外のファイルをすべて削除します（元に戻せません）。\n"
            "削除対象を事前確認するには: git clean -n\n"
        )
        sys.stderr.write(DETAIL)
        return 2

    # git checkout -b / switch -c を main 以外から
    if _CHECKOUT_NEW_BRANCH_RE.search(safe) or _SWITCH_NEW_BRANCH_RE.search(safe):
        current_branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if current_branch and current_branch != "main":
            sys.stderr.write(
                f"BLOCK: 新ブランチは main から切ってください（現在: {current_branch}）\n"
            )
            sys.stderr.write(DETAIL)
            return 2

    # git branch -D / 強制削除
    if _BRANCH_DELETE_FORCE_RE.search(safe):
        if _check_branch_delete_safe(normalized):
            return 0
        sys.stderr.write(
            "BLOCK: 危険なgit操作: git branch -D\n"
            "ブランチを強制削除します。PR が GitHub でマージ済みであることを確認してから実行してください。\n"
            "マージ済みを確認後、worktree 削除を先に行ってください: git worktree remove <パス>\n"
        )
        sys.stderr.write(DETAIL)
        return 2

    # git commit --amend（Issue #1285: TDD 順序改竄防止・Issue #1352 で誤検知修正）
    # コマンド先頭・シェルセパレータ直後・改行後の `git commit ... --amend` のみをブロックする。
    # `[^&|;<(\n]` で shell separator / heredoc(<<) / subshell($()) / 改行の境界も除外する。
    # 改行を保持するため normalized ではなく元の command を検査する。
    # 引用符 (") と (') はそのまま残る（heredoc/subshell 判定に必要）。
    if re.search(
        r"(?:^|&&|\||;|\n)\s*git\s+commit\b[^&|;<(\n]*\s--amend\b",
        command,
    ):
        sys.stderr.write(
            "BLOCK: 危険なgit操作: git commit --amend（TDD RED-GREEN 順序改竄防止）\n"
            "既にコミット済みの内容を書き換えると、require-red-first hook が commit 履歴から\n"
            "TDD 順序を検証できなくなります。新しい commit を作成してください。\n"
        )
        sys.stderr.write(DETAIL)
        return 2

    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("block-dangerous-git"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
