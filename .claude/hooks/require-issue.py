#!/usr/bin/env python3
"""PreToolUse hook: git commit に Issue 参照キーワードが含まれるか確認する.

NO TICKET NO WORK — Issueなしのコミットをブロックする（exit 2）。
cache 優先で Issue 存在を確認し、TTL 切れ時は stale を即返しつつ
バックグラウンドで gh subprocess を起動する（Issue #1312 → #1393 stale-while-revalidate）。

#1969: rate limit 判定（_get_rate_limit_remaining）は cache 完全 miss 時のみ実行する。
stale hit 時は BG refresh 側（gh_cache_refresh.py）が quota 保護を担う。

Python 化（Phase 4 / #1057）。旧 require-issue.sh の振る舞いを 1:1 で踏襲する。
stdlib のみ使用。

#2638: 受理キーワードを closes のみから closes / fixes / resolves / refs に拡張する。
refs は GitHub のクローズキーワードではないため、決定記録など未実装 Issue の参照に使用できる。

Issue #2958: 対象リポジトリ CWD の解決を `_lib.hook_io.resolve_target_cwd()` へ移行する。
従来の `_resolve_target_repo_path` はコマンド文字列内の `cd <path> &&` / `git -C <path>`
しか見ておらず、payload の `cwd` フィールド（Bash 永続 CWD が worktree にあり、
コマンド自体にはパス指定がないケース）を無視していた。

PR #3026 codex レビュー指摘: `resolve_target_cwd()` は候補が 1 つもなければプロセス
CWD へフォールバックし常に非 None を返すため、戻り値をそのまま ``_issue_exists()`` の
``repo_path`` に渡すと、cd / -C / payload cwd のいずれも指定がない通常のコミットでも
常に ``repo_path`` 指定ありの経路（direct verify・cache 未使用）に入ってしまい、
fresh/stale cache と rate-limit bypass 経路（#1393 / #1969）が使われなくなる。
解決先がプロセス CWD 自身と一致する場合（＝実質「指定なし」）は ``repo_path=None``
を渡し、従来どおり cache 優先経路を使う。解決先がプロセス CWD と異なる場合のみ
（cd / -C / payload cwd による明示的なクロスリポジトリ指定）direct verify を使う。

Issue #3717: 本 hook は「セッションを開いたリポジトリ」の NO TICKET NO WORK 強制を
目的とする。Claude Code セッションが repo A で開かれている状態で repo B（別 git
リポジトリ）のファイルをコミットする場合、repo A のルールを repo B の操作へ適用しない。
コミット対象リポジトリがセッション CWD のリポジトリと異なる場合は skip（exit 0）する。
同一リポジトリの別 worktree は「同じリポジトリ」として扱う（common git dir 一致判定）。

escape hatch: 環境変数 `SKIP_REQUIRE_ISSUE_GATE=1` でチェックを全スキップする。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.gh_cache import get_issue as _get_issue_fresh
from _lib.gh_cache import (
    get_issue_or_stale_with_bg_refresh as _get_issue_swr,
)
from _lib.gh_cache import upsert_issue as _upsert_issue
from _lib.gh_command import CLOSES_OR_REFS_RE as _CLOSES_RE
from _lib.hook_io import (
    get_command,
    is_hook_enabled,
    read_hook_input,
    resolve_target_cwd,
)

# 旧 sh: grep -qE '(^|&&|;)\s*git commit(\s|$)'
# Issue #2226: `git -C <path> commit` 形式（クロスリポジトリコミット）も検出対象に含める。
_GIT_COMMIT_RE = re.compile(r"(^|&&|;)\s*git(?:\s+-C\s+\S+)?\s+commit(\s|$)")
# 旧 sh: grep -qiE 'closes #[0-9]+'
# #2638: closes / fixes / resolves / refs を受理する。
# refs は GitHub のクローズキーワードではないため、決定記録など未実装 Issue の参照に使用できる。
# #2653: word boundary を追加し、単語の一部として現れた文字列（例: unrefs / dereferences）を拒否する。
# Issue #2952: パターン実体は `_lib/gh_command.CLOSES_OR_REFS_RE` へ集約（refs を含む変種）。

# Issue #1463: adaptive stale TTL の閾値
_RATE_LIMIT_NEAR_EXCEEDED = 5

# escape hatch（Issue #3717）: 1 を設定すると本 hook のチェックを全スキップする.
_ESCAPE_HATCH_ENV = "SKIP_REQUIRE_ISSUE_GATE"


def _get_rate_limit_remaining() -> int | None:
    """Issue #1463: `gh api rate_limit --jq .resources.core.remaining` で remaining を返す.

    取得失敗時は None。
    #1969: cache 完全 miss 時のみ呼び出す（stale hit 時は BG refresh 側が quota 保護を担う）。
    """
    try:
        result = subprocess.run(
            ["gh", "api", "rate_limit", "--jq", ".resources.core.remaining"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    stripped = result.stdout.strip()
    if not stripped.isdigit():
        return None
    return int(stripped)


_RATE_LIMIT_SENTINEL: dict[str, str] = {"__rate_limit_exceeded__": "true"}


def _verify_issue_via_gh(issue_number: int, cwd: str | None = None) -> dict | None:
    """gh subprocess で Issue 存在を確認する。存在すれば dict を返す。

    Issue #1463: `gh` の stderr に "rate limit" が含まれる場合は
    ``_RATE_LIMIT_SENTINEL`` を返して呼び出し側で bypass 経路に流す。
    通常の失敗（Issue が存在しない・network error 等）は従来通り None。
    Issue #2226: ``cwd`` 指定時はそのディレクトリで `gh issue view` を実行する
    （クロスリポジトリコミット時に対象リポジトリを照会するため）。
    """
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--json", "number,title,state"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
            cwd=cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        # Issue #1463: rate limit exhaustion を stderr パターンで検知 → sentinel を返す
        if "rate limit" in (result.stderr or "").lower():
            sys.stderr.write(
                "GitHub API のリクエスト制限に達したため、一時的に Issue 確認をスキップします。\n"
            )
            return _RATE_LIMIT_SENTINEL
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _issue_exists(issue_number: int, repo_path: str | None = None) -> bool:
    """Issue の存在を確認する（#1393 SWR + #1463 adaptive TTL + #1969 rate limit 判定の遅延化）。

    Issue #2226: ``repo_path`` 指定時（コマンドが `cd <path> &&` / `git -C <path>` で
    セッション CWD と異なるリポジトリを対象にしている場合）は、セッション CWD の
    gh-cache（別リポジトリのキャッシュ）を使わず、対象リポジトリで直接 `gh issue view`
    を実行して存在確認する。

    優先順（``repo_path`` 未指定時。従来どおり）:
    1. fresh cache hit → 即 True
    2. stale cache hit → stale を即返し BG refresh 起動（rate limit guard は BG 側 gh_cache_refresh.py が行う。#1969）
    3. cache 完全 miss → rate limit 判定
       - remaining < 5 → allow without verification（一時 bypass・stderr 通知）
       - それ以外 → 同期 gh fetch（rate limit 枯渇 sentinel は bypass）

    Issue #2958: ``repo_path`` は呼び出し元（``_main_impl``）が
    ``_lib.hook_io.resolve_target_cwd()`` で解決するため、非 None 時は必ず実在する
    ディレクトリである（実在しない候補は ``resolve_target_cwd()`` 内部で silent に
    skip 済み）。よって本関数側での存在チェック・フォールバック WARN は不要（撤去）。
    """
    if repo_path is not None:
        data = _verify_issue_via_gh(issue_number, cwd=repo_path)
        if data is _RATE_LIMIT_SENTINEL:
            return True
        return data is not None

    # 1. fresh cache hit（既存テスト互換のため独立チェックを維持）
    fresh = _get_issue_fresh(issue_number)
    if fresh is not None:
        return True

    # 2. SWR: stale hit なら BG refresh を起動して即 True（同期 subprocess なし。#1969）
    cached = _get_issue_swr(issue_number)
    if cached is not None:
        return True

    # 3. cache 完全 miss のみ rate limit を同期照会（直後に同期 gh fetch する経路なので許容）
    remaining = _get_rate_limit_remaining()
    if remaining is not None and remaining < _RATE_LIMIT_NEAR_EXCEEDED:
        sys.stderr.write(
            f"GitHub API のリクエスト制限に近づいています（残り {remaining} 回）。"
            f"一時的に Issue 確認をスキップします。\n"
        )
        return True

    data = _verify_issue_via_gh(issue_number)
    if data is _RATE_LIMIT_SENTINEL:
        return True
    if data is not None:
        _upsert_issue(issue_number, data)
        return True
    return False


def _session_repo_root() -> str | None:
    """セッション CWD（process CWD）の git リポジトリルートを返す（Issue #3717）.

    `git rev-parse --show-toplevel` は cwd 基準で解決されるため、対象リポジトリの
    ルートは ``_target_repo_root`` で対象パスから引き直す。取得失敗時は None。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _target_repo_root(payload: dict, command: str) -> str | None:
    """コミット対象リポジトリのルートを返す（Issue #3717）.

    `resolve_target_cwd()` で解決したディレクトリから ``git rev-parse --show-toplevel``
    でリポジトリルートを引き直す（cwd 基準の解決を対象パス基準へ引き直す）。
    対象が git リポジトリでない・失敗時は None。
    """
    resolved_cwd = resolve_target_cwd(payload, command)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
            cwd=resolved_cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _repo_identity(repo_root: str) -> str | None:
    """リポジトリの同一性識別子（common git dir の絶対パス）を返す（Issue #3717）.

    同一リポジトリの全 worktree で共通の gitdir を返すため、worktree / メイン
    チェックアウトをまたいだ「同一リポジトリ」判定に使える。相対パス（メイン
    チェックアウトでは `.git`）は ``repo_root`` 起点に解決する。失敗時は None。
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    common = result.stdout.strip()
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.join(repo_root, common)
    return os.path.normpath(os.path.abspath(common))


def _is_outside_session_repo(payload: dict, command: str) -> bool:
    """コミット対象リポジトリがセッション CWD のリポジトリと異なるかを返す（Issue #3717）.

    セッション CWD（process CWD）のリポジトリとコミット対象リポジトリが「同じ
    リポジトリ」でない場合 True を返す（本 hook の対象外として skip する）。
    同一リポジトリの別 worktree は同じ扱い。

    セッション CWD が git リポジトリ外・対象リポジトリが判定不能の場合は False
    （従来挙動を維持し、自リポジトリの NO TICKET NO WORK を弱めない）。
    """
    session_root = _session_repo_root()
    if session_root is None:
        return False
    target_root = _target_repo_root(payload, command)
    if target_root is None:
        return False
    session_identity = _repo_identity(session_root)
    target_identity = _repo_identity(target_root)
    if session_identity is None or target_identity is None:
        return False
    return session_identity != target_identity


def _main_impl() -> tuple[int, dict[str, object]]:
    # Issue #1292: PreToolUse schema で payload を検証（不一致時は exit 2）
    payload = read_hook_input(hook_name="PreToolUse")
    command = get_command(payload)
    if not command:
        return 0, {"skip_reason": "no_command"}
    if not _GIT_COMMIT_RE.search(command):
        return 0, {"skip_reason": "not_git_commit"}

    # Issue #3717: コミット対象リポジトリがセッション CWD のリポジトリと異なる場合は
    # 本 hook（自リポジトリの NO TICKET NO WORK）の対象外として skip する。
    # `closes #N` 存在チェックより前に判定する（別リポジトリへの closes 無しコミットが
    # no_closes_ref で誤ってブロックされるのを防ぐ）。
    if _is_outside_session_repo(payload, command):
        return 0, {"skip_reason": "outside_session_repo"}

    match = _CLOSES_RE.search(command)
    if not match:
        sys.stderr.write(
            "コミットメッセージに Issue 参照キーワードが含まれていません。NO TICKET NO WORK。\n"
            "Issue なしのコミットはブロックされます。対象の Issue 番号を指定してください。\n"
            "  作業完了時（Issue をクローズする）: closes #N / fixes #N / resolves #N\n"
            "  参照のみ（Issue をクローズしない）: refs #N\n"
            '例: git commit -m "feat: XXX を追加する closes #1234"\n'
            '例: git commit -m "docs(decisions): 決定を記録する refs #1234"\n'
        )
        sys.stderr.write("詳細: docs/reference/hooks.md#require-issuepy\n")
        return 2, {"blocked_by": "no_closes_ref"}

    issue_number = int(match.group(1))

    resolved_cwd = resolve_target_cwd(payload, command)
    # PR #3026 codex レビュー指摘: resolve_target_cwd() の結果がプロセス CWD 自身と
    # 一致する場合はクロスリポジトリ指定なし（実質フォールバックのみ）とみなし、
    # None を渡して cache 優先経路（fresh/stale/rate-limit bypass）を維持する。
    repo_path = resolved_cwd if resolved_cwd != os.getcwd() else None
    if not _issue_exists(issue_number, repo_path=repo_path):
        sys.stderr.write(
            f"Blocked: Issue #{issue_number} が見つかりません。"
            "有効な Issue 番号を指定してください（closes / fixes / resolves / refs #N）。\n"
        )
        sys.stderr.write("詳細: docs/reference/hooks.md#require-issuepy\n")
        return 2, {"blocked_by": "issue_not_found", "issue_number": issue_number}

    return 0, {"issue_number": issue_number}


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("require-issue"):
        return 0
    # Issue #3717: escape hatch（環境変数）
    if os.environ.get(_ESCAPE_HATCH_ENV) == "1":
        sys.stderr.write(
            f"require-issue: {_ESCAPE_HATCH_ENV}=1 によりバイパスされました。\n"
        )
        return 0
    exit_code, _extra = _main_impl()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
