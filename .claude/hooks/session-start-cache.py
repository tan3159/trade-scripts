#!/usr/bin/env python3
"""SessionStart hook: PR / Issue メタデータを gh で取得して sqlite3 にキャッシュする.

起動時に PR / Issue 一覧・現在ブランチの PR 情報を事前取得し、
~/.cache/<repo-name>/gh-cache.db に保存する（repo-name は git rev-parse から動的に決定）。
PreToolUse hook 群が gh subprocess を叩く回数を削減し、
GitHub API 障害への耐性を高める（Issue #1312）。

DB スキーマ（3 テーブル）:
  pr_list          — open + 最近 merged PR（head_ref 単位）
  issue_list       — open Issue 一覧
  current_branch_pr — 現在ブランチの PR（body 含む・シングルトン行）

TTL: 5 分（300 秒）。期限切れ時は次の SessionStart で再取得する。
Cache 破損時は自動削除して再生成する。

stdlib のみ使用。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import is_hook_enabled, read_hook_input  # noqa: E402


def _resolve_cache_dir() -> Path:
    """キャッシュディレクトリを git remote origin URL のリポジトリ名から動的に解決する.

    worktree でも canonical な repo 名を使うため git remote get-url origin で取得する。
    """
    import re

    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            m = re.search(r"/([^/]+?)(?:\.git)?$", r.stdout.strip())
            if m:
                return Path.home() / ".cache" / m.group(1)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return Path.home() / ".cache" / "tidd-hooks"


_CACHE_DIR = _resolve_cache_dir()
_DB_PATH = _CACHE_DIR / "gh-cache.db"
# Issue #1459: TTL を情報種別ごとに分離する
# - PR list: 300s (5 min)   ← マージ / merge 直後の見え方が変わりやすい
# - Issue list: 1800s (30 min) ← 変動率が低い（開発中の状態変化は少ない）
# - current_branch_pr: 120s (2 min) ← ブランチ切り替え直後は最新が欲しい
_TTL = 300  # 5 minutes (デフォルト / 後方互換のため保持)
_TTL_PR_LIST = 300
_TTL_ISSUE_LIST = 1800
_TTL_CURRENT_BRANCH_PR = 120


def _gh(*args: str, timeout: int = 15) -> str | None:
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=timeout
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=False, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _open_db() -> sqlite3.Connection:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
    # Issue #1393: 並行 hook 発火時の書き込み競合対策（WAL モード）
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        pass
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pr_list (
            head_ref TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS issue_list (
            issue_number INTEGER PRIMARY KEY,
            data TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS current_branch_pr (
            singleton INTEGER PRIMARY KEY DEFAULT 1,
            data TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    return conn


class RepairFailure(RuntimeError):
    """Issue #1454: DB 修復失敗の原因を stderr に区別して出せるようにするための例外.

    ``category`` は "permission" / "disk_full" / "lock_timeout" / "unknown" のいずれか。
    ``hint`` に解決手順文字列を格納する。
    """

    def __init__(self, category: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.category = category
        self.hint = hint


def _portable_path(path: Path) -> str:
    """HOME 配下の絶対パスを ``~/`` 記法に変換する（Gherkin 受け入れ基準向けにポータブル化）."""
    home = str(Path.home())
    s = str(path)
    if s.startswith(home + "/") or s == home:
        return "~" + s[len(home) :]
    return s


def _classify_os_error(exc: OSError) -> tuple[str, str]:
    """OSError / PermissionError から (category, hint) を返す."""
    import errno

    if isinstance(exc, PermissionError) or exc.errno == errno.EACCES:
        return (
            "permission",
            f"解決手順: chmod 755 {_portable_path(_CACHE_DIR)}/ を実行してください（"
            f"必要なら chmod 644 {_portable_path(_DB_PATH)} も併せて）。",
        )
    if exc.errno == errno.ENOSPC:
        return (
            "disk_full",
            "解決手順: 不要ファイルを削除するかディスク容量を追加してください。",
        )
    return ("unknown", "")


def _classify_sqlite_error(exc: sqlite3.Error) -> tuple[str, str]:
    """sqlite3.Error から (category, hint) を返す.

    sqlite3.OperationalError は「database is locked」「database or disk is full」など
    複数の症状を message で区別する。lock 系は lock_timeout に、full 系は disk_full に分類する。
    """
    msg = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError):
        if (
            "database or disk is full" in msg
            or "disk is full" in msg
            or "disk full" in msg
        ):
            return (
                "disk_full",
                "解決手順: 不要ファイルを削除するかディスク容量を追加してください。",
            )
        if "database is locked" in msg or "locked" in msg:
            return (
                "lock_timeout",
                "解決手順: 他プロセスの Claude Code セッションを終了するか、"
                f"lock file を削除してください（{_DB_PATH}-journal / -wal）。",
            )
    return ("unknown", "")


def _repair_db() -> sqlite3.Connection:
    """DB を unlink → 再生成する。失敗パターンを ``RepairFailure`` で区別して raise する.

    Issue #1454: silent skip 経路（旧: `except OSError: pass; return _open_db()`）を廃止し、
    permission denied / disk full / write lock timeout を stderr で区別できるようにする。
    """
    try:
        _DB_PATH.unlink(missing_ok=True)
    except PermissionError as exc:
        category, hint = _classify_os_error(exc)
        raise RepairFailure(category, f"unlink failed: {exc}", hint) from exc
    except OSError as exc:
        category, hint = _classify_os_error(exc)
        raise RepairFailure(category, f"unlink failed: {exc}", hint) from exc
    try:
        return _open_db()
    except sqlite3.OperationalError as exc:
        category, hint = _classify_sqlite_error(exc)
        raise RepairFailure(category, f"open failed: {exc}", hint) from exc
    except sqlite3.Error as exc:
        raise RepairFailure("unknown", f"open failed: {exc}", "") from exc
    except PermissionError as exc:
        category, hint = _classify_os_error(exc)
        raise RepairFailure(category, f"open failed: {exc}", hint) from exc
    except OSError as exc:
        category, hint = _classify_os_error(exc)
        raise RepairFailure(category, f"open failed: {exc}", hint) from exc


def _expires_at(kind: str = "default") -> int:
    """Issue #1459: 情報種別ごとの TTL を返す.

    Args:
        kind: "pr_list" / "issue_list" / "current_branch_pr" のいずれか。
            未指定 or 未対応の kind は default (_TTL) を使う。
    """
    ttl = {
        "pr_list": _TTL_PR_LIST,
        "issue_list": _TTL_ISSUE_LIST,
        "current_branch_pr": _TTL_CURRENT_BRANCH_PR,
    }.get(kind, _TTL)
    return int(time.time()) + ttl


def _fetch_prs(conn: sqlite3.Connection) -> None:
    raw = _gh(
        "pr",
        "list",
        "--state",
        "open",
        "--json",
        "number,title,labels,headRefName,headRefOid,state",
    )
    if raw:
        try:
            prs: list[dict] = json.loads(raw)
        except json.JSONDecodeError:
            prs = []
        exp = _expires_at("pr_list")
        for pr in prs:
            ref = pr.get("headRefName")
            if ref:
                conn.execute(
                    "INSERT OR REPLACE INTO pr_list (head_ref, data, expires_at) VALUES (?, ?, ?)",
                    (ref, json.dumps(pr, ensure_ascii=False), exp),
                )

    # 最近マージ済みの PR も取得（git branch -D の safety check に使用）
    merged_raw = _gh(
        "pr",
        "list",
        "--state",
        "merged",
        "--limit",
        "10",
        "--json",
        "number,headRefName,headRefOid,state",
    )
    if merged_raw:
        try:
            merged_prs: list[dict] = json.loads(merged_raw)
        except json.JSONDecodeError:
            merged_prs = []
        exp = _expires_at("pr_list")
        for pr in merged_prs:
            ref = pr.get("headRefName")
            if ref:
                existing = conn.execute(
                    "SELECT 1 FROM pr_list WHERE head_ref = ?", (ref,)
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT OR REPLACE INTO pr_list (head_ref, data, expires_at) VALUES (?, ?, ?)",
                        (ref, json.dumps(pr, ensure_ascii=False), exp),
                    )


def _fetch_issues(conn: sqlite3.Connection) -> None:
    raw = _gh(
        "issue",
        "list",
        "--state",
        "open",
        "--limit",
        "1000",
        "--json",
        "number,title,labels",
    )
    if not raw:
        return
    try:
        issues: list[dict] = json.loads(raw)
    except json.JSONDecodeError:
        return
    exp = _expires_at("issue_list")
    for issue in issues:
        num = issue.get("number")
        if num:
            conn.execute(
                "INSERT OR REPLACE INTO issue_list (issue_number, data, expires_at) VALUES (?, ?, ?)",
                (num, json.dumps(issue, ensure_ascii=False), exp),
            )


def _fetch_current_branch_pr(conn: sqlite3.Connection) -> None:
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":
        # ブランチが特定できない場合は古い singleton を削除してキャッシュを無効化する
        conn.execute("DELETE FROM current_branch_pr WHERE singleton = 1")
        return
    raw = _gh(
        "pr",
        "view",
        "--json",
        "number,title,state,headRefName,headRefOid,body",
        "--",
        branch,
    )
    if not raw:
        # gh 失敗は API 障害の可能性があるため singleton は保持する（stale cache として機能）
        # get_current_branch_pr() が headRefName でブランチ一致を検証するので安全
        return
    try:
        pr_data: dict = json.loads(raw)
    except json.JSONDecodeError:
        return
    # headRefName が gh から返らなかった場合は branch をセットしておく（照合に必要）
    if not pr_data.get("headRefName"):
        pr_data["headRefName"] = branch
    exp = _expires_at("current_branch_pr")
    conn.execute(
        "INSERT OR REPLACE INTO current_branch_pr (singleton, data, expires_at) VALUES (1, ?, ?)",
        (json.dumps(pr_data, ensure_ascii=False), exp),
    )
    # pr_list にも同期（body 含む完全データで上書き）
    ref = pr_data.get("headRefName") or branch
    conn.execute(
        "INSERT OR REPLACE INTO pr_list (head_ref, data, expires_at) VALUES (?, ?, ?)",
        (ref, json.dumps(pr_data, ensure_ascii=False), exp),
    )


_MODULE_NOT_FOUND_MARKER = "No module named tidd_tools"


def _report_health_check_failure(result: subprocess.CompletedProcess[str]) -> None:
    """health-check の失敗結果を stdout に警告として出力する."""
    lines = result.stderr.splitlines()[:20]
    detail = "\n".join(lines)
    sys.stdout.write(
        f"session-start-health-check: tidd health-check が失敗しました。\n{detail}\n"
    )


def _run_health_check() -> None:
    """SessionStart 時に tidd health-check を実行し、失敗時に stdout へ警告を出す（Issue #2205）.

    機能キー ``session-start-health-check`` で on/off できる（``session-start-cache`` とは独立）。
    git root が取得できない場合は silent skip する。
    ``OSError`` / ``subprocess.TimeoutExpired`` は silent skip（セッション開始をブロックしない）。
    exit 0 なら何も出力しない。exit != 0 なら stdout に警告 + stderr 先頭 20 行を出力する。
    hook 全体の exit code には影響しない。

    consumer が `uv tool install` 経路で tidd_tools を導入した場合、`.venv` には
    tidd_tools が入らないため `.venv/bin/python -m tidd_tools health-check` は
    「No module named tidd_tools」で失敗し続ける。この場合は警告を出さず、
    PATH 上の `tidd` 実行ファイルへフォールバックする（Issue #2211）。
    """
    if not is_hook_enabled("session-start-health-check"):
        return

    root = _git("rev-parse", "--show-toplevel")
    if not root:
        return

    root_path = Path(root)
    # venv python の探索（Linux/macOS 優先、Windows フォールバック）
    venv_python: Path | None = None
    for candidate in (
        root_path / ".venv" / "bin" / "python",
        root_path / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.is_file():
            venv_python = candidate
            break

    if venv_python is not None:
        try:
            result = subprocess.run(
                [str(venv_python), "-m", "tidd_tools", "health-check"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=root,
            )
        except (OSError, subprocess.TimeoutExpired):
            return

        if result.returncode == 0:
            return

        if _MODULE_NOT_FOUND_MARKER not in result.stderr:
            _report_health_check_failure(result)
            return
        # .venv に tidd_tools がない consumer 環境 → tidd フォールバックへ進む

    tidd_bin = shutil.which("tidd")
    if tidd_bin is None:
        return

    try:
        fallback_result = subprocess.run(
            [tidd_bin, "health-check"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=root,
        )
    except (OSError, subprocess.TimeoutExpired):
        return

    if fallback_result.returncode != 0:
        _report_health_check_failure(fallback_result)


def _run_cache() -> int:
    """PR/Issue キャッシュ本体。機能キー ``session-start-cache`` で on/off する（Issue #2167）."""
    if not is_hook_enabled("session-start-cache"):
        return 0

    read_hook_input(hook_name="SessionStart")

    # Issue #1459: 古い BG PID の zombie 掃除（24 時間 threshold）
    try:
        from _lib.gh_cache import _cleanup_stale_bg_pids  # noqa: PLC0415

        _cleanup_stale_bg_pids()
    except (ImportError, OSError):
        pass  # 失敗しても hook 本体には影響しない

    try:
        conn = _open_db()
    except sqlite3.Error as e:
        sys.stderr.write(f"session-start-cache: DB 初期化エラー: {e}\n")
        try:
            conn = _repair_db()
        except RepairFailure as rf:
            # Issue #1454: silent skip 廃止。原因別 stderr + exit 1 (fatal) に区別する。
            label = {
                "permission": "permission denied",
                "disk_full": "disk full",
                "lock_timeout": "write lock timeout",
            }.get(rf.category, "unknown error")
            sys.stderr.write(
                f"session-start-cache: FATAL: repair failed - {label}: {rf}\n"
            )
            if rf.hint:
                sys.stderr.write(f"session-start-cache: {rf.hint}\n")
            return 1

    try:
        _fetch_prs(conn)
        _fetch_issues(conn)
        _fetch_current_branch_pr(conn)
        conn.commit()
    except sqlite3.Error as e:
        sys.stderr.write(f"session-start-cache: DB 書き込みエラー: {e}\n")
    finally:
        conn.close()

    return 0


def main() -> int:
    exit_code = _run_cache()

    # Issue #2232: health check は session-start-health-check キーのみで gate する
    # （session-start-cache が無効でも実行する）
    _run_health_check()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
