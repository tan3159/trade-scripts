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
from _lib.gh_cache import get_pr_by_branch as _get_pr_by_branch_cached  # noqa: E402
from _lib.hook_io import get_command, is_hook_enabled, read_hook_input  # noqa: E402

DETAIL = "詳細: docs/reference/hooks.md#block-dangerous-gitpy\n"


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=False, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
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


def _check_branch_delete_safe(normalized: str) -> bool:
    """git branch -D が安全に許可できる条件を判定する.

    旧 sh の条件:
      1. コマンド連結（; && ||）を含まない（harness の先頭 `cd <path> &&` prefix は除く）
      2. -D フラグ以降のトークン数が1（ブランチ1つだけ）
      3. そのブランチに紐付く PR が MERGED 状態
      4. ローカル HEAD = PR の headRefOid （squash merge 後の追加コミット無し）
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

    normalized = _normalize(command)
    safe = _replace_safe_force(normalized)

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
    if _CHECKOUT_NEW_BRANCH_RE.search(normalized) or _SWITCH_NEW_BRANCH_RE.search(
        normalized
    ):
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
