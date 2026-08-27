"""issue-next の複数マシン同時実行時における同一 Issue 二重着手を防ぐ分散ロック (#3452).

`git push origin <sha>:refs/locks/issue-<N>` を排他制御プリミティブとして使う。
GitHub 側は同一 ref 名への同時 push を 1 件しか受理しないため（後発は
``! [rejected] (fetch first)`` で拒否される）、ローカルファイルロックでは実現できない
複数マシン・複数プロセス間の真の排他性が得られる。

**ロック獲得（``acquire_lock``）:**
1. ``git rev-parse HEAD^{tree}`` + ``git commit-tree`` で「現在時刻をコミット日時とする
   空コミット」を作る（HEAD のツリーを再利用するため新規 blob は作られない）。
   このコミット日時をロック獲得時刻として stale 判定に使う。
2. ``git push origin <sha>:refs/locks/issue-<N>`` で ref を作成する。成功すればロック獲得。
3. 失敗（既に ref が存在する = 他プロセスが保持中）した場合、``git fetch`` + ``git log``
   で既存 ref のコミット日時を取得し、``ISSUE_NEXT_LIVENESS_TTL_SECONDS``
   （デフォルト 1800 秒・``issue_next_state`` の liveness 判定と同じ TTL）を超えて
   経過していれば stale と判定する。
4. stale と判定しても、対象 Issue に `🔧 in-progress` ラベルが付いている、または
   Issue を close する Open PR が存在する場合は強制解放しない（安全側）。
   両方の条件を満たさない場合のみ ``git push origin --delete`` で ref を削除し、
   再度 push を試みる。

**ロック解放（``release_lock``）:** ``git push origin --delete refs/locks/issue-<N>``。
失敗しても例外は送出せず stderr に warning を出すのみ（``clear`` 自体は成功させる）。

**テスト隔離:** `ISSUE_NEXT_STATE_ROOT` が設定されている場合（テスト隔離用）、
実 git ネットワーク呼び出しは一切行わず ``acquire_lock`` は無条件に True を返す
（`issue_progress_label` の隔離パターンと同じ・#2804）。

**ロック証跡ファイルによる worktree-add 側の機械強制（#4059）:** 分散ロックは
「同名 ref への同時 push は 1 つしか成功しない」atomic 性のみを排他制御の根拠にしているが、
実運用ではこの前提だけに依存する構成（`issue-next-state init` の exit code を呼び出し元の
LLM が正しく読んで従うことに依存）では二重着手を防ぎきれないことが判明した（#4059 背景）。
最終防衛線として、``acquire_lock`` 成功時に獲得した commit sha を
``acquired_lock_sha()`` で取得可能にし、``issue_next_state._cmd_init`` がこれを
``write_lock_evidence()`` でローカル証跡ファイル（``cache/issue-next-state/issue-<N>.lock``）へ
書き込む。``tidd worktree-add`` は ``git worktree add`` を実行する前に
``verify_lock_evidence()`` でこの証跡ファイルと ``git ls-remote origin refs/locks/issue-<N>``
の現在値を照合し、一致しない（証跡なし・他プロセスがロックを取り直した）場合は
worktree を作成せずブロックする。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tidd_tools import issue_progress_label
from tidd_tools.shared.errors import SubprocessTimeoutError
from tidd_tools.shared.issue_body import extract_closes_issues
from tidd_tools.shared.subprocess_runner import run as run_subprocess

DEFAULT_LIVENESS_TTL_SECONDS = 1800  # 30 分（issue_next_state の liveness TTL と同一値・#3452）

# `issue_next_state.STATE_SUBDIR` と同じ値。issue_next_state.py が本モジュールを import する
# ため、逆方向の import による循環を避けて意図的に重複定義している（#4059）。
_STATE_SUBDIR = "issue-next-state"

# acquire_lock() 成功時に得た commit sha を issue 番号ごとに一時保持する（#4059）。
# acquired_lock_sha() で 1 回だけ取り出す（pop）ため、次の init 呼び出しへ混入しない。
_LAST_LOCK_SHA: dict[int, str] = {}


def _is_test_isolated() -> bool:
    """テスト隔離目的の env var が設定されているか（実 git ネットワーク呼び出しをスキップする条件）."""
    return bool(os.environ.get("ISSUE_NEXT_STATE_ROOT"))


def _lock_ref(issue_num: int) -> str:
    return f"refs/locks/issue-{issue_num}"


def _resolve_ttl_seconds() -> int:
    """`ISSUE_NEXT_LIVENESS_TTL_SECONDS` から TTL を取得する（非数値・非正値はデフォルトへ）."""
    ttl_raw = os.environ.get("ISSUE_NEXT_LIVENESS_TTL_SECONDS", "")
    try:
        ttl = int(ttl_raw)
        if ttl <= 0:
            raise ValueError("TTL must be positive")
    except (ValueError, TypeError):
        ttl = DEFAULT_LIVENESS_TTL_SECONDS
    return ttl


def _create_lock_commit() -> str | None:
    """HEAD のツリーを再利用し、現在時刻をコミット日時とする空コミットを作る（失敗時 None）."""
    tree_result = run_subprocess(["git", "rev-parse", "HEAD^{tree}"], timeout=8)
    if tree_result.returncode != 0:
        return None
    tree_sha = tree_result.stdout.strip()
    if not tree_sha:
        return None
    commit_result = run_subprocess(["git", "commit-tree", tree_sha, "-m", "issue-next-lock"], timeout=8)
    if commit_result.returncode != 0:
        return None
    commit_sha = commit_result.stdout.strip()
    return commit_sha or None


def _push_lock_ref(commit_sha: str, issue_num: int) -> bool:
    result = run_subprocess(["git", "push", "origin", f"{commit_sha}:{_lock_ref(issue_num)}"], timeout=15)
    return result.returncode == 0


def _delete_lock_ref(issue_num: int) -> bool:
    result = run_subprocess(["git", "push", "origin", "--delete", _lock_ref(issue_num)], timeout=15)
    return result.returncode == 0


def _lock_acquired_at(issue_num: int) -> datetime | None:
    """既存ロック ref のコミット日時を取得する（fetch 失敗・パース失敗時は判定不能として None）."""
    fetch_result = run_subprocess(["git", "fetch", "origin", _lock_ref(issue_num)], timeout=15)
    if fetch_result.returncode != 0:
        return None
    log_result = run_subprocess(["git", "log", "-1", "--format=%cI", "FETCH_HEAD"], timeout=8)
    if log_result.returncode != 0:
        return None
    raw = log_result.stdout.strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _is_stale(acquired_at: datetime) -> bool:
    ttl = _resolve_ttl_seconds()
    return datetime.now(UTC) - acquired_at > timedelta(seconds=ttl)


def _has_open_pr_for_issue(issue_num: int) -> bool:
    """Issue を close する Open PR が存在するか判定する（判定不能時は安全側で True）.

    `issue_next_state._pr_ever_created_for_issue` と同じ
    `shared.issue_body.extract_closes_issues()` フィルタを使うが、対象は Open PR のみに絞る
    （stale ロックの強制解放を許可してよいかの判定に使うため）。
    """
    try:
        result = run_subprocess(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--search",
                f"closes #{issue_num} in:body",
                "--json",
                "number,body",
            ],
            timeout=8,
        )
    except (OSError, SubprocessTimeoutError):
        return True
    if result.returncode != 0:
        return True
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return True
    if not isinstance(data, list):
        return True
    return any(
        isinstance(entry, dict)
        and isinstance(entry.get("body"), str)
        and issue_num in extract_closes_issues(entry["body"])
        for entry in data
    )


_TEST_ISOLATED_LOCK_SHA = "test-isolated-lock-sha"  # テスト隔離時のダミー sha（#4059）


def acquire_lock(issue_num: int) -> bool:
    """Issue #``issue_num`` の分散ロックを獲得する.

    成功時 True。既に他プロセスが保持中（stale でない）なら False。
    stale と判定でき、かつ強制解放してよい条件（`🔧 in-progress` ラベルなし・
    Open PR なし）を満たす場合は自動的に解放して再獲得を試みる。

    成功時、獲得した commit sha を ``acquired_lock_sha(issue_num)`` で 1 回だけ
    取得できるよう記録する（worktree-add 側のロック証跡検証・#4059）。
    """
    if _is_test_isolated():
        _LAST_LOCK_SHA[issue_num] = _TEST_ISOLATED_LOCK_SHA
        return True

    commit_sha = _create_lock_commit()
    if commit_sha is None:
        return False

    if _push_lock_ref(commit_sha, issue_num):
        _LAST_LOCK_SHA[issue_num] = commit_sha
        return True

    acquired_at = _lock_acquired_at(issue_num)
    if acquired_at is None:
        return False
    if not _is_stale(acquired_at):
        return False
    if issue_progress_label.has_in_progress_label(issue_num):
        return False
    if _has_open_pr_for_issue(issue_num):
        return False
    if not _delete_lock_ref(issue_num):
        return False
    if _push_lock_ref(commit_sha, issue_num):
        _LAST_LOCK_SHA[issue_num] = commit_sha
        return True
    return False


def acquired_lock_sha(issue_num: int) -> str | None:
    """直前の ``acquire_lock(issue_num)`` 成功呼び出しで得た commit sha を 1 回だけ取り出す（#4059）.

    ``acquire_lock`` 自体は既存呼び出し元との後方互換のため bool を返し続けるが、
    ``issue_next_state._cmd_init`` が worktree-add 用の証跡ファイルを書くために別途 sha を
    取得する必要がある。取り出すと内部記録は消費される（pop）ため、``acquire_lock`` を
    呼ばずに呼び出した場合や、テストが ``acquire_lock`` 自体を丸ごと monkeypatch で
    差し替えた場合（sha が記録されない）は None を返す。
    """
    return _LAST_LOCK_SHA.pop(issue_num, None)


def release_lock(issue_num: int) -> None:
    """Issue #``issue_num`` の分散ロックを解放する（失敗しても例外は送出しない）."""
    if _is_test_isolated():
        return
    if not _delete_lock_ref(issue_num):
        print(
            f"==> WARN: refs/locks/issue-{issue_num} の削除に失敗しました（既に存在しない可能性があります）",
            file=sys.stderr,
        )


def _evidence_path(issue_num: int) -> Path:
    """ロック証跡ファイルのパスを返す（#4059）.

    ``issue_next_state._state_dir()`` と同じ規則（``ISSUE_NEXT_STATE_ROOT`` 優先・
    未設定時は CWD）に従う。``issue-next-state init`` と ``worktree-add`` を同じ
    作業ディレクトリ（リポジトリルート）から実行する限り、書き込み側・検証側で一致する。
    """
    root_override = os.environ.get("ISSUE_NEXT_STATE_ROOT")
    base = Path(root_override) if root_override else Path.cwd()
    return base / "cache" / _STATE_SUBDIR / f"issue-{issue_num}.lock"


def write_lock_evidence(issue_num: int, sha: str) -> None:
    """ロック証跡ファイルへ ``sha`` を書き込む（``issue_next_state._cmd_init`` から呼び出す・#4059）.

    ``verify_lock_evidence()`` がこのファイルと ``git ls-remote origin refs/locks/issue-<N>``
    の現在値を突き合わせて ``worktree-add`` の実行前に検証する。
    """
    path = _evidence_path(issue_num)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha + "\n", encoding="utf-8")


def verify_lock_evidence(issue_num: int) -> bool:
    """ロック証跡ファイルと remote ref の現在値が一致するか検証する（#4059）.

    ``tidd worktree-add`` が ``git worktree add`` を実行する前に呼び出す。
    (a) 証跡ファイルが存在し (b) ``git ls-remote origin refs/locks/issue-<N>`` の現在値が
    証跡ファイルの sha と一致することを確認する。証跡ファイルが無い
    （``issue-next-state init`` を経ていない）、または一致しない
    （他プロセスがロックを取り直した）場合は False。

    テスト隔離時（``ISSUE_NEXT_STATE_ROOT`` 設定時）は ``acquire_lock`` と同じ方針で
    無条件に True を返す（実 git ネットワーク呼び出しを行わない）。
    """
    if _is_test_isolated():
        return True

    path = _evidence_path(issue_num)
    if not path.is_file():
        return False
    local_sha = path.read_text(encoding="utf-8").strip()
    if not local_sha:
        return False

    result = run_subprocess(["git", "ls-remote", "origin", _lock_ref(issue_num)], timeout=15)
    if result.returncode != 0:
        return False
    stdout = result.stdout.strip()
    if not stdout:
        return False
    remote_sha = stdout.splitlines()[0].split()[0]
    return remote_sha == local_sha
