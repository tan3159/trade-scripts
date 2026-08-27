"""pre-flight pytest フルスイート実行キャッシュ（マーカーファイル管理）.

`pre_flight.py`（PR 作成前）と `ai_review/core.py`（ai-review 全 attempt）が同一 commit
（または同一 working tree 内容）に対して pytest フルスイートを重複実行しないよう、
SHA / tree hash 単位のマーカーファイルで「この SHA・tree は clean/検証済みで pytest 成功済み」
を記録する。

もともと `test_plan.py`（Issue #2311/#2449/#2696/#2800 で追加）に同居していたが、
test-plan コマンドとは独立のライフサイクルを持つ共有インフラであるため、
独立モジュールへ移設した（挙動変更なし・純粋な移設・Issue #2949）。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from tidd_tools.shared.errors import SubprocessTimeoutError
from tidd_tools.shared.subprocess_runner import run as run_subprocess
from tidd_tools.shared.subprocess_timeouts import QUICK_TIMEOUT_SEC, STANDARD_TIMEOUT_SEC

# ── pytest フルスイート実行キャッシュ（Issue #2311） ─────────────────────────
#
# pre-flight（PR 作成前）と ai-review（全 attempt）が同一 commit SHA に対して
# pytest フルスイートを重複実行しないよう、SHA 単位のマーカーファイルで
# 「この SHA は clean tree で pytest 成功済み」を記録する。


def _preflight_marker_path(repo_root: Path, sha: str) -> Path:
    return repo_root / ".tidd" / "state" / f"preflight-pytest-{sha}.json"


def has_fresh_preflight_marker(repo_root: Path, sha: str) -> bool:
    """指定 SHA に対する pre-flight pytest 成功マーカーが存在するか判定する（Issue #2311）."""
    if not sha:
        return False
    path = _preflight_marker_path(repo_root, sha)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or data.get("sha") != sha or data.get("success") is not True:
        return False
    # pytest を実行していないマーカー（doc-only 等）は pytest キャッシュとしては hit させない
    # （未実行のまま ai-review が pytest をスキップするのを防ぐ・Issue #2778 / #3458）。
    return not data.get("pytest_skipped_reason")


def write_preflight_pytest_marker(repo_root: Path, sha: str, *, pytest_skipped_reason: str = "") -> None:
    """clean working tree での pre-flight 成功を SHA 単位でマーカーに記録する（Issue #2311）.

    `pytest_skipped_reason` を渡すと「pre-flight は成功したが pytest は実行していない」ことを
    記録する。push gate（`require-preflight-marker.py`）は通すが pytest キャッシュとしては
    hit しない（Issue #3458）。
    """
    if not sha:
        return
    path = _preflight_marker_path(repo_root, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"sha": sha, "success": True, "written_at": datetime.now(UTC).isoformat()}
    if pytest_skipped_reason:
        payload["pytest_skipped_reason"] = pytest_skipped_reason
    path.write_text(json.dumps(payload), encoding="utf-8")


def _git_head_sha(repo_root: Path) -> str:
    try:
        proc = run_subprocess(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            timeout=QUICK_TIMEOUT_SEC,
        )
    except (FileNotFoundError, SubprocessTimeoutError):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


# ── pytest フルスイート実行キャッシュ・tree hash 版（Issue #2449） ───────────────
#
# 上記 SHA 版マーカーは commit SHA（author/date/message を含む commit metadata）を
# キーにしているため、dirty working tree で pre-flight を実行した直後に commit すると
# 別 SHA になり cache miss してしまう。tree hash はファイル内容のみで決まるため、
# dirty tree で pre-flight → commit しても内容が同じなら同一キーで cache hit する。
# 旧 SHA マーカーとは別ファイルで管理し、読み取り側は tree hash → SHA の順に確認する
# （後方互換: 既存 SHA マーカーも引き続き cache hit する）。


def _preflight_tree_marker_path(repo_root: Path, tree_hash: str) -> Path:
    return repo_root / ".tidd" / "state" / f"preflight-pytest-tree-{tree_hash}.json"


def has_fresh_preflight_tree_marker(repo_root: Path, tree_hash: str) -> bool:
    """指定 tree hash に対する pre-flight pytest 成功マーカーが存在するか判定する（Issue #2449）.

    マーカー JSON が壊れている・巨大・null 相当・tree_hash 不一致の場合は
    例外を送出せず安全側（cache miss）にフォールバックする。
    """
    if not tree_hash:
        return False
    path = _preflight_tree_marker_path(repo_root, tree_hash)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("tree_hash") != tree_hash or data.get("success") is not True:
        return False
    return not data.get("pytest_skipped_reason")


def write_preflight_pytest_tree_marker(
    repo_root: Path, tree_hash: str, sha: str = "", *, pytest_skipped_reason: str = ""
) -> None:
    """pytest フルスイート成功を tree hash 単位でマーカーに記録する（Issue #2449）.

    tree hash は working tree の内容のみで決まるため、dirty tree での成功後に
    commit して SHA が変わっても cache hit する。`sha` は追跡用の付随情報として書くのみで
    照合には使わない。
    """
    if not tree_hash:
        return
    path = _preflight_tree_marker_path(repo_root, tree_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "tree_hash": tree_hash,
        "sha": sha,
        "success": True,
        "written_at": datetime.now(UTC).isoformat(),
    }
    if pytest_skipped_reason:
        payload["pytest_skipped_reason"] = pytest_skipped_reason
    path.write_text(json.dumps(payload), encoding="utf-8")


# ── 配布物検証テスト実行キャッシュ（Issue #3604） ─────────────────────────────
#
# `.claude/**`・`templates/**`・`.rulesync/**` 変更時に限り `slow` marker 込みで実行する
# 配布物検証テスト（`test_plan._run_dist_check_tests`）専用のマーカー。上記フルスイート用
# tree hash マーカーとは別ファイルで管理する。フルスイートは既定で `-m "not slow"`
# （Issue #2969）のため、フルスイート成功マーカーが fresh でも配布物検証テスト（slow 込み）が
# 実際に検証済みとは限らない。


def _dist_check_marker_path(repo_root: Path, tree_hash: str) -> Path:
    return repo_root / ".tidd" / "state" / f"preflight-pytest-dist-check-tree-{tree_hash}.json"


def has_fresh_dist_check_marker(repo_root: Path, tree_hash: str) -> bool:
    """指定 tree hash に対する配布物検証テスト成功マーカーが存在するか判定する（Issue #3604）."""
    if not tree_hash:
        return False
    path = _dist_check_marker_path(repo_root, tree_hash)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return data.get("tree_hash") == tree_hash and data.get("success") is True


def write_dist_check_marker(repo_root: Path, tree_hash: str) -> None:
    """配布物検証テスト成功を tree hash 単位でマーカーに記録する（Issue #3604）."""
    if not tree_hash:
        return
    path = _dist_check_marker_path(repo_root, tree_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "tree_hash": tree_hash,
        "success": True,
        "written_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _git_tree_hash(repo_root: Path) -> str:
    """working tree の現在の内容（追跡ファイル + untracked ファイル）を表す tree hash を返す.

    一時 index ファイル（`GIT_INDEX_FILE` 環境変数）を使って `git add -A` → `git write-tree`
    を実行することで、untracked ファイルを含む working tree の内容から tree hash を算出する。
    正規 index（`.git/index`）は変更しない。計算失敗時は空文字列を返す。

    TDD ワークフローでは pre-flight 実行時点で新規テストファイルが untracked 状態にあるのが
    常態であるため、旧実装（untracked があると空文字列を返す）では TDD PR でキャッシュが
    一度も hit しなかった（Issue #2696）。

    gitignore 済みファイルは `git add -A` の対象外のため tree hash に影響しない。
    commit 後の `<sha>^{tree}` と内容が一致するため、読み取り側（`_git_commit_tree_hash`）
    は変更なしで cache hit する。
    """
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_index_path = os.path.join(tmpdir, "tmp_index")
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = tmp_index_path

            # 一時 index に working tree の全ファイル（gitignore 除く）を追加
            add_proc = run_subprocess(
                ["git", "add", "-A"],
                cwd=repo_root,
                timeout=STANDARD_TIMEOUT_SEC,
                env=env,
            )
            if add_proc.returncode != 0:
                return ""

            # 一時 index の内容から tree hash を算出
            write_tree_proc = run_subprocess(
                ["git", "write-tree"],
                cwd=repo_root,
                timeout=QUICK_TIMEOUT_SEC,
                env=env,
            )
            if write_tree_proc.returncode != 0:
                return ""

            return (write_tree_proc.stdout or "").strip()
    except (FileNotFoundError, SubprocessTimeoutError, OSError):
        return ""


def _git_commit_tree_hash(repo_root: Path, sha_or_tree_ref: str) -> str:
    """指定 commit（または `<rev>^{tree}` 形式の tree 参照）の tree hash を返す.

    対象を解決できない場合（未 fetch の SHA・不正な rev 等）は空文字列を返す。
    """
    if not sha_or_tree_ref:
        return ""
    rev = sha_or_tree_ref if sha_or_tree_ref.endswith("^{tree}") else f"{sha_or_tree_ref}^{{tree}}"
    try:
        proc = run_subprocess(
            ["git", "rev-parse", rev],
            cwd=repo_root,
            timeout=QUICK_TIMEOUT_SEC,
        )
    except (FileNotFoundError, SubprocessTimeoutError):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


# ── マーカー出自追跡（Issue #2800） ──────────────────────────────────────────
#
# マーカー hit 時に「どのファイルが・いつ書かれたか」を一切記録していないと、
# 一度も pytest で検証されていない tree がマーカー hit で誤って通過した場合に
# 事後調査する手段がない（#2799 の実測ケース）。マーカーファイルの mtime を
# 「書き込み時刻」の権威ソースとして使う（マーカー JSON 自身の `written_at` は
# 内容が壊れていても mtime は改変されない限り信頼できるため）。


def marker_origin(repo_root: Path, marker_path: Path) -> tuple[str | None, str | None]:
    """マーカーファイルの出自（相対パス・書き込み時刻）を返す（Issue #2800）.

    マーカーファイルが存在しない・stat 失敗の場合は ``(None, None)`` を返す。

    Returns:
        ``(repo_root からの相対パス文字列, mtime から得た ISO8601 文字列)``
    """
    try:
        mtime = marker_path.stat().st_mtime
    except OSError:
        return None, None
    try:
        rel_path = str(marker_path.relative_to(repo_root))
    except ValueError:
        rel_path = str(marker_path)
    written_at = datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return rel_path, written_at
