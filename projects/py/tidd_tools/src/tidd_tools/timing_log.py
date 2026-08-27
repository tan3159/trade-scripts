"""共通タイミング記録モジュール（Issue #2933・SQLite化 #3084）.

所要時間サマリの記録が 5 系統のファイル（``ai-review-timing/issue-N.jsonl``・
``ai-review-timing/PR-num.jsonl``・``pre-flight/preflight-record.json``・
``pre-flight/issue-N.jsonl``・``$STATE_DIR/timing.json``）と 2 種のキー体系
（issue-N / pr-N）に分散し、書き手ごとに独立した追記ロジックを持つせいで、
mark 打ち忘れ・スプリアス mark・中断再着手時の過去試行混入・経路単位の書き込み
抜けといった非対称バグが再発していた（詳細:
``docs/decisions/2026-08-01-timing-record-redesign.md``）。

本モジュールは per-issue の統一イベントログへ単一 API で追記する共通記録モジュールを提供する。
Issue #3084 でストレージ層を JSONL から SQLite（WAL・単一 DB）へ差し替えた。
既存 JSONL は issue_key への初アクセス時に lazy migration される。

スキーマ・フィールド定義・step 名一覧・サンプルレコード・読み方:
``docs/reference/timing-db-schema.md``（SQLite 移行後）・``docs/reference/timing-log-schema.md``（JSONL 形式資料）

**スコープ（Issue #2933）:** 本モジュールは新規追加のみ。既存書き込み元
（``ai_review/timing_steps.py``・``pre_flight.py``・``ai_review/timing.py`` 等）
の移行と ``merge_summary.py`` 読み側の対応は後続 Issue（#2934・#2935）のスコープ。

**移行状況（Issue #2934・#3126・#2936）:** ``pre_flight.py``・``ai_review/timing.py``・
``issue_next_timing.py``・``ai_review/timing_steps.py``（``append_step_timing()``）が
``record_event_safe()`` 経由で統一日誌へ記録する。旧 5 系統の書き込みのうち
``ai-review-timing/<PR番号>.jsonl``・``pre-flight/preflight-record.json``・
``pre-flight/issue-<N>.jsonl``・``ai-reviewer/pr-<N>/timing.json`` は #2936 で撤去し、
``ai-review-timing/issue-<N>.jsonl``（``mark_boundary`` 直書き）は #3340 で撤去
（参照元 hook を統一日誌参照へ移行）した。
読み側 ``merge_summary.py`` のフォールバック撤去は #3322。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tidd_tools.shared.paths import cache_dir

_LOG_SUBDIR = "timing-events"
_DB_FILENAME = "timing.db"

_DDL_CREATE_EVENTS = """\
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_key TEXT NOT NULL,
    attempt_id INTEGER NOT NULL,
    step TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    repo TEXT
)
"""

_DDL_CREATE_INDEX = """\
CREATE INDEX IF NOT EXISTS idx_events_issue_key_id ON events (issue_key, id)
"""

_SQL_INSERT_EVENT = (
    "INSERT INTO events (issue_key, attempt_id, step, kind, source, timestamp, meta, repo) VALUES (?,?,?,?,?,?,?,?)"
)

# `.claude/hooks/label-pr.py` の `_session_repo()`（Issue #2865）と同型の正規表現。
# ssh (`git@github.com:owner/repo.git`) / https (`https://github.com/owner/repo.git`)
# いずれの remote URL 形式にも対応する（Issue #3588）。
_REMOTE_OWNER_REPO_RE = re.compile(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$")

# `~/.ssh/config` の Host エイリアス（例: `git@github-being:owner/repo.git`）経由の
# remote URL からも owner/repo を解決する（Issue #3949）。エイリアスのホスト名は
# `github.com` 固定にできないため、scp-like 構文（`user@host:path`）であることのみで
# 判定する。`https://gitlab.com/...` のような無関係な URL 形式（`user@host:` の形を
# 取らない）は誤って解決しない。`.claude/hooks/_lib/hook_io.py` と同型（二重実装の
# 一致は `test_timing_log_hook_io_repo_contract.py` で機械検証する）。
_SSH_ALIAS_OWNER_REPO_RE = re.compile(r"^[^@/\s]+@[^:/\s]+:([^/]+/[^/]+?)(?:\.git)?/?$")

# スキーマで許可された step 名の集合（Issue #2933 やること4項目目）。
# `/issue-next` SKILL の STEP 境界（docs/reference/issue-next-loop-operations.md
# 「mark 語彙一覧」）を初期集合として採用する。既存 5 系統の書き込み元を本モジュールへ
# 移行する際（#2934）は、移行対象の step 名をここに追加すること。
KNOWN_STEPS: frozenset[str] = frozenset(
    {
        "step0-pr-limit-check",
        "step1-confirmed",
        "step1.5-quality-check",
        "step1.5.5-duplicate-triage",
        "step1.7-conflict-check",
        "step2-implementation",
        "step2-branch-created",
        # 実装フェーズ（step2-branch-created ～ 最初の step3-preflight-start）内の
        # 細粒度境界（Issue #3940）。record-timing-boundaries.py が git commit 成功時の
        # ファイル分類・pytest 起動検知から自動記録する。1 Issue 内で繰り返し発生するため
        # 冪等スキップせず毎回追記する（record_event_safe 相当）。
        "step2-test-committed",
        "step2-test-run",
        "step2-impl-committed",
        "step3-preflight-start",
        "step3-preflight-end",
        "step3-rework-start",
        "step4-pr-created",
        "step5-fix-start",
        "step5-fix-end",
        "step5-airview-start",
        "step5-airview-end",
        "step5-aiconfirm-start",
        "step5-aiconfirm-end",
        "step6-merge-start",
        "step6-merged",
        "step6-cleanup-done",
        # ai_review/timing.py save_timing() が記録する verdict 確定イベント（Issue #2934）。
        # /issue-next SKILL の STEP 境界とは別カテゴリ（PR 単位の判定確定点）。
        "ai-review-verdict",
        # ai_review/timing_steps.py append_step_timing() が記録する PR 単位ステップ計測
        # （Issue #3126）。measure_step()/_step() 呼び出し箇所（core.py・backends.py・
        # approve_flow.py・test_statuses.py・gates.py・pre_flight.py）が渡す step 名。
        "backend-selection",
        "pr-context-collection",
        "backend-subprocess-execution",
        "verdict-extraction",
        "test-plan-gate",
        "yaru-evidence-tick",
        "issue-exhaustion-gate",
        "auto-merge",
        "commit-status-check",
        "preflight.context-budget",
        "preflight.pytest",
        "preflight.jest",
        "preflight.ruff-format",
        "preflight.ruff-lint",
        "preflight.mypy",
        "preflight.ruff-format-hooks",
        "preflight.ruff-lint-hooks",
        "preflight.mypy-hooks",
        "preflight.gherkin-lint",
        "preflight.mermaid-lint",
        "preflight.health-check",
        "preflight.prettier-css",
        "preflight.stylelint-css",
        "impl-delegation-used",
    }
)

# `start_attempt` が書き込む attempt 境界イベント専用の step 名（Issue #2933）。
# ユーザー入力を経由しないため KNOWN_STEPS の検証対象には含めない。
_ATTEMPT_START_STEP = "attempt-start"


class UnknownStepError(ValueError):
    """`KNOWN_STEPS` に含まれない step 名が指定されたときに送出される."""


def _db_path() -> Path:
    """単一 SQLite DB のパスを返す（Issue #3084）."""
    return cache_dir() / _LOG_SUBDIR / _DB_FILENAME


def _log_path(issue_key: str) -> Path:
    """per-issue JSONL パスを返す（lazy migration 用途で残す・Issue #3084）."""
    return cache_dir() / _LOG_SUBDIR / f"{issue_key}.jsonl"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_current_repo(cwd: str | None = None) -> str | None:
    """`git remote get-url origin` から現在のリポジトリ（`owner/repo`）を解決する（Issue #3588）.

    `.claude/hooks/label-pr.py` の `_session_repo()`（Issue #2865）と同型の実装。
    `.claude/hooks/_lib/hook_io.py` にも stdlib のみで同等のロジックを重複実装している
    （二重実装の同期は契約テストで保証する）。`~/.ssh/config` の Host エイリアス経由の
    origin にも対応する（Issue #3949）。取得・解析に失敗した場合は None を返し、
    呼び出し元は repo=NULL で記録する（`record_event_safe` と同じ fail-open 方針）。

    Args:
        cwd: git コマンドを実行するディレクトリ。None ならプロセスの現在の cwd を使う。

    Returns:
        ``"owner/repo"`` 文字列。取得・解析に失敗した場合は None。
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
            cwd=cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, subprocess.CalledProcessError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    match = _REMOTE_OWNER_REPO_RE.search(url)
    if match:
        return match.group(1)
    match = _SSH_ALIAS_OWNER_REPO_RE.match(url)
    if not match:
        return None
    return match.group(1)


def _migrate_add_repo_column(con: sqlite3.Connection) -> None:
    """events テーブルに repo カラムを冪等に追加する（Issue #3588）.

    新規 DB では `_DDL_CREATE_EVENTS` に repo カラムが含まれるため不要だが、
    既存 DB（repo カラムなしで作成済み）には ALTER TABLE で追加する。既存行は
    NULL のまま残る（SQLite の ALTER TABLE ADD COLUMN の既定動作）。2 回目以降の
    接続では列が既に存在するため何もしない。
    """
    columns = {row[1] for row in con.execute("PRAGMA table_info(events)").fetchall()}
    if "repo" not in columns:
        con.execute("ALTER TABLE events ADD COLUMN repo TEXT")
        con.commit()


@contextmanager
def _connect() -> Generator[sqlite3.Connection, None, None]:
    """SQLite DB に接続し PRAGMA を設定するコンテキストマネージャ（Issue #3084）.

    WAL モード・busy_timeout を毎接続で設定する。初回接続時に DDL を実行し
    テーブル・インデックス・user_version を作成する。

    **Issue #3685:** 接続クローズ前に ``PRAGMA wal_checkpoint(TRUNCATE)`` を実行し、
    WAL の書き込みを DB 本体ファイルへ追い出してから閉じる。WAL にのみ残った行は
    ``require-quality-check.py`` の ``has_timing_event``（新規 ``sqlite3.connect``）が
    WSL2 等で ``-shm`` が破棄されると読めないため、DB ファイル単体から読めるようにする。
    """
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db), timeout=5.0)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        # 初回作成時のみ DDL を実行し user_version をセット
        con.execute(_DDL_CREATE_EVENTS)
        con.execute(_DDL_CREATE_INDEX)
        _migrate_add_repo_column(con)
        if con.execute("PRAGMA user_version").fetchone()[0] == 0:
            con.execute("PRAGMA user_version=1")
        con.commit()
        yield con
    finally:
        # Issue #3685: WAL の書き込みを DB 本体へ checkpoint してから閉じる。
        # 読み取り専用接続の暗黙トランザクションを終了してから checkpoint を実行する。
        # checkpoint 失敗は記録・読み出し自体を失敗させない（fail-open・#3685）。
        try:
            con.commit()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        con.close()


def _read_valid_jsonl_records(jsonl_path: Path) -> list[dict[str, Any]] | None:
    """JSONL を読み込み有効行（dict の行）のみを返す。読み込み失敗時は None を返す."""
    try:
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _insert_records_dedup(issue_key: str, records: list[dict[str, Any]], con: sqlite3.Connection) -> None:
    """DB 未登録行（step + timestamp で判定）のみを INSERT する（Issue #3138 重複防止）.

    バッチ内部の同一 (step, timestamp) 行も 2 行目以降を skip する（Issue #3150）.
    events テーブルに UNIQUE 制約がないため、ここで重複除去しないと
    ``read_events`` の返り値と merge-summary の集計が水増しされる。
    """
    existing_pairs: set[tuple[str, str]] = set()
    for row in con.execute("SELECT step, timestamp FROM events WHERE issue_key = ?", (issue_key,)).fetchall():
        existing_pairs.add((row[0], row[1]))

    # バッチ内 dedup（#3150）: INSERT 時に (step, timestamp) を seen へ追加し、
    # 同一ペアの 2 行目以降を skip する。
    seen_pairs: set[tuple[str, str]] = set()
    new_records = []
    for r in records:
        pair = (r.get("step", ""), r.get("timestamp", ""))
        if pair in existing_pairs or pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        new_records.append(r)
    if not new_records:
        return
    with con:
        for rec in new_records:
            con.execute(
                _SQL_INSERT_EVENT,
                (
                    issue_key,
                    rec.get("attempt_id", 1),
                    rec.get("step", ""),
                    rec.get("kind", ""),
                    rec.get("source", ""),
                    rec.get("timestamp", ""),
                    json.dumps(rec.get("meta", {}), ensure_ascii=False),
                    rec.get("repo"),
                ),
            )


def _migrate_jsonl_reimport(issue_key: str, jsonl_path: Path, imported_path: Path, con: sqlite3.Connection) -> None:
    """ケース A: migration 済み後に旧コードが再作成した JSONL を DB に取り込む（Issue #3138）.

    DB 未登録行のみを INSERT し、JSONL 内容を ``.imported`` に追記してから JSONL を削除する。
    """
    records = _read_valid_jsonl_records(jsonl_path)
    if records is None:
        return
    if not records:
        with suppress(OSError):
            jsonl_path.unlink()
        return

    _insert_records_dedup(issue_key, records, con)

    # .imported へ有効行を追記してから JSONL を削除する
    with imported_path.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with suppress(OSError):
        jsonl_path.unlink()


def _migrate_jsonl_initial(issue_key: str, jsonl_path: Path, imported_path: Path, con: sqlite3.Connection) -> None:
    """ケース B: 初回 migration（DB にレコードがない場合のみ）を行う（Issue #3084）.

    DB にレコードが既にある場合は何もしない（新コードが書いた JSONL のため）。
    """
    existing_count = con.execute("SELECT COUNT(*) FROM events WHERE issue_key = ?", (issue_key,)).fetchone()[0]
    if existing_count > 0:
        return

    records = _read_valid_jsonl_records(jsonl_path)
    if records is None:
        return

    # バッチ内重複（同一 step + timestamp）も除去して INSERT する（Issue #3150）
    _insert_records_dedup(issue_key, records, con)

    # rename（rename は実質的にアトミック操作）
    jsonl_path.rename(imported_path)


def _migrate_jsonl_if_needed(issue_key: str, con: sqlite3.Connection) -> None:
    """JSONL が存在すれば DB 未登録行を取り込み、JSONL を処理する（Issue #3084・#3138）.

    Issue #3138 修正: ``.imported`` が存在する（migration 済み）状態で旧コードが JSONL を
    再作成した場合も、次回アクセス時に DB 未登録行を取り込む。

    **ケース A: ``.imported`` が存在する** → `_migrate_jsonl_reimport` を呼ぶ
    **ケース B: ``.imported`` が存在しない** → `_migrate_jsonl_initial` を呼ぶ
    """
    jsonl_path = _log_path(issue_key)
    if not jsonl_path.exists():
        return

    imported_path = jsonl_path.with_suffix(".jsonl.imported")

    if imported_path.exists():
        _migrate_jsonl_reimport(issue_key, jsonl_path, imported_path, con)
    else:
        _migrate_jsonl_initial(issue_key, jsonl_path, imported_path, con)


def _last_attempt_id(issue_key: str, con: sqlite3.Connection) -> int:
    """DB から最終 attempt_id を取得する（未記録・NULL は 0 扱い・Issue #3084）."""
    row = con.execute(
        "SELECT attempt_id FROM events WHERE issue_key = ? ORDER BY id DESC LIMIT 1",
        (issue_key,),
    ).fetchone()
    if row is None or row[0] is None:
        return 0
    return row[0] if isinstance(row[0], int) else 0


def _insert_event(issue_key: str, record: dict[str, Any], con: sqlite3.Connection) -> None:
    """1 イベントを events テーブルへ INSERT する（Issue #3084・#3588 で repo 列を追加）."""
    meta_json = json.dumps(record.get("meta", {}), ensure_ascii=False)
    with con:
        con.execute(
            _SQL_INSERT_EVENT,
            (
                issue_key,
                record["attempt_id"],
                record["step"],
                record["kind"],
                record["source"],
                record["timestamp"],
                meta_json,
                record.get("repo"),
            ),
        )


def _append_jsonl(issue_key: str, record: dict[str, Any]) -> None:
    """per-issue JSONL にも追記する（二重書き込み・#2936 撤去予定）.

    既存テスト（test_timing_log.py・test_issue_2933.py 等）が JSONL ファイルへの
    直接書き込みを検証しているため、SQLite への差し替え（Issue #3084）後も JSONL への
    書き込みを維持する。JSONL の撤去は #2936 のスコープ。
    """
    path = _log_path(issue_key)
    # migration 済みの場合（.imported が存在）は書き込まない
    imported_path = path.with_suffix(".jsonl.imported")
    if imported_path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def start_attempt(issue_key: str) -> int:
    """新規 attempt を開始し attempt_id を採番する（Issue #2933 やること2項目目）.

    直近の attempt_id + 1 を新 attempt_id として使う（未記録時は 1）。
    採番と同時に attempt 境界イベントを DB へ書き込むため、
    以後クラッシュしても「どこで attempt が切り替わったか」がログから復元できる
    （中断・再着手の境界をスキーマで表現する）。

    issue_key への初アクセス時に既存 JSONL の lazy migration を実行する（Issue #3084）。
    """
    with _connect() as con:
        _migrate_jsonl_if_needed(issue_key, con)
        attempt_id = _last_attempt_id(issue_key, con) + 1
        record: dict[str, Any] = {
            "attempt_id": attempt_id,
            "step": _ATTEMPT_START_STEP,
            "kind": "attempt-start",
            "source": "timing_log",
            "timestamp": _now_iso(),
            "meta": {},
            "repo": get_current_repo(),
        }
        _insert_event(issue_key, record, con)
    _append_jsonl(issue_key, record)
    return attempt_id


def record_event(
    issue_key: str,
    step: str,
    kind: str,
    source: str,
    meta: dict[str, Any] | None = None,
) -> None:
    """統一イベントログに 1 イベントを追記する（Issue #2933 やること1項目目）.

    `step` が `KNOWN_STEPS` に含まれない場合は `UnknownStepError` を送出し、
    DB へは一切書き込まない（やること4項目目: スキーマ未定義 step の拒否）。

    attempt_id は DB の直近 attempt_id を自動的に引き継ぐ
    （`start_attempt` が一度も呼ばれていない場合は 1 として扱う・やること2項目目）。

    issue_key への初アクセス時に既存 JSONL の lazy migration を実行する（Issue #3084）。
    """
    if step not in KNOWN_STEPS:
        raise UnknownStepError(f"未定義の step 名です: {step!r}（tidd_tools.timing_log.KNOWN_STEPS を参照）")
    with _connect() as con:
        _migrate_jsonl_if_needed(issue_key, con)
        attempt_id = _last_attempt_id(issue_key, con) or 1
        record: dict[str, Any] = {
            "attempt_id": attempt_id,
            "step": step,
            "kind": kind,
            "source": source,
            "timestamp": _now_iso(),
            "meta": meta or {},
            "repo": get_current_repo(),
        }
        _insert_event(issue_key, record, con)
    _append_jsonl(issue_key, record)


def read_events(issue_key: str) -> list[dict[str, Any]]:
    """指定 Issue の統一イベントログを読み込む（Issue #2935 やること1項目目: 読み側の第一ソース化）.

    DB が存在しない・対象 issue_key のレコードがない場合は空リストを返す。
    返り値のレコード形状は JSONL 版に repo を加えたもの:
    attempt_id・step・kind・source・timestamp・meta・repo の 7 キー dict（Issue #3084・#3588）。

    issue_key への初アクセス時に既存 JSONL の lazy migration を実行する（Issue #3084）。

    **Issue #3588:** 現在のリポジトリ（`get_current_repo()`）の行と、migration 前の
    legacy 行（`repo IS NULL`）のみを返す（`(repo, issue_key)` の複合キー）。
    別リポジトリの同一 issue_key 行は混入しない。全リポジトリ横断で読む用途には
    `read_all_events()` を使う。

    呼び出し元自身の repo が解決できない場合（git 管理外ディレクトリからの呼び出し等）は、
    どの行が「自分のもの」か判断できないため repo フィルタを適用せず、issue_key のみで
    絞り込む（fail-open。`record_event_safe` と同じ方針）。
    """
    current_repo = get_current_repo()
    with _connect() as con:
        _migrate_jsonl_if_needed(issue_key, con)
        if current_repo is None:
            rows = con.execute(
                "SELECT attempt_id, step, kind, source, timestamp, meta, repo FROM events "
                "WHERE issue_key = ? ORDER BY id",
                (issue_key,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT attempt_id, step, kind, source, timestamp, meta, repo FROM events "
                "WHERE issue_key = ? AND (repo = ? OR repo IS NULL) ORDER BY id",
                (issue_key, current_repo),
            ).fetchall()

    records: list[dict[str, Any]] = []
    for row in rows:
        attempt_id, step, kind, source, timestamp, meta_json, repo = row
        try:
            meta = json.loads(meta_json)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        records.append(
            {
                "attempt_id": attempt_id,
                "step": step,
                "kind": kind,
                "source": source,
                "timestamp": timestamp,
                "meta": meta,
                "repo": repo,
            }
        )
    return records


def read_all_events() -> list[dict[str, Any]]:
    """全 Issue キーの統一イベントログを時系列順で読み込む（Issue #3295）.

    `tidd ai-review-timing --report` の集計用。DB が存在しない場合は空リストを返す。
    レコード形状は :func:`read_events` と同一（issue_key キーを付加）。

    **Issue #3588:** `read_events()` と異なり repo でのフィルタは行わず、全リポジトリの
    行をそのまま返す（横断集計用途）。各レコードに `repo` フィールドを含めるため、
    呼び出し側は `(repo, issue_key)` の複合キーで区別できる。
    """
    db = _db_path()
    if not db.is_file():
        return []
    with _connect() as con:
        rows = con.execute(
            "SELECT issue_key, attempt_id, step, kind, source, timestamp, meta, repo FROM events ORDER BY id"
        ).fetchall()

    records: list[dict[str, Any]] = []
    for row in rows:
        issue_key, attempt_id, step, kind, source, timestamp, meta_json, repo = row
        try:
            meta = json.loads(meta_json)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        records.append(
            {
                "issue_key": issue_key,
                "attempt_id": attempt_id,
                "step": step,
                "kind": kind,
                "source": source,
                "timestamp": timestamp,
                "meta": meta,
                "repo": repo,
            }
        )
    return records


def record_event_safe(
    issue_key: str,
    step: str,
    kind: str,
    source: str,
    meta: dict[str, Any] | None = None,
) -> None:
    """`record_event` を fail-open で呼び出す（Issue #2934 やること1-3項目目: 移行元 3 箇所の共通経路）.

    既存 5 系統の書き込み元（``pre_flight.py``・``ai_review/timing.py``・
    ``issue_next_timing.py``）は、本体の pre-flight チェック・レビュー・mark 処理を
    統一イベントログの記録失敗（未定義 step・DB 書き込み不可等）で止めては
    ならないため、本関数を経由して呼び出す。失敗時は例外を送出せず stderr に警告のみ出す。

    ``measure_step``（``ai_review/timing_steps.py``）と同じ規約で
    ``PYTEST_CURRENT_TEST`` が設定されている間（既定の pytest 実行時）は時刻取得・
    DB I/O を一切行わずに no-op で戻る。書き込み動作そのものをテストしたい場合は
    テスト内で ``monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)`` を呼ぶこと
    （既存モジュールテストが実ユーザーの ``~/.cache`` を汚染しないための安全装置）。
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        record_event(issue_key, step, kind, source, meta)
    except (UnknownStepError, OSError, sqlite3.Error) as exc:
        print(f"WARN: timing_log: 統一イベントログへの記録に失敗しました（{issue_key}/{step}）: {exc}", file=sys.stderr)


def record_event_once_safe(
    issue_key: str,
    step: str,
    kind: str,
    source: str,
    meta: dict[str, Any] | None = None,
) -> None:
    """`record_event_safe` を冪等化して呼び出す（Issue #3516）.

    ``issue_key`` の ``step`` が統一日誌に既に 1 件でも存在する場合は追記しない。
    ai-review の自己記録系（``step5-airview-*``・``step6-merge-*``）が、レビュー
    再実行・SHA キャッシュヒット等で同じ境界を複数回通過しても二重記録しないように
    するための呼び出し口。

    ``record_event_safe`` と同じく ``PYTEST_CURRENT_TEST`` が設定されている間は
    冪等性チェックの読み取りを含め一切の I/O を行わず no-op で戻る（実ユーザーの
    ``~/.cache`` を汚染しないための安全装置。書き込み動作をテストしたい場合は
    ``monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)`` を呼ぶこと）。
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        existing = read_events(issue_key)
    except (OSError, sqlite3.Error):
        existing = []
    if any(event.get("step") == step for event in existing):
        return
    record_event_safe(issue_key, step, kind, source, meta)


def start_attempt_safe(issue_key: str) -> int | None:
    """`start_attempt` を fail-open で呼び出す（Issue #3384）.

    `record_event_safe` と同じく、計測記録の失敗で本体フロー（``issue-next-state init``
    等）を止めない。失敗時は例外を送出せず stderr に警告のみ出して ``None`` を返す。
    ``PYTEST_CURRENT_TEST`` が設定されている間（既定の pytest 実行時）は no-op で戻る。
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        return start_attempt(issue_key)
    except (UnknownStepError, OSError, sqlite3.Error) as exc:
        print(
            f"WARN: timing_log: 計測記録（attempt 開始）に失敗しました（{issue_key}）: {exc}",
            file=sys.stderr,
        )
        return None
