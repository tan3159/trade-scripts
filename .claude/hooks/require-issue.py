#!/usr/bin/env python3
"""PreToolUse hook: git commit に closes #N が含まれるか確認する.

NO TICKET NO WORK — Issueなしのコミットをブロックする（exit 2）。
cache 優先で Issue 存在を確認し、TTL 切れ時は stale を即返しつつ
バックグラウンドで gh subprocess を起動する（Issue #1312 → #1393 stale-while-revalidate）。

#1969: rate limit 判定（_get_rate_limit_remaining）は cache 完全 miss 時のみ実行する。
stale hit 時は BG refresh 側（gh_cache_refresh.py）が quota 保護を担う。

Python 化（Phase 4 / #1057）。旧 require-issue.sh の振る舞いを 1:1 で踏襲する。
stdlib のみ使用。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.gh_cache import get_issue as _get_issue_fresh  # noqa: E402 - Issue #1463
from _lib.gh_cache import get_issue_or_stale_with_bg_refresh as _get_issue_swr  # noqa: E402
from _lib.gh_cache import upsert_issue as _upsert_issue  # noqa: E402
from _lib.hook_io import get_command, is_hook_enabled, read_hook_input  # noqa: E402

# 旧 sh: grep -qE '(^|&&|;)\s*git commit(\s|$)'
# Issue #2226: `git -C <path> commit` 形式（クロスリポジトリコミット）も検出対象に含める。
_GIT_COMMIT_RE = re.compile(r"(^|&&|;)\s*git(?:\s+-C\s+\S+)?\s+commit(\s|$)")
# 旧 sh: grep -qiE 'closes #[0-9]+'
_CLOSES_RE = re.compile(r"closes #([0-9]+)", re.IGNORECASE)

# Issue #2226: `cd <path> &&` / `git -C <path>` から対象リポジトリのパスを解決する。
_CD_PREFIX_RE = re.compile(r"(?:^|&&|;)\s*cd\s+(.+?)\s*&&")
_GIT_DASH_C_RE = re.compile(r"git\s+-C\s+(\"[^\"]+\"|'[^']+'|\S+)")


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _resolve_target_repo_path(command: str) -> str | None:
    """コマンド文字列から対象リポジトリのパスを解決する（Issue #2226）.

    `cd <path> &&` prefix があればそのパスを、`git -C <path>` があればそのパスを返す。
    どちらも見つからなければ None（セッション CWD をそのまま使う従来挙動）。
    """
    match = _CD_PREFIX_RE.search(command)
    if match:
        return _strip_quotes(match.group(1).strip())
    match = _GIT_DASH_C_RE.search(command)
    if match:
        return _strip_quotes(match.group(1).strip())
    return None


# Issue #1463: adaptive stale TTL の閾値
_RATE_LIMIT_NEAR_EXCEEDED = 5


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


def _main_impl() -> tuple[int, dict[str, object]]:
    # Issue #1292: PreToolUse schema で payload を検証（不一致時は exit 2）
    payload = read_hook_input(hook_name="PreToolUse")
    command = get_command(payload)
    if not command:
        return 0, {"skip_reason": "no_command"}
    if not _GIT_COMMIT_RE.search(command):
        return 0, {"skip_reason": "not_git_commit"}

    match = _CLOSES_RE.search(command)
    if not match:
        sys.stderr.write(
            "コミットメッセージに「closes #N」が含まれていません。NO TICKET NO WORK。\n"
            "Issue なしのコミットはブロックされます。対象の Issue 番号を指定してください。\n"
            '例: git commit -m "feat: XXX を追加する closes #1234"\n'
        )
        sys.stderr.write("詳細: docs/reference/hooks.md#require-issuepy\n")
        return 2, {"blocked_by": "no_closes_ref"}

    issue_number = int(match.group(1))
    repo_path = _resolve_target_repo_path(command)
    if not _issue_exists(issue_number, repo_path=repo_path):
        sys.stderr.write(
            f"Blocked: Issue #{issue_number} が見つかりません。"
            "有効な Issue 番号で closes #N を指定してください。\n"
        )
        sys.stderr.write("詳細: docs/reference/hooks.md#require-issuepy\n")
        return 2, {"blocked_by": "issue_not_found", "issue_number": issue_number}

    return 0, {"issue_number": issue_number}


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("require-issue"):
        return 0
    exit_code, _extra = _main_impl()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
