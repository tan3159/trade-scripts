"""`tidd issue-next-state` サブコマンド（旧 `scripts/issue-next-state.sh` の Python 移植）.

`issue-next` スキルのバッチ状態を JSON で管理するヘルパー。

サブコマンド:
- `init [--enforce-session-limit] <current> [queue...] [--unattended]` 状態ファイルを初期化する
    （Issue 番号ごとに分離したファイルに書き込む）
    `--enforce-session-limit`（#3626）: `/issue-next` 引数なしモード専用の session-count gate。
    state ファイル作成前に同時実行セッション数上限（`ISSUE_NEXT_MAX_SESSIONS`・デフォルト 2）を
    機械判定し、上限到達時は state・ロック・ラベルを一切作らず exit 1 でブロックする。
    `--unattended` 付与時は `unattended: true` を状態に永続化する（#3633）
- `is-unattended [issue]` unattended モードかを判定する（#3633）
    exit 0 = `unattended: true`（unattended モード）、exit 1 = false / キー不在 / state 不在
    `[issue]` 省略時の解決規則は `current`/`queue` と同じ（候補 1 件なら自動解決）
- `queue [issue]` 残キューを空白区切りで stdout に出力する
- `current [issue]` current_issue を stdout に出力する
- `consume [issue]` キュー先頭を current_issue にして消費した番号を stdout に出力する
    （`unattended` 等の未知キーは保持する）
- `clear [issue]` 状態ファイルを削除する
- `migrate` 旧形式（フラットファイル / さらに古いキューファイル）から新形式に移行する
- `check-liveness [issue] [--self-session <session_id>]` TTL 付きで「作業中か」を判定する
    exit 1 = TTL 内の liveness あり（作業中）、exit 0 = 作業中でない（stale・不在・破損）
    `--self-session`（#4221）指定時は所有者識別を追加する: state が active（TTL 内）の場合、
    state の `session_id`（`stamp-issue-next-session.py` が記録）と `--self-session` を照合し、
    一致すれば exit 0（自セッション所有・継続してよい）、不一致なら exit 1（別セッション所有）、
    `session_id` が欠落・不正な値なら exit 2（所有者情報が不整合）を返す。汎用 worker が
    専用 issue-implementer の代替として動く場合等、親セッション自身が `init` で作成した
    state・`🔧 in-progress` ラベルを「別セッションの着手状態」と誤認して処理を止めないための
    識別手段（詳細は `_cmd_check_liveness()` の docstring 参照）
- `next-unattended [--exclude N ...] [--no-conflict-filter]` Open Issue を優先順位順に取得し
    次に着手すべき 1 件を stdout に返す（#2874）
    `🙋 needs-human-input`/`🔧 in-progress` ラベル付きを除外し、
    `priority: critical`→`high`→`medium`→`low`（ラベルなしは最低優先度）の順、
    同一 priority 内は Issue 番号昇順でソートする。対象 0 件・gh コマンド失敗時は
    stdout 空のまま exit 0（終端シグナル。`/issue-next-all` が実行中増分を検知するために使う）。
    `--exclude` はラベルに関わらず指定した Issue 番号を選定対象から除外する（`/issue-next` の
    skip 終端・#2452 はラベルを一切付与しない意図的仕様のため、呼び出し側が同一 Issue の
    無限再選定を防ぐために明示除外する用途・#2899 レビュー指摘）。
    GitHub ネイティブの依存関係（blocked-by）で `blockedBy.nodes[]` に state が OPEN の
    blocker を 1 件以上持つ Issue も候補から除外する（#3640）。`blockedBy` キー欠損・
    nodes が list でない等の判定不能時は除外しない（フェイルオープン。`extract_issue_paths()`
    の「判定不能は除外しない」方針にそろえる）。`blocking`（この Issue がブロックしている側）は
    選定に使わない。
    GitHub ネイティブの親子関係（sub-issues）で `subIssuesSummary`（`total`/`completed`）が
    open なサブ Issue を 1 件以上示す Issue（サブ Issue へ分割済みの親 Issue＝Epic）も
    候補から除外する（#3997）。`subIssuesSummary` キー欠損・フィールドが int でない等の
    判定不能時は除外しない（フェイルオープン）。
    デフォルトでは着手中 OPEN PR の変更ファイルと Issue 本文のリポジトリ相対パスが重なる
    Issue を候補から除外する競合フィルタ（#3455）が有効。`--no-conflict-filter` で無効化できる
    （本文からのパス抽出は正規表現のみで行い LLM には渡さない・プロンプトインジェクション対策）
- `observe-pr <issue> <pr> [--actor <name>]` PR の現在状態（state/headRefOid/updatedAt）を
    `gh pr view` で取得し、actions ログへ `action="observe-pr"` エントリとして記録する（#4038）。
    `.claude/hooks/require-pr-state-drift-check.py`（`gh pr edit`/`merge`/`close` 実行前の
    PreToolUse hook）が、このエントリを「直近 observed PR 状態」の baseline として使い、
    実行直前に再取得した現在の PR 状態と compare-before-mutate 方式で照合する。詳細:
    `docs/reference/action-log-and-drift-check.md`

**Issue 番号スコープ（#2474）:** `init` は Issue 番号ごとに分離したファイルに書き込む。
`queue`/`current`/`consume`/`clear`/`check-liveness` は `[issue]` を省略した場合、
候補ファイルが 1 つだけならそれを対象にする（従来の単一ターミナル運用との後方互換）。
複数ターミナルが異なる Issue 番号を並行処理する場合は各コマンドで `[issue]` を明示することで
互いの状態ファイルを上書きしない。同時実行セッション数（session-count gate・#3457）の上限
判定は `init --enforce-session-limit` が `init` 実行前に行う（#3626）。

状態ファイル: `<root>/cache/issue-next-state/issue-<N>.json`
（旧形式 `<root>/cache/issue-next-state.json` は `migrate` で段階的に移行する）
JSON 構造:
    {"current_issue": int | None, "queue": [int, ...], "started_at": ISO8601, "last_active": ISO8601,
     "liveness_at": ISO8601, "unattended": bool, "session_id": str,
     "actions": [{"at": ISO8601, "actor": str, "action": str, "details": dict (任意)}, ...]}
    `unattended` は `init --unattended` で `true` になる（#3633）。旧形式 state（キーなし）は
    attended 扱い（後方互換）。`consume` でキューを進めても値は保持される。
    `session_id`（#3779）は本モジュール自身は書き込まない。CLI サブプロセスは Claude Code の
    session_id を知り得ないため、`init` 実行後に `.claude/hooks/stamp-issue-next-session.py`
    （PostToolUse hook）が後付けで記録する。`require-issue-next-completion.py`（Stop hook）が
    この識別子を使って別セッション所有の state を判定し、無関係なセッションの stop を誤って
    ブロックしないようにする。

**action log（#4037）:** `actions` は状態変更コマンド（`init`/`consume`）実行のたびに
`{"at": ISO8601, "actor": "orchestrator" | "subagent:<agent_type>", "action": str}` を
追記する監査ログである。現状 `issue_next_state.py` 内のコマンドはすべて `/issue-next`
オーケストレータ（main session）からのみ呼び出されるため `actor` は常に `"orchestrator"`
固定値になる（`subagent:<agent_type>` は後続の drift チェック機構実装時に使用する想定）。
`issue_next_timing.py` の `mark-quality-check-done`（`cache/issue-next-state/` とは別系統の
`timing_log` SQLite に記録する）からも `append_action()` 経由で同じ `actions` 配列へ追記する。
`clear` は state ファイルごと削除するため `actions` も同時に失われる（意図的な設計。
`## 設計の選択肢` で別ファイル分離案を不採用としたのと同じ理由でファイルのライフサイクルを
一致させている）。

**observe-pr / drift チェック（#4038）:** `observe-pr` が記録するエントリは
`action="observe-pr"`・`details={"pr_number": int, "state": str, "headRefOid": str,
"updatedAt": str}` を持つ（他の action エントリは `details` キーを持たない）。
`require-pr-state-drift-check.py` は `actions` を逆順に走査し、`action == "observe-pr"` かつ
`details` に `state`/`headRefOid`/`updatedAt` が揃っている最新エントリを baseline として使う。

環境変数:
- `ISSUE_NEXT_STATE_ROOT` 状態ファイル配置ベースのオーバーライド（テスト用）
- `ISSUE_NEXT_LIVENESS_TTL_SECONDS` check-liveness・session-count gate の TTL 秒数（デフォルト: 1800 秒）
- `ISSUE_NEXT_MAX_SESSIONS` `init --enforce-session-limit` の同時実行セッション数上限
  （デフォルト: 2・非数値/0 以下はデフォルトへフォールバック・#3457）

**多重着手防止ラベル（#2804）:** `init` 成功時に GitHub Issue へ `🔧 in-progress` ラベルを
付与し、`clear` 成功時に除去する（`tidd_tools.issue_progress_label` 経由。詳細は同モジュールの
docstring 参照）。`ISSUE_NEXT_STATE_ROOT` が設定されている場合（テスト隔離用）はこの
GitHub API 呼び出し自体をスキップする。

**分散ロックによる多重着手防止（#3452）:** `🔧 in-progress` ラベル（#2804）はラベル付与前の
TOCTOU（Time-of-check to time-of-use）レースを塞げない（複数マシンがほぼ同時に
`next-unattended` で同一 Issue を選定し、ラベルが付く前に両方が `init` を実行してしまう
可能性がある）。この隙間を塞ぐため、`init` は状態ファイル書き込み前に
`tidd_tools.issue_next_lock.acquire_lock()` で `refs/locks/issue-<N>` という git ref への
push を排他制御プリミティブとして使い、ロックを獲得できたプロセスのみが処理を継続する
（獲得失敗時は状態ファイルを書き換えず exit 1）。`clear` は成功時に
`tidd_tools.issue_next_lock.release_lock()` でロックを解放する。異常終了で解放されずに
残った stale ロックは `ISSUE_NEXT_LIVENESS_TTL_SECONDS`（デフォルト 1800 秒）超過かつ
`🔧 in-progress` ラベルなし・Open PR なしの場合に `acquire_lock()` 内部で自動解放される
（詳細は `tidd_tools.issue_next_lock` の docstring 参照）。

**ロック証跡ファイルによる worktree-add 側の機械強制（#4059）:** 上記の分散ロックは
「同名 ref への同時 push は 1 つしか成功しない」atomic 性のみを排他制御の根拠にしており、
`init` の exit code を呼び出し元（LLM）が正しく読んで従うことに実運用上依存してしまう
弱点がある。最終防衛線として、`init` は `acquire_lock()` 成功時に得た commit sha を
`tidd_tools.issue_next_lock.write_lock_evidence()` でローカル証跡ファイル
（`cache/issue-next-state/issue-<N>.lock`）へ書き込む。`tidd worktree-add` はこの証跡と
`refs/locks/issue-<N>` の remote 上の現在値を照合してから `git worktree add` を実行するため、
`init` を経ていない・他プロセスがロックを取り直した場合は worktree 自体が作成されない
（詳細は `tidd_tools.issue_next_lock` / `tidd_tools.worktree_add` の docstring 参照）。

**merge-summary marker ゲート（#2391）と park 免除（#2881・#3372）:** `clear` は
`current_issue` が設定されている場合、対応する `cache/merge-summary-emitted/<N>.marker`
の存在を要求し、未存在なら exit 1 でブロックする（マージ後の所要時間サマリ報告漏れ防止）。
ただし `current_issue` に対応する PR が一度も作成されていない park ケース（STEP 1.5-d・
STEP 1.7 skip 等 STEP 2 未到達）や、PR が作成された後 close されたが merge されなかった
park ケース（STEP 2 到達後・STEP 5 park）では `tidd merge-summary report --pr` を実行する
手段がないため、`_pr_ever_created_for_issue()` で PR の状態を確認しゲートを免除する。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from tidd_tools import issue_next_lock, issue_progress_label, merge_summary, timing_log
from tidd_tools.shared import gh_client
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError, SubprocessTimeoutError
from tidd_tools.shared.issue_body import extract_closes_issues
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)

STATE_FILENAME = "issue-next-state.json"  # 旧単一ファイル形式（#2474 で per-issue 形式に段階移行）
STATE_SUBDIR = "issue-next-state"  # per-issue ファイルを置くディレクトリ名（#2474）
LEGACY_FILENAME = "issue-next-queue"
DEFAULT_LIVENESS_TTL_SECONDS = 1800  # 30 分（/loop 5m /issue-next の 6 tick 分）
DEFAULT_MAX_SESSIONS = 2  # init --enforce-session-limit の同時実行セッション数上限（#3457）


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "issue-next-state",
        help="issue-next バッチ状態を JSON で管理する（旧 scripts/issue-next-state.sh）",
        description=__doc__,
    )
    add_common_flags(parser)
    sub = parser.add_subparsers(dest="action", required=True)

    p_init = sub.add_parser("init", help="状態ファイルを初期化する（Issue 番号ごとに分離）")
    p_init.add_argument(
        "--enforce-session-limit",
        action="store_true",
        help=(
            "init 前に同時実行セッション数上限（ISSUE_NEXT_MAX_SESSIONS・デフォルト 2）を機械判定し、"
            "上限到達時は state・ロック・ラベルを一切作らず exit 1 でブロックする"
            "（/issue-next 引数なしモード専用・#3457/#3626）"
        ),
    )
    p_init.add_argument("current", type=int, help="current_issue 番号")
    p_init.add_argument("queue", nargs="*", type=int, help="残キューの Issue 番号（任意）")
    p_init.add_argument(
        "--unattended",
        action="store_true",
        help="unattended モードとして状態に永続化する（#3633・分岐機械強制のための状態保存）",
    )

    p_is_unattended = sub.add_parser(
        "is-unattended",
        help="unattended モードかを判定する（exit 0 = true / exit 1 = false・不在・キーなし・#3633）",
    )
    p_is_unattended.add_argument(
        "issue",
        nargs="?",
        type=int,
        help="対象 Issue 番号（省略時は候補1件のみなら自動解決）",
    )

    p_queue = sub.add_parser("queue", help="残キューを空白区切りで出力する")
    p_queue.add_argument("issue", nargs="?", type=int, help="対象 Issue 番号（省略時は候補1件のみなら自動解決）")

    p_current = sub.add_parser("current", help="current_issue を出力する")
    p_current.add_argument("issue", nargs="?", type=int, help="対象 Issue 番号（省略時は候補1件のみなら自動解決）")

    p_consume = sub.add_parser("consume", help="キュー先頭を current_issue にして消費した番号を出力する")
    p_consume.add_argument("issue", nargs="?", type=int, help="対象 Issue 番号（省略時は候補1件のみなら自動解決）")

    p_clear = sub.add_parser("clear", help="状態ファイルを削除する")
    p_clear.add_argument("issue", nargs="?", type=int, help="対象 Issue 番号（省略時は候補1件のみなら自動解決）")

    sub.add_parser("migrate", help="旧形式（フラットファイル / さらに古いキューファイル）から新形式に移行する")

    p_liveness = sub.add_parser(
        "check-liveness",
        help="TTL 付きで作業中セッションを判定する（exit 1=作業中、exit 0=非作業中）",
    )
    p_liveness.add_argument(
        "issue",
        nargs="?",
        type=int,
        help="対象 Issue 番号（省略時は候補1件のみなら自動解決）",
    )
    p_liveness.add_argument(
        "--self-session",
        default=None,
        help=(
            "呼び出し元（worker）自身の session_id（#4221）。指定時は state ファイルの "
            "session_id と照合し、自セッション所有なら exit 0、別セッション所有なら "
            "exit 1、session_id 欠落/不正なら exit 2 を返す（未指定時は従来どおり "
            "所有者を問わず active なら exit 1）"
        ),
    )

    p_next_unattended = sub.add_parser(
        "next-unattended",
        help="Open Issue から優先順位順で次に着手すべき1件を選定する（0件なら空出力・exit 0）",
    )
    p_next_unattended.add_argument(
        "--exclude",
        type=int,
        nargs="*",
        default=[],
        metavar="N",
        help=(
            "ラベルに関わらず除外する Issue 番号（skip 終端・#2452 はラベルを付与しないため、"
            "呼び出し側が同一 Issue の無限再選定を防ぐための明示除外・#2899）"
        ),
    )
    p_next_unattended.add_argument(
        "--no-conflict-filter",
        dest="conflict_filter",
        action="store_false",
        default=True,
        help=(
            "着手中 OPEN PR の変更ファイルと Issue 本文のパスが重なる Issue を除外する"
            "競合フィルタ（#3455）を無効化し、priority 順の最優先を返す従来動作にする"
        ),
    )

    p_observe_pr = sub.add_parser(
        "observe-pr",
        help=(
            "PR の現在状態（state/headRefOid/updatedAt）を actions ログへ観測記録する"
            "（drift チェックの baseline・#4038）"
        ),
    )
    p_observe_pr.add_argument("issue", type=int, help="対象 Issue 番号")
    p_observe_pr.add_argument("pr", help="対象 PR 識別子（番号・URL・ブランチ名）")
    p_observe_pr.add_argument(
        "--actor",
        default=_ACTOR_ORCHESTRATOR,
        help="actions ログの actor フィールド（既定: orchestrator）",
    )

    parser.set_defaults(func=run_cli)


def _run_init_action(args: argparse.Namespace, state_dir: Path, per_issue_dir: Path) -> int:
    """`init` サブコマンドの実処理（session-count gate + 分散ロック獲得 + 状態ファイル書き込み・#3452）.

    `--enforce-session-limit` 指定時（`/issue-next` 引数なしモード相当・#3626）は、
    state ファイル作成前に同時実行セッション数の上限判定（session-count gate・#3457）を
    実行し、上限到達時は state・ロック・ラベルを一切作らず exit 1 でブロックする。
    引数ありモード（フラグなし）は従来どおり gate なしで進む。

    `run_cli` から切り出したヘルパー（ruff C901 対策・複雑度を run_cli 側に集中させない）。
    """
    if getattr(args, "enforce_session_limit", False):
        gate_rc = _cmd_check_liveness_scan(state_dir)
        if gate_rc != 0:
            return gate_rc
    if not issue_next_lock.acquire_lock(args.current):
        print(
            f"ERROR: Issue #{args.current} は他プロセスが着手中です（refs/locks/issue- の獲得に失敗しました）",
            file=sys.stderr,
        )
        return 1
    # #4059: acquire_lock() 成功時に得た commit sha をロック証跡ファイルへ書き込む
    # （worktree-add 側の最終防衛線・verify_lock_evidence() が参照する）。
    lock_sha = issue_next_lock.acquired_lock_sha(args.current)
    target = _issue_file(per_issue_dir, args.current)
    rc = _cmd_init(
        target,
        per_issue_dir,
        args.current,
        list(args.queue),
        unattended=getattr(args, "unattended", False),
        lock_sha=lock_sha,
    )
    if rc == 0:
        # Issue #3154: step1-confirmed を issue-next-session と issue-<N> の両キーに記録する。
        # merge-summary は issue-<N> キーから「Issue品質チェック」行の開始時刻を探すため、
        # 旧来の issue-next-session のみ記録では開始時刻が "-" になる。
        # #3340: mark_boundary（旧 jsonl）は撤去済み。統一日誌のみに記録する。
        # Issue #3384: issue-<N> キーには attempt 境界を記録してから step1-confirmed を
        # 記録する。同一 Issue の再着手時に前回 attempt のイベントが merge-summary に
        # 混入するのを防ぐ。issue-next-session は長期セッションキー（attempt 単位の
        # 集計対象外）のため start_attempt を呼ばない。
        timing_log.start_attempt_safe(f"issue-{args.current}")
        timing_log.record_event_safe(f"issue-{args.current}", "step1-confirmed", "point", "issue-next-timing")
        timing_log.record_event_safe("issue-next-session", "step1-confirmed", "point", "issue-next-timing")
        issue_progress_label.add_in_progress_label(args.current)
    return rc


def run_cli(args: argparse.Namespace) -> int:
    state_dir = _state_dir()
    legacy_flat_file = state_dir / STATE_FILENAME
    legacy_queue_file = state_dir / LEGACY_FILENAME
    per_issue_dir = state_dir / STATE_SUBDIR
    issue = getattr(args, "issue", None)

    if args.action == "init":
        return _run_init_action(args, state_dir, per_issue_dir)
    if args.action == "is-unattended":
        return _cmd_is_unattended(_resolve_target_file(state_dir, issue))
    if args.action == "queue":
        return _cmd_queue(_resolve_target_file(state_dir, issue))
    if args.action == "current":
        return _cmd_current(_resolve_target_file(state_dir, issue))
    if args.action == "consume":
        target = _resolve_target_file(state_dir, issue)
        previous = _candidate_current_issue(target)
        rc, next_issue = _cmd_consume(target)
        if rc == 0:
            _move_in_progress_label(previous, next_issue)
        return rc
    if args.action == "clear":
        target = _resolve_target_file(state_dir, issue)
        released_issue = _candidate_current_issue(target)
        if released_issue is None:
            released_issue = issue
        already_released = _in_progress_label_released(target)
        rc = _cmd_clear(target)
        if rc == 0 and released_issue is not None and not already_released:
            issue_progress_label.remove_in_progress_label(released_issue)
        if rc == 0 and released_issue is not None:
            issue_next_lock.release_lock(released_issue)
        return rc
    if args.action == "migrate":
        return _cmd_migrate(legacy_flat_file, state_dir, legacy_queue_file)
    if args.action == "check-liveness":
        # #3626: 引数なしの session-count gate は `init --enforce-session-limit` へ統合済み。
        # ここでは Issue 番号を明示した per-issue 判定に専念する（省略時は候補1件のみなら自動解決）。
        # #4221: `--self-session` 指定時は session_id による所有者識別を追加する。
        return _cmd_check_liveness(
            _resolve_target_file(state_dir, issue),
            self_session=getattr(args, "self_session", None),
        )
    if args.action == "next-unattended":
        return _cmd_next_unattended(
            frozenset(getattr(args, "exclude", None) or []),
            conflict_filter=getattr(args, "conflict_filter", True),
        )
    if args.action == "observe-pr":
        return _cmd_observe_pr(args.issue, args.pr, args.actor)
    print(f"ERROR: 未対応のサブコマンド: {args.action}", file=sys.stderr)
    return 1


# ── 各サブコマンド ──────────────────────────────────────────────────────────


_ACTOR_ORCHESTRATOR = "orchestrator"  # #4037: issue_next_state.py のコマンドは常にオーケストレータが呼ぶ


def _append_action(payload: dict[str, Any], actor: str, action: str, details: dict[str, Any] | None = None) -> None:
    """state payload の `actions` 配列にエントリを1件追記する（#4037・破壊的操作の action log）.

    既存の `actions` が list でない（旧 state・破損）場合は空リストから作り直す。
    `details`（#4038）を指定した場合はエントリに `details` キーとして含める。未指定時は
    従来どおり `details` キーを持たないエントリになる（後方互換）。
    """
    entries = payload.get("actions")
    if not isinstance(entries, list):
        entries = []
    entry: dict[str, Any] = {"at": _now_iso(), "actor": actor, "action": action}
    if details is not None:
        entry["details"] = details
    entries.append(entry)
    payload["actions"] = entries


def append_action(issue: int, actor: str, action: str, details: dict[str, Any] | None = None) -> int:
    """Issue番号 `issue` の per-issue 状態ファイルへ actions エントリを追記する（#4037）.

    `issue_next_state.py` 外の状態変更コマンド（`issue_next_timing.py` の
    `mark-quality-check-done` 等）から呼び出すための公開関数。対象の state ファイルが
    存在しない・破損している場合は何もせず 1 を返す（例外は発生させない。異常系
    Scenario: 未知の Issue 番号に対する呼び出しでも新たな例外を発生させない）。
    `details`（#4038）は `_append_action()` にそのまま渡す。
    """
    per_issue_dir = _state_dir() / STATE_SUBDIR
    target = _issue_file(per_issue_dir, issue)
    payload = _load_state(target)
    if payload is None:
        return 1
    now = _now_iso()
    payload["last_active"] = now
    payload["liveness_at"] = now
    _append_action(payload, actor, action, details=details)
    _atomic_write_json(target, payload)
    return 0


_OBSERVE_PR_FIELDS = ("state", "headRefOid", "updatedAt")


def _cmd_observe_pr(issue: int, pr_identifier: str, actor: str) -> int:
    """`observe-pr` サブコマンドの実処理（drift チェックの baseline 記録・#4038）.

    `gh pr view <pr_identifier> --json number,state,headRefOid,updatedAt` を実行し、
    結果を `actions` ログへ `action="observe-pr"` エントリとして記録する。記録した
    `details`（`state`/`headRefOid`/`updatedAt`）は `.claude/hooks/require-pr-state-drift-check.py`
    が `gh pr edit`/`merge`/`close` 実行前の compare-before-mutate 判定に使う。
    """
    try:
        result = run_subprocess(
            ["gh", "pr", "view", pr_identifier, "--json", "number,state,headRefOid,updatedAt"],
            timeout=15,
        )
    except (OSError, SubprocessTimeoutError) as exc:
        print(f"Error: gh pr view の実行に失敗しました: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        print(
            f"Error: gh pr view {pr_identifier} が失敗しました（exit {result.returncode}）: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    try:
        meta = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"Error: gh pr view の出力を解析できませんでした: {exc}", file=sys.stderr)
        return 1
    if not isinstance(meta, dict) or any(field not in meta for field in _OBSERVE_PR_FIELDS):
        print("Error: gh pr view の出力に必要なフィールドがありません", file=sys.stderr)
        return 1

    details = {"pr_number": meta.get("number")} | {field: meta.get(field) for field in _OBSERVE_PR_FIELDS}
    return append_action(issue, actor, "observe-pr", details=details)


def _cmd_init(
    state_file: Path,
    state_dir: Path,
    current: int,
    queue: list[int],
    *,
    unattended: bool = False,
    lock_sha: str | None = None,
) -> int:
    """状態ファイルを初期化する.

    `unattended`（#3633）は `--unattended` 指定時のみ `true` になり、`consume` でキューを
    進めても保持される。未指定（デフォルト）は `false` を書き込み、旧形式 state の後方互換の
    ため `_cmd_is_unattended` はキー不在も attended（exit 1）として扱う。

    `lock_sha`（#4059）が指定された場合、`issue_next_lock.write_lock_evidence()` で
    ロック証跡ファイルを書き込む。`acquire_lock` が monkeypatch で丸ごと差し替えられている
    テスト等、sha が取得できない場合（None）は証跡ファイルを書かない。
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    now = _now_iso()
    payload = {
        "current_issue": current,
        "queue": queue,
        "started_at": now,
        "last_active": now,
        "liveness_at": now,
        "unattended": unattended,
    }
    _append_action(payload, _ACTOR_ORCHESTRATOR, "init")
    _atomic_write_json(state_file, payload)
    if lock_sha is not None:
        issue_next_lock.write_lock_evidence(current, lock_sha)
    return 0


def _cmd_is_unattended(state_file: Path) -> int:
    """`unattended` モードかを exit code で判定する（#3633・分岐機械強制用）.

    exit 0 = `unattended: true`（unattended モード）、exit 1 = false / キー不在（旧形式・
    後方互換） / state 不在。stdout には何も出力しない（exit code のみが判定に使われる）。
    """
    payload = _load_state(state_file)
    if payload is None:
        return 1
    return 0 if payload.get("unattended") is True else 1


def is_unattended_for_issue(issue: int | None) -> bool:
    """Issue 番号に対応する state から unattended モードかを判定する（Issue #4149）.

    `_cmd_is_unattended` と同じ判定基準（`unattended: true` のみ True）を、サブプロセスを
    起動せず同一プロセス内で使えるライブラリ関数として提供する（`pre_flight` からの利用
    想定）。`issue` が `None`、state ファイル未初期化・`unattended` キー不在の場合は
    fail-safe で `False`（attended 扱い）を返す。
    """
    if issue is None:
        return False
    payload = _load_state(_resolve_target_file(_state_dir(), issue))
    if payload is None:
        return False
    return payload.get("unattended") is True


def _cmd_queue(state_file: Path) -> int:
    payload = _load_state(state_file)
    if payload is None:
        return 0
    queue = payload.get("queue", [])
    if isinstance(queue, list):
        print(" ".join(str(x) for x in queue))
    return 0


def _cmd_current(state_file: Path) -> int:
    payload = _load_state(state_file)
    if payload is None:
        return 0
    current = payload.get("current_issue")
    if current is None:
        return 0
    print(current)
    return 0


def _cmd_consume(state_file: Path) -> tuple[int, int | None]:
    """キュー先頭を取り出す。戻り値は (exit code, 次の Issue 番号 or None)."""
    payload = _load_state(state_file)
    if payload is None:
        print("Error: state file not found.", file=sys.stderr)
        return 1, None
    queue = payload.get("queue", [])
    if not isinstance(queue, list) or not queue:
        # キューが空はバッチの正常終端でありエラーではない（#2817）。呼び出し側は
        # stdout が空であることで終端を判定する
        print("==> queue is empty (batch finished).", file=sys.stderr)
        # ラベルはこの時点で解放されるため、後続 clear が存在しないラベルへ
        # DELETE を投げて誤解を招く WARN を出さないよう記録する
        payload["in_progress_label_released"] = True
        _append_action(payload, _ACTOR_ORCHESTRATOR, "consume")
        _atomic_write_json(state_file, payload)
        return 0, None
    head = queue[0]
    now = _now_iso()
    payload["current_issue"] = head
    payload["queue"] = queue[1:]
    payload["last_active"] = now
    payload["liveness_at"] = now
    _append_action(payload, _ACTOR_ORCHESTRATOR, "consume")
    _atomic_write_json(state_file, payload)
    print(head)
    return 0, head


def _cmd_clear(state_file: Path) -> int:
    """state ファイルを削除する（#2391: merge-summary marker ゲート付き / #2881・#3372: park 免除）.

    state の current_issue が設定されている場合、対応する
    ``cache/merge-summary-emitted/<N>.txt``（移行期は既存 ``<N>.marker`` も互換受理・#3387）が
    存在しなければ exit 1 でブロックする。clear 成功時はマーカー・txt を削除して
    次 Issue での誤検知を防ぐ。

    **#2881:** marker が未存在でも、``current_issue`` に対応する PR が一度も
    作成されていない（``_pr_ever_created_for_issue`` が False）場合はゲートを免除する。
    STEP 1.5-d・STEP 1.7 skip 等 STEP 2 未到達で park した Issue は PR が存在しないため
    ``tidd merge-summary report --pr`` を実行する手段がなく、免除しなければ恒久的に
    ブロックされてしまう。

    **#3372:** ``_pr_ever_created_for_issue`` は STEP 2 到達後・PR 作成後に STEP 5 で
    park する場合（PR は作成済みだが merge されず close されるだけ）も False を返すため、
    この分岐がそのまま同ケースの免除も兼ねる（`_cmd_clear` 自体の分岐は変更不要）。
    """
    # state ファイルが存在しない場合はゲート不要（noop）
    if not state_file.is_file():
        return 0

    payload = _load_state(state_file)
    current_issue = payload.get("current_issue") if payload else None

    if current_issue is not None:
        marker_dir = merge_summary._marker_dir()
        marker_txt = marker_dir / f"{current_issue}.txt"
        marker_legacy = marker_dir / f"{current_issue}.marker"
        if not (marker_txt.is_file() or marker_legacy.is_file()):
            if not _pr_ever_created_for_issue(current_issue):
                # PR が一度も作成されていない park ケース（#2881）はゲートを免除する
                state_file.unlink()
                return 0
            print(
                f"Error: Issue #{current_issue} の merge-summary report が未実行です。"
                f" 先に `tidd merge-summary report issue-{current_issue}` を実行してください。",
                file=sys.stderr,
            )
            return 1
        state_file.unlink()
        # txt・移行期マーカーを削除して次 Issue での誤検知を防ぐ（#2391・#3387）
        marker_txt.unlink(missing_ok=True)
        marker_legacy.unlink(missing_ok=True)
        return 0

    state_file.unlink()
    return 0


def _pr_ever_created_for_issue(issue_num: int) -> bool:
    """``issue_num`` に対応する PR が merge-summary marker ゲートを維持すべきか判定する（#2881 / #2915 / #3372）.

    ``gh pr list --state all --search "closes #<N> in:body"`` で open/closed/merged
    いずれの状態の PR も候補として取得する（`.claude/hooks/on-stop.py` の孤児 state
    検出ロジックと同じ検索条件）。

    **#2915:** GitHub の検索 API は上記クエリをフレーズ一致ではなく緩いトークン一致で
    評価するため、"closes" と "#<N>" が本文中の別々の場所にあるだけでもヒットしてしまう
    （例: 別 Issue 用 PR の本文に単なる横断参照として "refs #<N>" が含まれる場合）。
    これを避けるため、取得した各 PR の ``body`` を
    ``shared.issue_body.extract_closes_issues()``（closes/fixes/fix/resolves・
    大文字小文字無視・#3550）で後段フィルタし、実際に issue_num をクローズする語句が
    隣接している PR のみを「実装 PR」として扱う。

    **#3372:** フィルタ後の PR が 1 件以上見つかっても、その全てが ``state == "CLOSED"``
    （close 済み・未マージ）の場合は False を返す。STEP 2 到達後・PR 作成後に STEP 5 で
    park する場合（PR は作成済みだが merge されず close されるだけ）は
    ``tidd merge-summary report --pr`` を実行する手段が原理的にないため、merge 済み
    （``"MERGED"``）・まだ open のまま（``"OPEN"``）の PR が 1 件でも混じっていれば
    従来通り True（ゲート維持）を返し、この判定に影響しない。

    gh コマンド自体が失敗した場合（非ゼロ終了・タイムアウト・gh 未インストール・
    JSON パース失敗）は判定不能として True（PR が存在する扱い）を返す。これにより
    ゲートは安全側（従来通りブロック）にフォールバックし、判定不能を理由に
    本来ブロックすべきケースを誤って免除しない。
    """
    try:
        result = run_subprocess(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "all",
                "--search",
                f"closes #{issue_num} in:body",
                "--json",
                "number,body,state",
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
        return False
    matched = [
        entry
        for entry in data
        if isinstance(entry, dict)
        and isinstance(entry.get("body"), str)
        and issue_num in extract_closes_issues(entry["body"])
    ]
    if not matched:
        return False
    # #3372: 一致した PR が全て close 済み・未マージ（park）なら、merge-summary report を
    # 実行する見込みがないためゲートを維持する必要がない（False = 免除対象）
    return not all(entry.get("state") == "CLOSED" for entry in matched)


def _resolve_liveness_ttl() -> int:
    """`ISSUE_NEXT_LIVENESS_TTL_SECONDS` から TTL を取得する（非数値・負値はデフォルトへ）."""
    ttl_raw = os.environ.get("ISSUE_NEXT_LIVENESS_TTL_SECONDS", "")
    try:
        ttl = int(ttl_raw)
        if ttl <= 0:
            raise ValueError("TTL must be positive")
    except (ValueError, TypeError):
        ttl = DEFAULT_LIVENESS_TTL_SECONDS
    return ttl


def _resolve_max_sessions() -> int:
    """`ISSUE_NEXT_MAX_SESSIONS` から同時実行セッション数の上限を取得する（#3457）.

    非数値・0 以下は DEFAULT_MAX_SESSIONS にフォールバックする（誤設定フェイルセーフ）。
    """
    raw = os.environ.get("ISSUE_NEXT_MAX_SESSIONS", "")
    try:
        max_sessions = int(raw)
        if max_sessions <= 0:
            raise ValueError("max sessions must be positive")
    except (ValueError, TypeError):
        max_sessions = DEFAULT_MAX_SESSIONS
    return max_sessions


def _parse_liveness_timestamp(raw: object) -> datetime | None:
    """ISO8601 文字列を aware datetime に変換する（不正値は None）."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    # naive datetime（tz 情報なし）は UTC とみなして aware に変換する。
    # naive のまま aware datetime と比較すると TypeError が発生するため防御する（#2389）。
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _latest_liveness(payload: dict[str, Any]) -> tuple[datetime, str] | None:
    """`liveness_at` と `last_active` のうち新しい方を返す（#2819）.

    どちらか一方の更新が漏れても他方が生きていれば作業中と判定できるようにする。
    """
    candidates: list[tuple[datetime, str]] = []
    for raw in (payload.get("liveness_at"), payload.get("last_active")):
        parsed = _parse_liveness_timestamp(raw)
        if parsed is not None:
            candidates.append((parsed, str(raw)))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])


def _evaluate_liveness(payload: dict[str, Any], ttl: int) -> tuple[bool, str] | None:
    """1 件の state payload に対して liveness 判定を行う.

    Returns:
        None: liveness_at フィールドなし・不正値（非作業中と判定・呼び出し元は無視してよい）
        (True, message): TTL 内（作業中）
        (False, message): TTL 超過（stale）
    """
    latest = _latest_liveness(payload)
    if latest is None:
        return None
    liveness_at, liveness_at_raw = latest

    now = datetime.now(UTC)
    current_issue = payload.get("current_issue")
    try:
        age_seconds = (now - liveness_at).total_seconds()
    except TypeError:
        # フェイルセーフ: 比較不能な場合は stale 扱い（#2389）
        return (False, f"stale: liveness_at={liveness_at_raw} は解釈不能なため stale 扱いにします")
    deadline = now - timedelta(seconds=ttl)

    if liveness_at < deadline:
        return (
            False,
            f"stale: liveness_at={liveness_at_raw} は TTL({ttl}s) を超過しています "
            f"(age={int(age_seconds)}s, current_issue={current_issue})",
        )

    queue = payload.get("queue", [])
    return (
        True,
        f"active: liveness_at={liveness_at_raw} TTL({ttl}s) 内 "
        f"(age={int(age_seconds)}s, current_issue={current_issue}, queue={queue})",
    )


def _cmd_check_liveness(state_file: Path, self_session: str | None = None) -> int:
    """TTL 付きで「作業中か」を判定する（Issue 番号が明示された単一ファイル対象）.

    `self_session` 未指定時（従来動作）:
        exit 1: TTL 内の liveness が存在する（作業中セッションあり）
        exit 0: TTL 超過・state 不在・JSON 破損のいずれか（フェイルセーフ: 作業中でない）

    `self_session` 指定時（#4221: 汎用 worker が親セッション自身の state を別セッションの
    着手状態と誤認しないための所有者識別）:
        state が active（TTL 内）でない場合は所有者を問わず exit 0（従来どおり非作業中）。
        active な場合のみ state の `session_id`（`stamp-issue-next-session.py` が記録）と
        `self_session` を照合する:
        - `session_id` が欠落・非文字列・空文字列（不正な値）→ exit 2
          （所有者情報が不整合で自セッション所有と断定できないため、フェイルセーフとして
          「継続しない」側に倒す。#3779 の `_is_own_session()` とは異なり、本関数の
          呼び出し元は「自セッション所有と確認できたときのみ継続してよい」worker のため、
          判定不能を「所有」扱いにする後方互換フォールバックは採用しない）
        - `self_session` と一致 → exit 0（自セッション所有・継続してよい）
        - 不一致 → exit 1（別セッション所有・停止する）

    TTL は環境変数 `ISSUE_NEXT_LIVENESS_TTL_SECONDS`（デフォルト 1800 秒）で制御する。
    非数値・負値は DEFAULT_LIVENESS_TTL_SECONDS にフォールバックする（消し忘れフェイルセーフ）。
    """
    ttl = _resolve_liveness_ttl()

    # state 不在・破損 → フェイルセーフで exit 0
    payload = _load_state(state_file)
    if payload is None:
        return 0

    result = _evaluate_liveness(payload, ttl)
    if result is None:
        return 0
    active, message = result
    print(message)
    if not active:
        return 0

    if self_session is None:
        return 1

    recorded = payload.get("session_id")
    if not isinstance(recorded, str) or not recorded:
        print(
            "所有者情報が不整合です: state ファイルに有効な session_id がありません",
            file=sys.stderr,
        )
        return 2
    if recorded == self_session:
        return 0
    print(
        f"別セッションが作業中です: state の session_id={recorded!r} は自セッション ({self_session!r}) と一致しません",
        file=sys.stderr,
    )
    return 1


def _cmd_check_liveness_scan(state_dir: Path) -> int:
    """同時実行セッション数の上限判定（session-count gate・#3457）.

    `init --enforce-session-limit`（`/issue-next` 引数なしモード・#3626）が `init` 実行前に
    呼び出す。候補ファイル（per-issue ディレクトリ配下 + 未移行の旧単一ファイル）のうち TTL 内の
    liveness を持つ件数（同時実行セッション数）を数え、`ISSUE_NEXT_MAX_SESSIONS`
    （デフォルト 2）**以上**なら exit 1（上限到達・着手不可）、未満なら exit 0（着手可）。
    state ファイルは Issue 単位に分離済み（#2474）で `🔧 in-progress` ラベル（#2804）・
    分散ロック（#3452）により同一 Issue への多重着手は別途排他されているため、本関数は
    「マシン全体で同時に走ってよいセッション数」の上限判定に専念する（#3457）。
    """
    ttl = _resolve_liveness_ttl()
    max_sessions = _resolve_max_sessions()
    candidates = _candidate_files(state_dir)
    if not candidates:
        return 0

    active_count = 0
    last_message: str | None = None
    for candidate in candidates:
        payload = _load_state(candidate)
        if payload is None:
            continue
        result = _evaluate_liveness(payload, ttl)
        if result is None:
            continue
        active, message = result
        if active:
            active_count += 1
        last_message = message

    if active_count >= max_sessions:
        print(
            f"同時実行セッション数が上限（{max_sessions}）に達しています (active={active_count})",
            file=sys.stderr,
        )
        return 1

    if last_message is not None:
        print(last_message)
    return 0


# ── next-unattended（#2874） ─────────────────────────────────────────────────

#: `priority: *` ラベル名 → ソート順位（小さいほど優先）
_PRIORITY_ORDER: dict[str, int] = {
    "priority: critical": 0,
    "priority: high": 1,
    "priority: medium": 2,
    "priority: low": 3,
}
#: このいずれかのラベルが付いた Issue は選定対象から除外する
_NEXT_UNATTENDED_EXCLUDED_LABELS = frozenset({"🙋 needs-human-input", "🔧 in-progress"})


def _issue_label_names(issue: dict[str, Any]) -> set[str]:
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return set()
    return {entry["name"] for entry in labels if isinstance(entry, dict) and isinstance(entry.get("name"), str)}


def _has_open_blocker(issue: dict[str, Any]) -> bool:
    """`blockedBy.nodes[]` に `state == "OPEN"` の blocker が 1 件以上あるかを判定する（#3640）.

    GitHub ネイティブの Issue 依存関係（blocked-by）による選定除外用。`gh issue list --json blockedBy`
    は各 Issue の `blockedBy.nodes[]`（`number` / `state` / `title` を含む）を返す。

    フェイルオープン方針（`extract_issue_paths()` の「判定不能は除外しない」にそろえる）:
    `blockedBy` キーが存在しない・`blockedBy` が dict でない・`nodes` が list でない・
    各ノードが dict でない・`state` が `"OPEN"` 以外の場合はすべて False を返す（= 除外しない）。
    これによりフィールド取得不能・スキーマ変更時も誤って全 Issue をブロックしない。
    `state == "CLOSED"` の blocker のみを持つ Issue は除外しない（マージ済みブロッカーが
    永久に依存先をブロックしないようにするため・不採用案 3）。
    """
    blocked_by = issue.get("blockedBy")
    if not isinstance(blocked_by, dict):
        return False
    nodes = blocked_by.get("nodes")
    if not isinstance(nodes, list):
        return False
    return any(isinstance(node, dict) and node.get("state") == "OPEN" for node in nodes)


def _has_open_sub_issue(issue: dict[str, Any]) -> bool:
    """`subIssuesSummary` に open なサブ Issue（`total - completed > 0`）があるかを判定する（#3997）.

    GitHub ネイティブの Issue 親子関係（sub-issues）による選定除外用。`gh issue list --json
    subIssuesSummary` は各 Issue の `{"total": int, "completed": int, "percentCompleted": int}`
    を返す。サブ Issue へ分割済みの親 Issue（Epic）は `## やること` が「サブ Issue へのリンクだけ」で
    実装対象のコードが存在しないため、open なサブ Issue が 1 件以上残っている間は選定候補から除外する。

    フェイルオープン方針（`_has_open_blocker()` と同じ「判定不能は除外しない」にそろえる）:
    `subIssuesSummary` キーが存在しない・dict でない・`total`/`completed` が int でない場合は
    すべて False を返す（= 除外しない）。これによりフィールド取得不能・スキーマ変更時も
    誤って全 Issue をブロックしない。サブ Issue が全て close された（`total == completed`）
    Issue も除外しない（選定対象に戻る）。
    """
    summary = issue.get("subIssuesSummary")
    if not isinstance(summary, dict):
        return False
    total = summary.get("total")
    completed = summary.get("completed")
    if not isinstance(total, int) or not isinstance(completed, int):
        return False
    return total - completed > 0


#: 本文中のテキストのみの blocked-by 記法（`**blocked-by:** #123 / #456`）を抽出する正規表現（#4206・#4224）
_TEXT_BLOCKED_BY_RE = re.compile(r"\*\*blocked-by:\*\*\s*((?:#\d+[^\n]*)?)", re.IGNORECASE)
_TEXT_BLOCKED_BY_NUMBER_RE = re.compile(r"#(\d+)")


def extract_text_blocked_by_numbers(body: str | None) -> frozenset[int]:
    """Issue 本文中のテキストのみの `**blocked-by:** #N` 記法（`/` 区切りで複数列挙可）を抽出する（#4206）.

    GitHub Issue Dependencies REST API が 404 を返す環境（consumer 実測: tan3159/mn-scripts）向けの
    フォールバック記法。`**blocked-by:** #123 / #456` のように `/` 区切りで複数の Issue 番号を
    列挙でき、`**blocked-by:** #B` を別行で複数回並べた場合も全行を抽出する（#4224・`finditer` 化）。
    マッチしない・Issue 番号を1件も抽出できない場合は空集合を返す（フェイルオープン。
    `select_next_unattended_issue()` 側で「判定不能は除外しない」扱いになる）。
    """
    if not isinstance(body, str) or not body:
        return frozenset()
    numbers: set[int] = set()
    for match in _TEXT_BLOCKED_BY_RE.finditer(body):
        # 各マッチは「記法直後の1行分」（`\n` は上の正規表現で除外済み）。`finditer` 化により
        # 別行で並んだ 2 件目以降の blocked-by も取りこぼさない（#4224）。
        numbers.update(int(n) for n in _TEXT_BLOCKED_BY_NUMBER_RE.findall(match.group(1)))
    return frozenset(numbers)


def _fetch_text_blocked_by_issue(number: int) -> dict[str, Any] | None:
    """`issues_by_number` に無い blocker 番号を個別に取得する（PR #4210 レビュー指摘）.

    `_fetch_open_issues()` は `gh issue list --limit 100` の取得件数を超えた Issue を含まない
    ため、一覧に無い番号は「Close 済み」と「単に取得件数外にいる Open」を区別できなかった。
    `gh_client.issue_view()` で個別に state・labels を取得することで両者を区別する。
    gh コマンド失敗（`GhCommandError`）・タイムアウト（`SubprocessTimeoutError`）時は判定不能
    としてフェイルオープンで `None` を返す（呼び出し側は `None` を「解決済み扱い・除外しない」
    として扱う。PR #4210 レビュー指摘: `SubprocessTimeoutError` を捕捉しないと `run_cli` まで
    伝播し `next-unattended` 全体が異常終了する）。
    """
    try:
        return gh_client.issue_view(number, fields=("number", "state", "labels"))
    except (GhCommandError, SubprocessTimeoutError):
        return None


def _has_unresolved_text_blocked_by(issue: dict[str, Any], issues_by_number: dict[int, dict[str, Any]]) -> bool:
    """テキストのみの blocked-by（#4206）が未解決かを判定する.

    `extract_text_blocked_by_numbers()` で抽出した各番号について、まず `issues_by_number`
    （今回 fetch した Open Issue 一覧の番号索引）を参照する。一覧に無い番号は
    `_fetch_text_blocked_by_issue()` で個別に state を取得し、`--limit 100` の取得件数外に
    いる Open な blocker を見逃さないようにする（PR #4210 レビュー指摘: 一覧に無い番号を
    無条件に「解決済み」扱いすると、Issue #4206 Scenario 1 の Then に違反する）。個別取得も
    失敗した場合（gh コマンド失敗等）は判定不能として解決済み扱いでフェイルオープンする。
    存在する場合でも、その blocker が `🙋 needs-human-input`（park 済み）ラベルを持つ場合は
    「解決済み・park 済みの依存は除外しない」（Issue #4206 やること）方針により未解決として
    扱わない。
    """
    numbers = extract_text_blocked_by_numbers(issue.get("body"))
    if not numbers:
        return False
    for number in numbers:
        blocker = issues_by_number.get(number)
        if blocker is None:
            blocker = _fetch_text_blocked_by_issue(number)
        if blocker is None:
            continue
        if blocker.get("state") == "CLOSED":
            continue
        if "🙋 needs-human-input" in _issue_label_names(blocker):
            continue
        return True
    return False


def _priority_rank(label_names: set[str]) -> int:
    """priority ラベルからソート順位を返す。ラベルなしは最低優先度（最後）扱い."""
    for name, rank in _PRIORITY_ORDER.items():
        if name in label_names:
            return rank
    return len(_PRIORITY_ORDER)


#: Issue 本文からリポジトリ相対パスを抽出する対象 prefix（#3455）
_ISSUE_PATH_PREFIXES = ("projects", r"\.claude", "docs", "templates")
_ISSUE_PATH_RE = re.compile(rf"(?:{'|'.join(_ISSUE_PATH_PREFIXES)})/[A-Za-z0-9_./-]+")


def extract_issue_paths(body: str | None) -> frozenset[str]:
    """Issue 本文からリポジトリ相対パスを正規表現で抽出する（#3455）.

    対象 prefix は `projects/**`・`.claude/**`・`docs/**`・`templates/**`。
    プロンプトインジェクション対策として本文は正規表現でのみ処理し、LLM には渡さない。
    行番号サフィックス（`path.md:50` 等）はパス文字クラスに `:` を含めないため自然に切り落とされる。
    """
    if not isinstance(body, str) or not body:
        return frozenset()
    return frozenset(match.rstrip(".") for match in _ISSUE_PATH_RE.findall(body))


def select_next_unattended_issue(
    issues: list[dict[str, Any]],
    excluded_numbers: frozenset[int] = frozenset(),
    conflict_files: frozenset[str] = frozenset(),
) -> int | None:
    """Open Issue 一覧から次に着手すべき 1 件の Issue 番号を選定する（#2874）.

    `🙋 needs-human-input`・`🔧 in-progress` ラベル付きを除外し、
    `priority: critical`→`high`→`medium`→`low`（ラベルなしは最低優先度）の順、
    同一 priority 内は Issue 番号昇順でソートして先頭を返す。候補が 0 件なら None。

    `excluded_numbers` に含まれる Issue 番号はラベルに関わらず除外する。`/issue-next` の
    skip 終端（Issue #2452）はラベルを一切付与しない意図的仕様のため、この引数がないと
    呼び出し側（`/issue-next-all`）が同一 Issue を無限に再選定し続けてしまう（#2899 レビュー指摘）。

    `conflict_files`（着手中 OPEN PR の変更ファイル集合）が指定された場合、Issue 本文から
    `extract_issue_paths()` で抽出したパスと重なる Issue は選定候補から除外する（#3455）。
    本文にリポジトリ相対パスが1つも見つからない Issue は「判定不能」として除外しない
    （フェイルオープン）。

    GitHub ネイティブの依存関係（blocked-by）で `blockedBy.nodes[]` に `state == "OPEN"` の
    blocker を 1 件以上持つ Issue は候補から除外する（#3640）。`blockedBy` キーが存在しない・
    nodes が list でない・パースできない場合は「判定不能」として除外しない（フェイルオープン）。
    `state == "CLOSED"` の blocker のみを持つ Issue は除外しない（マージ済みブロッカーが
    永久に依存先をブロックしないようにするため）。`blocking`（この Issue がブロックしている側）は
    選定に使わない。

    GitHub ネイティブの親子関係（sub-issues）で `subIssuesSummary` が open なサブ Issue
    （`total - completed > 0`）を 1 件以上示す Issue は候補から除外する（#3997）。サブ Issue へ
    分割済みの親 Issue（Epic）は `## やること` が「サブ Issue へのリンクだけ」で実装対象の
    コードが存在しないため、そのまま選定されると issue-implementer が「実装対象のない Issue」を
    渡されて時間とトークンを浪費する。`subIssuesSummary` キーが存在しない・`total`/`completed`
    が int でない場合は「判定不能」として除外しない（フェイルオープン）。全サブ Issue が
    close された（`total == completed`）Issue は除外しない（選定対象に戻る）。

    テキストのみの `**blocked-by:** #N`（`/` 区切りで複数列挙可）記法（#4206）も、ネイティブ
    `blockedBy` に加えてフォールバックで選定除外に使う。GitHub Issue Dependencies REST API が
    404 を返す環境（consumer 実測）では `blockedBy` が機能しないため、本文のテキスト記法が
    唯一の依存関係表明手段になるケースがある。抽出した各 blocker 番号について、今回 fetch した
    Open Issue 一覧に存在すれば Open と判定し、一覧に無い番号（`--limit 100` の取得件数外）は
    `gh issue view` で個別に state を確認する（PR #4210 レビュー指摘: 一覧に無い番号を無条件に
    Close 済み扱いすると、取得件数外にいる Open な blocker を見逃す）。個別取得の結果 Open かつ
    `🙋 needs-human-input`（park 済み）でない場合のみ「未解決」とみなして除外する。個別取得も
    失敗した場合（判定不能）や park 済みの blocker は除外条件に使わない（フェイルオープン）。
    """
    issues_by_number: dict[int, dict[str, Any]] = {
        issue["number"]: issue for issue in issues if isinstance(issue.get("number"), int)
    }
    candidates: list[tuple[int, int]] = []
    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            continue
        if number in excluded_numbers:
            continue
        label_names = _issue_label_names(issue)
        if label_names & _NEXT_UNATTENDED_EXCLUDED_LABELS:
            continue
        if _has_open_blocker(issue):
            continue
        if _has_open_sub_issue(issue):
            continue
        if _has_unresolved_text_blocked_by(issue, issues_by_number):
            continue
        if conflict_files and extract_issue_paths(issue.get("body")) & conflict_files:
            continue
        candidates.append((_priority_rank(label_names), number))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def _fetch_open_issues() -> list[dict[str, Any]]:
    """`gh issue list --state open --json number,labels,body,blockedBy,subIssuesSummary --limit 100` を実行する.

    `blockedBy`（#3640）は GitHub ネイティブの Issue 依存関係で、`select_next_unattended_issue()`
    が `blockedBy.nodes[]` に OPEN な blocker を持つ Issue を候補から除外するために取得する。
    `subIssuesSummary`（#3997）は GitHub ネイティブの Issue 親子関係（sub-issues）で、
    open なサブ Issue を持つ親 Issue（Epic）を候補から除外するために取得する。
    追加の API 呼び出しは発生しない（`gh issue list --json blockedBy,subIssuesSummary` が
    各 Issue のフィールドを返すことを実機確認済み）。

    gh コマンド失敗（非ゼロ終了・タイムアウト・未インストール・JSON パース失敗）時は
    フェイルセーフで空リストを返す（次に着手すべき Issue なし＝終端シグナル扱い）。
    """
    try:
        result = run_subprocess(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--json",
                "number,labels,body,blockedBy,subIssuesSummary",
                "--limit",
                "100",
            ],
            timeout=15,
        )
    except (OSError, SubprocessTimeoutError):
        return []
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _fetch_open_pr_files() -> frozenset[str]:
    """着手中 OPEN PR の変更ファイルパスの和集合を取得する（#3455）.

    `gh pr list` 失敗時（`GhCommandError`）はフェイルオープンで空集合を返す
    （競合フィルタが誤って全 Issue を除外しないようにするため）。
    """
    try:
        prs = gh_client.pr_list(state="open", limit=50, fields=("number", "headRefName", "files"))
    except GhCommandError:
        return frozenset()
    files: set[str] = set()
    for pr in prs:
        for entry in pr.get("files") or []:
            path = entry.get("path") if isinstance(entry, dict) else None
            if isinstance(path, str):
                files.add(path)
    return frozenset(files)


def _cmd_next_unattended(excluded_numbers: frozenset[int] = frozenset(), conflict_filter: bool = True) -> int:
    """`next-unattended` サブコマンド本体。対象があれば番号を、なければ何も出力しない."""
    conflict_files = _fetch_open_pr_files() if conflict_filter else frozenset()
    next_issue = select_next_unattended_issue(_fetch_open_issues(), excluded_numbers, conflict_files)
    if next_issue is not None:
        print(next_issue)
    return 0


def _cmd_migrate(state_file: Path, state_dir: Path, legacy_file: Path) -> int:
    """旧形式から新形式へ段階的に移行する（#2474）.

    Stage 1（#1054 由来・挙動は変更しない）: さらに古いキューファイル
        （`cache/issue-next-queue`）→ 旧単一 JSON ファイル（`state_file`）。
    Stage 2（#2474 新規）: 旧単一 JSON ファイル（`current_issue` が設定されている場合のみ）
        → per-issue ディレクトリ（`cache/issue-next-state/issue-<N>.json`）へ昇格する。
        `current_issue` が None（移行先キーが未確定）の場合は昇格せず単一 JSON ファイルの
        ままにする（次の `init` で自然に per-issue 形式へ切り替わる）。
    """
    _migrate_queue_file_to_flat(state_file, state_dir, legacy_file)
    return _promote_flat_file_to_per_issue(state_file, state_dir)


def _migrate_queue_file_to_flat(state_file: Path, state_dir: Path, legacy_file: Path) -> int:
    # 新ファイルがあれば旧ファイルだけ削除して終了
    if state_file.is_file():
        if legacy_file.is_file():
            legacy_file.unlink()
        return 0

    # 旧ファイルなし → 何もしない
    if not legacy_file.is_file():
        return 0

    content = legacy_file.read_text(encoding="utf-8").split()
    # 旧ファイルが空 → 削除のみ
    if not content:
        legacy_file.unlink()
        return 0

    try:
        nums = [int(x) for x in content]
    except ValueError as exc:
        print(f"Error: 旧ファイルの内容が数値ではありません: {exc}", file=sys.stderr)
        return 1

    state_dir.mkdir(parents=True, exist_ok=True)
    now = _now_iso()
    payload = {
        "current_issue": None,
        "queue": nums,
        "started_at": now,
        "last_active": now,
    }
    _atomic_write_json(state_file, payload)
    legacy_file.unlink()
    return 0


def _promote_flat_file_to_per_issue(state_file: Path, state_dir: Path) -> int:
    """旧単一 JSON ファイルを per-issue ディレクトリへ昇格する（#2474 Stage 2）.

    `current_issue` が確定していない（None）場合は移行先ファイル名が決まらないため
    何もしない（単一 JSON ファイルはそのまま残る）。
    """
    if not state_file.is_file():
        return 0
    payload = _load_state(state_file)
    if payload is None:
        return 0
    current = payload.get("current_issue")
    if not isinstance(current, int):
        return 0
    per_issue_dir = state_dir / STATE_SUBDIR
    target = _issue_file(per_issue_dir, current)
    _atomic_write_json(target, payload)
    state_file.unlink()
    return 0


# ── 補助 ────────────────────────────────────────────────────────────────────


def _state_dir() -> Path:
    """状態ファイルを置くディレクトリ.

    `ISSUE_NEXT_STATE_ROOT` 環境変数があればそれを優先する（テスト用）。
    未設定時はリポジトリルートを推定する（呼び出し元の git rev-parse を経由したくないため
    `cwd` ベースで cache/ ディレクトリを探す。本来は `git_client.rev_parse_show_toplevel`
    を使うのが理想だが、本サブコマンドは git 外でも動かしたいケースがあるので CWD ベース）。
    """
    root_override = os.environ.get("ISSUE_NEXT_STATE_ROOT")
    if root_override:
        return Path(root_override) / "cache"
    return Path.cwd() / "cache"


def _issue_file(per_issue_dir: Path, issue: int) -> Path:
    """Issue 番号に対応する per-issue 状態ファイルのパスを返す（#2474）."""
    return per_issue_dir / f"issue-{issue}.json"


def _candidate_files(state_dir: Path) -> list[Path]:
    """state 候補ファイル一覧を返す（#2474）.

    per-issue ディレクトリ配下の `issue-*.json` すべてと、未移行の旧単一 JSON ファイル
    （存在する場合）を候補として返す。順序は `issue-*.json` のソート順 → 旧ファイル。
    """
    per_issue_dir = state_dir / STATE_SUBDIR
    candidates: list[Path] = []
    if per_issue_dir.is_dir():
        candidates.extend(sorted(per_issue_dir.glob("issue-*.json")))
    legacy_flat_file = state_dir / STATE_FILENAME
    if legacy_flat_file.is_file():
        candidates.append(legacy_flat_file)
    return candidates


def _candidate_last_active(path: Path) -> str:
    """候補選定のソートキー用に last_active（無ければ空文字）を返す（#2474）."""
    payload = _load_state(path)
    if payload is None:
        return ""
    value = payload.get("last_active") or payload.get("started_at") or ""
    return value if isinstance(value, str) else ""


def _resolve_target_file(state_dir: Path, issue: int | None) -> Path:
    """コマンドの対象ファイルを解決する（#2474）.

    - `issue` が指定された場合: per-issue ファイルを対象にする。まだ per-issue 形式に
      昇格していない旧単一ファイルが同じ Issue 番号を指していればそれを対象にする
      （移行途中の後方互換）。
    - `issue` 省略時: 候補ファイルが1つだけならそれを対象にする（単一ターミナル運用時の
      後方互換）。候補が複数あれば `last_active` が最も新しいものを選ぶ（ヒューリスティック。
      並行実行時の排他は各コマンドで `issue` を明示することを前提とする）。候補が0件なら
      存在しないパスを返し、以降の `_cmd_*` が「state なし」として扱う。
    """
    per_issue_dir = state_dir / STATE_SUBDIR
    if issue is not None:
        target = _issue_file(per_issue_dir, issue)
        if target.is_file():
            return target
        legacy_flat_file = state_dir / STATE_FILENAME
        if legacy_flat_file.is_file():
            payload = _load_state(legacy_flat_file)
            if payload is not None and payload.get("current_issue") == issue:
                return legacy_flat_file
        return target

    candidates = _candidate_files(state_dir)
    if not candidates:
        return per_issue_dir / "issue-none.json"
    if len(candidates) == 1:
        return candidates[0]
    return max(candidates, key=_candidate_last_active)


def _candidate_current_issue(state_file: Path) -> int | None:
    """解決済みファイルから current_issue を読み取る（#2804 / #2817）.

    ラベル付与・除去対象の Issue 番号を特定するために使う。ファイル不在・破損・
    `current_issue` 欠損の場合は None（ラベル操作をスキップする）。
    """
    payload = _load_state(state_file)
    if payload is None:
        return None
    current = payload.get("current_issue")
    return current if isinstance(current, int) else None


def _in_progress_label_released(state_file: Path) -> bool:
    """空キュー consume でラベルを解放済みか（#2817）."""
    payload = _load_state(state_file)
    return bool(payload and payload.get("in_progress_label_released"))


def _move_in_progress_label(previous: int | None, next_issue: int | None) -> None:
    """`🔧 in-progress` ラベルを作業中の 1 件だけに保つよう移動させる（#2817）."""
    if previous is not None and previous != next_issue:
        issue_progress_label.remove_in_progress_label(previous)
    if next_issue is not None and next_issue != previous:
        issue_progress_label.add_in_progress_label(next_issue)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_state(state_file: Path) -> dict[str, Any] | None:
    if not state_file.is_file():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Error: state file の JSON が壊れています: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        return None
    return cast(dict[str, Any], data)


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    """同一ディレクトリの一時ファイルに書いてから rename する（POSIX で原子的）."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=target.name + ".",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_path, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
