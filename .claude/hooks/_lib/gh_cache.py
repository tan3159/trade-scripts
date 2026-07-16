"""hook 共通の GitHub データキャッシュ読み取りヘルパー（Issue #1312）.

SessionStart hook（session-start-cache.py）が
~/.cache/<repo-name>/gh-cache.db に書いたデータを読む。
repo-name は git rev-parse --show-toplevel のディレクトリ名から動的に決定する。

DB スキーマ（3 テーブル）:
  pr_list          — open PR 一覧（head_ref 単位）
  issue_list       — open Issue 一覧
  current_branch_pr — 現在ブランチの PR（body 含む・シングルトン行）

- stdlib のみ使用
- キャッシュ未生成・期限切れの場合は None を返す（呼び出し側がフォールバック）
- DB 破損時は自動削除して None を返す
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _resolve_db_path() -> Path:
    """DB パスを git remote origin URL のリポジトリ名から動的に解決する.

    worktree でもディレクトリ名ではなく remote の canonical 名を使うため、
    git remote get-url origin で取得する。
    """
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            m = re.search(r"/([^/]+?)(?:\.git)?$", r.stdout.strip())
            if m:
                return Path.home() / ".cache" / m.group(1) / "gh-cache.db"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return Path.home() / ".cache" / "tidd-hooks" / "gh-cache.db"


_DB_PATH = _resolve_db_path()
_TTL = 300  # 5 minutes（session-start-cache.py と揃える）


def _open_db() -> sqlite3.Connection | None:
    if not _DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
        conn.row_factory = sqlite3.Row
        # 並行 hook 発火時の DB 書き込み競合対策 (Issue #1393)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            pass
        return conn
    except sqlite3.Error:
        try:
            _DB_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _get_row(conn: sqlite3.Connection, table: str, where: str, params: tuple) -> Any | None:
    try:
        row = conn.execute(
            f"SELECT data FROM {table} WHERE {where} AND expires_at > ?",
            (*params, int(time.time())),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return json.loads(row["data"])
    except (json.JSONDecodeError, TypeError):
        return None


def get_pr_by_branch(branch: str) -> dict[str, Any] | None:
    """ブランチ名に紐づく PR 情報を返す。キャッシュ未存在・期限切れ時は None.

    Parameters
    ----------
    branch:
        headRefName（例: ``feat/issue-123-slug``）

    Returns
    -------
    dict or None
        ``{"number": int, "state": str, "headRefOid": str, ...}``
        または None（キャッシュなし・期限切れ）。
    """
    conn = _open_db()
    if conn is None:
        return None
    try:
        return _get_row(conn, "pr_list", "head_ref = ?", (branch,))
    finally:
        conn.close()


def _current_git_branch() -> str | None:
    """現在チェックアウト中のブランチ名を返す。取得失敗時は None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch and branch != "HEAD" else None


def get_current_branch_pr() -> dict[str, Any] | None:
    """現在ブランチの PR 情報を返す。

    ブランチ切替後のキャッシュ不一致・期限切れ・未存在時は None を返す。
    """
    conn = _open_db()
    if conn is None:
        return None
    try:
        pr = _get_row(conn, "current_branch_pr", "singleton = 1", ())
    finally:
        conn.close()
    if pr is None:
        return None
    # 保存時のブランチと現在のブランチが一致するか検証する
    stored_ref = pr.get("headRefName")
    if stored_ref:
        current = _current_git_branch()
        if current and current != stored_ref:
            return None  # ブランチ切替後のため無効
    return pr


def get_pr_body() -> str | None:
    """現在ブランチの PR body を返す。キャッシュ未存在・期限切れ時は None."""
    pr = get_current_branch_pr()
    if pr is None:
        return None
    body = pr.get("body")
    return body if isinstance(body, str) else None


def get_pr_body_stale() -> str | None:
    """現在ブランチの PR body を TTL 無視で返す（GitHub API 障害時のフォールバック用）。

    ブランチ一致チェックは行うが、TTL は無視する。キャッシュ未存在時は None。
    """
    conn = _open_db()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT data FROM current_branch_pr WHERE singleton = 1",
        ).fetchone()
        if row is None:
            return None
        pr = json.loads(row["data"])
        # ブランチが切り替わっていたら無効とする（TTL は無視）
        stored_ref = pr.get("headRefName")
        if stored_ref:
            current = _current_git_branch()
            if current and current != stored_ref:
                return None
        body = pr.get("body")
        return body if isinstance(body, str) else None
    except (sqlite3.Error, json.JSONDecodeError, TypeError):
        return None
    finally:
        conn.close()


def get_issue(issue_number: int) -> dict[str, Any] | None:
    """Issue 番号に紐づく Issue 情報を返す。キャッシュ未存在・期限切れ時は None.

    Parameters
    ----------
    issue_number:
        GitHub Issue 番号（例: 1312）

    Returns
    -------
    dict or None
        ``{"number": int, "title": str, "labels": list, ...}``
        または None（キャッシュなし・期限切れ）。
    """
    conn = _open_db()
    if conn is None:
        return None
    try:
        return _get_row(conn, "issue_list", "issue_number = ?", (issue_number,))
    finally:
        conn.close()


def get_issue_stale(issue_number: int) -> dict[str, Any] | None:
    """Issue 番号に紐づく Issue 情報を TTL 無視で返す（GitHub API 障害時のフォールバック用）。

    キャッシュ未存在時は None を返す。
    """
    conn = _open_db()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT data FROM issue_list WHERE issue_number = ?",
            (issue_number,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["data"])
    except (sqlite3.Error, json.JSONDecodeError, TypeError):
        return None
    finally:
        conn.close()


def upsert_issue(issue_number: int, data: dict[str, Any]) -> None:
    """Issue 情報をキャッシュに書き込む（TTL をリセット）。失敗時はサイレント。

    TTL 超過時に require-issue.py が gh で再取得した後にキャッシュを更新するために使う。
    """
    conn = _open_db()
    if conn is None:
        return
    try:
        expires_at = int(time.time()) + _TTL
        conn.execute(
            "INSERT OR REPLACE INTO issue_list (issue_number, data, expires_at) VALUES (?, ?, ?)",
            (issue_number, json.dumps(data, ensure_ascii=False), expires_at),
        )
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def _spawn_background_refresh(issue_number: int) -> None:
    """gh 再取得を detach 起動する。失敗しても silent (Issue #1393).

    stale cache hit 時に呼ばれる。バックグラウンドプロセスが cache を更新するため、
    次回の hook 呼び出し時には fresh cache が返る。
    """
    refresh_helper = Path(__file__).parent / "gh_cache_refresh.py"
    if not refresh_helper.is_file():
        return
    try:
        subprocess.Popen(
            [sys.executable, str(refresh_helper), str(issue_number)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (FileNotFoundError, OSError):
        pass


def get_issue_or_stale_with_bg_refresh(issue_number: int) -> dict[str, Any] | None:
    """stale-while-revalidate パターン (Issue #1393).

    - TTL 未超過の cache hit → fresh dict を返す
    - TTL 超過だが stale row 存在 → stale dict を返し、バックグラウンドで gh 再取得を起動
    - cache miss（該当 row なし）→ None を返す（呼び出し側で同期 fetch にフォールバック）

    hook の応答時間を最小化するため、TTL 超過時も同期 fetch にフォールバックせず
    stale を即返す。BG 再取得は次回呼び出しで反映される。
    """
    fresh = get_issue(issue_number)
    if fresh is not None:
        return fresh
    stale = get_issue_stale(issue_number)
    if stale is None:
        return None
    _spawn_background_refresh(issue_number)
    return stale
