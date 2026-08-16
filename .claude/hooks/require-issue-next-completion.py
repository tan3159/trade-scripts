#!/usr/bin/env python3
"""Stop hook: `/issue-next <N>`（引数ありモード）の途中終了を機械的に防ぐ (Issue #2321・#3724)。

**背景:** `/issue-next <N>` は STEP 0（品質チェック）後に turn が終了しても機械的な
継続保証がなかった。既存の `on-stop.py` 孤児検出（Issue #1698）は `current_issue` に
対応する worktree・open PR の有無で判定するため、worktree 作成前（STEP 0 〜 STEP 1 の
間）の離脱を検出できない。本 hook は issue-next state の `current_issue` が設定されて
いる「間」を単純にブロック対象とすることでこのギャップを埋める。

**Issue #3724（コミット禁止規約誤適用による停止防止）:**

実装・テスト・pre-flight まで完了した後に、エージェントが一般規約「明示的に求められない限り
コミットしない」を理由にコミット・PR作成を停止する事象が発生した。issue-next の継続フローは
実装から PR・レビューまでを一気通貫で進める契約だが、一般規約との優先順位が機械的に明示されて
いなかった。

**Issue #3846（exit code 契約違反の是正）:**

Claude Code の Stop hook 契約では、stderr を Claude へフィードバックして実際に Stop を
ブロックするのは **exit code 2 のみ** であり、exit code 1 は「非ブロッキングのフック実行
エラー」として扱われる（`Stop hook error: Failed with non-blocking status code` という
汎用エラーバナーが表示されるだけで、意図した継続強制は機能しない）。従来 PR 未作成分岐は
exit 1 を返していたため、この契約と矛盾し「コミット → push → PR作成まで続行させる」継続
強制が有効に機能していなかった。対策として PR 未作成分岐も **exit 2** に統一する（PR 作成
済み分岐と exit code は同一になるが、stderr メッセージで未完了の遷移内容を区別する）。

**Issue #3779（別セッションの state による誤ブロック防止）:**

state ファイルの走査にセッション所有者判定が無く、複数ターミナルが並行稼働する運用では
別ターミナルが作業中の Issue の state が無関係なセッションの stop を誤ってブロックする
事故が実際に発生した。対策として state ファイルの `session_id`（PostToolUse hook
`stamp-issue-next-session.py` が記録）と Stop hook payload の `session_id` を照合し、
不一致の state はチェック対象から除外する。`session_id` 未記録の旧 state は後方互換として
従来どおり扱う。
併せて `_pr_exists()` は `gh pr list --search` の緩い全文検索結果を鵜呑みにせず、各 PR 本文が
実際に `closes #<N>` を含むかを検証してから「PR 作成済み」と判定する（GitHub の検索 API は
フレーズ一致ではないため、無関係な PR が誤ヒットしていた）。

**Issue #3868（連続ブロック時のメッセージ縮小）:**

`ScheduleWakeup` で継続を待機している間、turn が終わるたびに本 hook が発火し、同一のフル
文面警告が数十回連続して stderr に出力される事象が観測された（トークン・turn の浪費）。
継続強制自体（exit 2）は必要だが、フル文面の詳細説明（「未完了の遷移」等）を毎回繰り返す
必要はない。対策として Issue ごとに直近のブロック時刻を
`cache/issue-next-completion-block/issue-<N>.txt` に記録し、
`REQUIRE_ISSUE_NEXT_COMPLETION_REPEAT_WINDOW_SECONDS`（デフォルト 60 秒）以内の連続ブロック
では 2 回目以降のメッセージを簡潔な再掲文面に縮小する（exit code は 2 のまま維持）。
ウィンドウを超えて間隔が空けば、再びフル文面に戻る（sliding window：ブロックのたびに記録を
更新する）。

対策:
1. state ファイルは #2474 移行後の per-issue 形式
   （`cache/issue-next-state/issue-<N>.json`）を読む。旧フラット
   （`cache/issue-next-state.json`）は移行未了・後方互換のため併読する。
2. in-progress かつ TTL 内で、PR 未作成の場合（コミット・push・PR作成が未完了）は
   **exit 2**（Issue #3846・Claude Code の Stop hook 契約で Stop をブロックできるのは
   exit 2 のみのため）で stop をブロックし、Issue 番号 N と未完了の遷移を stderr に
   出力する。stderr には一般規約より継続契約（コミット・push・PR作成まで進める）が
   優先される旨も含める。
3. PR 作成済み（マージ・後処理待ち）も同じく **exit 2** でブロックする。
4. Issue に `🙋 needs-human-input` ラベルが付いている場合（park 済み・人間判断待ち）は
   ブロックしない。単なるコミット・push・PR作成未実施を人間待ちとして扱わない。

**動作:**

1. issue-next state ファイルを走査する。ファイル無し・JSON 不正・`current_issue` が
   null の場合は何もしない（exit 0）。
2. `current_issue` が設定されていても、`last_active`（無ければ `started_at`）から
   `REQUIRE_ISSUE_NEXT_COMPLETION_TTL_SECONDS`（デフォルト 7200 秒 = 2 時間）以上
   経過していればブロックしない（フェイルセーフ。state 消し忘れによる永久ブロック防止）。
3. state の `session_id`（`stamp-issue-next-session.py` が記録）が Stop hook payload の
   `session_id` と異なる場合はブロックしない（別セッション所有・#3779）。`session_id` が
   未記録（旧 state）または自セッションの `session_id` が不明な場合は後方互換・
   フェイルセーフとしてチェック対象にする（判定不能時はブロック方向に倒す）。
4. それ以外（in-progress かつ TTL 内・自セッション所有）は:
   - Issue が `🙋 needs-human-input`（park 済み）→ ブロックしない（exit 0）
   - PR 未作成（`closes #N` を本文に含む PR が実在しない）→ **exit 2**（#3846）で
     stop をブロックし、Issue 番号と未完了の遷移（コミット・push・PR作成）を
     stderr に出力する
   - PR 作成済み → **exit 2** で stop をブロックし、マージ・後処理の継続指示を
     stderr に注入する
5. ブロックする直前に、Issue ごとの直近ブロック時刻
   （`cache/issue-next-completion-block/issue-<N>.txt`）を確認する（Issue #3868）。
   `REQUIRE_ISSUE_NEXT_COMPLETION_REPEAT_WINDOW_SECONDS`（デフォルト 60 秒）以内の
   連続ブロックであれば stderr を簡潔な再掲文面に縮小する。exit code は変えない。
   ブロック確定後に記録を現在時刻へ更新する（sliding window）。

**Issue #3882（gh timeout WARN の連続抑制）:** `_issue_is_parked()`・`_pr_exists()` が
呼ぶ `run_gh()` は gh 呼び出しが timeout（デフォルト 8 秒）すると `_lib/git_helpers.py`
が毎回 `on-stop: WARN: gh timeout ...` を stderr に出力する。gh timeout が続く環境では
この WARN 行が Issue #3868 のメインブロック文面縮小の対象外のまま毎回フル出力され続け、
turn ごとに最大 16 秒（`_issue_is_parked` 8 秒 + `_pr_exists` 8 秒）を要していた。対策として、
上記 5. の直近ブロック時刻判定（ウィンドウ内かどうか）を **gh 呼び出しより前** に行い、
ウィンドウ内の連続ブロックであれば `run_gh(..., warn_on_timeout=False)` で WARN 出力自体を
抑制する（メインブロックメッセージの再掲判定と同一のウィンドウ・同一のマーカーファイルを使う
ため常に同期する）。ウィンドウを超えていれば従来どおり WARN を出力する。

**Issue #3943（session_id stamp 前クラッシュによる孤児 state の除外）:**

`tidd issue-next-state init <N>` 実行直後にセッションがクラッシュし、
`stamp-issue-next-session.py`（PostToolUse hook）が発火する前に終了すると、
`session_id` が永遠に記録されない state が残ることがある。`_is_own_session()` は
`session_id` 未記録の state を後方互換として「自セッション所有」扱いにするため（3.
参照）、この孤児 state は無関係な別セッションの Stop も無期限にブロックし続けてしまう
（TTL・デフォルト 2 時間はこの種の誤ブロックを解消するには長すぎる）。

対策として、`session_id` 未記録の state について以下の 2 条件をすべて満たす場合は
「孤児 state」としてブロック対象から除外する（exit 0）:
1. `session_id` が未記録（`stamp-issue-next-session.py` 未発火）
2. `started_at` から `REQUIRE_ISSUE_NEXT_COMPLETION_ORPHAN_GRACE_SECONDS`
   （デフォルト 120 秒）以上経過している（`stamp-issue-next-session.py` が確実に
   発火するはずの猶予時間。この時間内は「まだ発火していないだけの正常フロー」と
   区別できないため従来どおりチェック対象にする）

この判定は `_is_own_session()` によるセッション所有判定の直後（自セッション所有と
判定された state のみ）に行う。別セッション所有と判定済みの state は既に除外済みの
ため対象外。

**Issue #3951（stamp 失敗が STEP 2 以降に及ぶケースへの拡張）:**

上記 #3943 は当初、対応する worktree（branch 名に `issue-<N>-` を含む）が存在しない
ことも孤児判定の 3 つ目の条件にしていた（worktree が既に存在すれば STEP 2 まで進んで
いる正常な進行中 state のため、session_id 未記録でも従来どおりチェック対象にする、と
いう設計）。しかし実際には、`init` 直後のクラッシュ後に別プロセス・別セッションが同じ
state ファイルを使って手動で worktree 作成（STEP 2）まで進める（`init` を再実行しない
ため stamp hook が発火する機会が二度と無い）実例が観測され、session_id が永遠に記録
されないまま worktree だけが存在する state が残った。この state は worktree 存在を
理由に孤児と判定されず、無関係な別セッションの Stop を長時間ブロックし続けた
（実測ケース: 別 Issue に着手していたセッションが、PR マージ・state clear 済みにも
かかわらず無関係な Issue の state によって Stop を繰り返しブロックされた）。

対策として、孤児判定の条件から worktree の有無を外し、`session_id` 未記録・猶予時間
超過の 2 条件のみで判定するよう変更した。worktree の有無にかかわらず、無関係な別
セッションを誤ってブロックし続ける害の方が大きいと判断したため。

`config.json` で default OFF（`is_hook_enabled()` の非安全系 hook デフォルト）。
stdlib のみ使用。git/gh subprocess 実行は `_lib/git_helpers.py` に委譲する。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.git_helpers import (  # type: ignore[import-not-found]
    DEFAULT_GH_TIMEOUT_SEC,
    git_toplevel,
    run_gh,
)
from _lib.hook_io import (  # type: ignore[import-not-found]
    is_hook_enabled,
    read_stop_hook_input,
)

_DEFAULT_TTL_SECONDS = 7200  # 2 時間
_DEFAULT_REPEAT_WINDOW_SECONDS = 60  # 連続ブロックとみなす閾値（Issue #3868）
_DEFAULT_ORPHAN_GRACE_SECONDS = (
    120  # session_id stamp 前クラッシュの猶予（Issue #3943）
)
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_NEEDS_HUMAN_INPUT_LABEL = "🙋 needs-human-input"
# close(s)|fixes|fix|resolves #N（`shared.issue_body.CLOSES_RE` と同一パターン）。
# hooks/ は stdlib のみ使用の制約上 tidd_tools を import できないため regex を複製する
# （Issue #3779: gh 全文検索の緩いヒットを本文の実文字列一致で後段フィルタするため）。
_CLOSES_RE = re.compile(r"\b(?:closes?|fixes|fix|resolves)[:\s]+#(\d+)", re.IGNORECASE)


def _resolve_ttl_seconds() -> int:
    raw = os.environ.get("REQUIRE_ISSUE_NEXT_COMPLETION_TTL_SECONDS", "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TTL_SECONDS
    if value <= 0:
        return _DEFAULT_TTL_SECONDS
    return value


def _resolve_repeat_window_seconds() -> int:
    """連続ブロックとみなす秒数を返す（Issue #3868）."""
    raw = os.environ.get("REQUIRE_ISSUE_NEXT_COMPLETION_REPEAT_WINDOW_SECONDS", "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_REPEAT_WINDOW_SECONDS
    if value <= 0:
        return _DEFAULT_REPEAT_WINDOW_SECONDS
    return value


def _resolve_orphan_grace_seconds() -> int:
    """session_id 未記録の state を孤児判定するまでの猶予秒数を返す（Issue #3943）."""
    raw = os.environ.get("REQUIRE_ISSUE_NEXT_COMPLETION_ORPHAN_GRACE_SECONDS", "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_ORPHAN_GRACE_SECONDS
    if value <= 0:
        return _DEFAULT_ORPHAN_GRACE_SECONDS
    return value


def _resolve_gh_call_timeout_seconds() -> float:
    """個別 gh 呼び出しの timeout 秒数を返す（Issue #3882）.

    本番動作のデフォルトは `_lib.git_helpers.DEFAULT_GH_TIMEOUT_SEC`（8 秒）と同じ。
    テストで実際の gh timeout を高速に再現するため env override を許可する。
    """
    raw = os.environ.get("REQUIRE_ISSUE_NEXT_COMPLETION_GH_TIMEOUT_SECONDS", "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_GH_TIMEOUT_SEC
    if value <= 0:
        return DEFAULT_GH_TIMEOUT_SEC
    return value


def _block_marker_path(repo_root: str, issue_num: int) -> Path:
    """直近ブロック時刻を記録するマーカーファイルのパス（Issue #3868・Issue ごとに独立）."""
    return (
        Path(repo_root)
        / "cache"
        / "issue-next-completion-block"
        / f"issue-{issue_num}.txt"
    )


def _read_last_blocked_at(repo_root: str, issue_num: int) -> datetime | None:
    """直近ブロック時刻を返す（マーカー無し・破損時は None）."""
    marker = _block_marker_path(repo_root, issue_num)
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return datetime.strptime(raw, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _record_blocked_at(repo_root: str, issue_num: int, now: datetime) -> None:
    """直近ブロック時刻をマーカーファイルへ記録する（sliding window）.

    書き込み失敗はメッセージが毎回フル文面になるだけで継続強制（exit 2）には影響しない
    ため、例外は握りつぶす（Stop hook 全体のフェイルセーフ方針に合わせる）。
    """
    marker = _block_marker_path(repo_root, issue_num)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(now.strftime(_TIMESTAMP_FORMAT), encoding="utf-8")
    except OSError:
        pass


def _iter_state_files(repo_root: str) -> list[Path]:
    """issue-next state ファイル一覧を返す（per-issue 優先・旧フラット併読）.

    Issue #2474 で per-issue ファイル（`cache/issue-next-state/issue-<N>.json`）に移行した。
    移行未了・後方互換のため旧フラット（`cache/issue-next-state.json`）も対象に含める。
    """
    cache_dir = Path(repo_root) / "cache"
    candidates: list[Path] = []
    per_issue_dir = cache_dir / "issue-next-state"
    if per_issue_dir.is_dir():
        candidates.extend(sorted(per_issue_dir.glob("issue-*.json")))
    legacy = cache_dir / "issue-next-state.json"
    if legacy.is_file():
        candidates.append(legacy)
    return candidates


def _read_current_issue(state_path: Path) -> tuple[int | None, dict]:
    """state ファイルから current_issue と data を返す（不正・null は (None, {})）."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    current = data.get("current_issue")
    if current is None:
        return None, data
    try:
        issue_num = int(current)
    except (TypeError, ValueError):
        return None, data
    if issue_num <= 0:
        return None, data
    return issue_num, data


def _within_ttl(data: dict, ttl_seconds: int) -> bool:
    """`last_active`（無ければ `started_at`）から TTL 内なら True.

    timestamp が解釈できない場合は False（ブロックしない・安全側フォールバック）。
    """
    timestamp_raw = data.get("last_active") or data.get("started_at")
    if not isinstance(timestamp_raw, str) or not timestamp_raw:
        return False
    try:
        last_active = datetime.strptime(timestamp_raw, _TIMESTAMP_FORMAT).replace(
            tzinfo=UTC
        )
    except ValueError:
        return False
    elapsed = (datetime.now(UTC) - last_active).total_seconds()
    return elapsed <= ttl_seconds


def _issue_is_parked(issue_num: int, *, suppress_gh_timeout_warn: bool = False) -> bool:
    """Issue に `🙋 needs-human-input` ラベルがあれば True（park 済み・人間判断待ち）.

    gh 呼び出し失敗時は「park ではない」扱い（fail-closed）。本 hook の目的が
    「コミット・PR作成前の早期離脱防止」であるため、判定不能時はブロック方向（継続強制）
    に倒す。

    `suppress_gh_timeout_warn`（Issue #3882）: True の場合、gh timeout 時の
    `on-stop: WARN: gh timeout ...` 出力を抑制する（連続ブロックのウィンドウ内で
    毎回フル出力され続けるノイズを削減するため）。
    """
    rc, out = run_gh(
        "issue",
        "view",
        str(issue_num),
        "--json",
        "labels",
        timeout=_resolve_gh_call_timeout_seconds(),
        warn_on_timeout=not suppress_gh_timeout_warn,
    )
    if rc != 0:
        return False
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return False
    labels = data.get("labels")
    if not isinstance(labels, list):
        return False
    return any(
        isinstance(label, dict) and label.get("name") == _NEEDS_HUMAN_INPUT_LABEL
        for label in labels
    )


def _pr_body_closes_issue(body: object, issue_num: int) -> bool:
    """PR 本文が実際に `closes #<issue_num>` を含むかを判定する（Issue #3779）."""
    if not isinstance(body, str) or not body:
        return False
    return any(int(m.group(1)) == issue_num for m in _CLOSES_RE.finditer(body))


def _pr_exists(issue_num: int, *, suppress_gh_timeout_warn: bool = False) -> bool:
    """`closes #<N>` を実際に本文に含む PR が 1 件以上存在すれば True.

    `gh pr list --search "closes #<N> in:body"` は GitHub の全文検索を使うため
    フレーズ一致ではなく緩いトークン一致で評価される。"closes" と "#<N>" が本文中の
    別々の場所にあるだけでもヒットしてしまう（Issue #3779 の実測ケース）ため、
    取得した各 PR の本文を `_pr_body_closes_issue()` で後段フィルタし、実際に
    issue_num をクローズする語句が隣接している PR のみを「PR 作成済み」と判定する。

    gh 呼び出し失敗時は「PR なし」扱い（コミット・push・PR作成が未完了として報告する）。

    `suppress_gh_timeout_warn`（Issue #3882）: `_issue_is_parked()` と同様、
    連続ブロックのウィンドウ内では gh timeout WARN 出力を抑制する。
    """
    rc, out = run_gh(
        "pr",
        "list",
        "--state",
        "all",
        "--search",
        f"closes #{issue_num} in:body",
        "--json",
        "number,body",
        timeout=_resolve_gh_call_timeout_seconds(),
        warn_on_timeout=not suppress_gh_timeout_warn,
    )
    if rc != 0:
        return False
    try:
        parsed = json.loads(out or "[]")
    except json.JSONDecodeError:
        parsed = []
    if not isinstance(parsed, list):
        return False
    return any(
        isinstance(entry, dict) and _pr_body_closes_issue(entry.get("body"), issue_num)
        for entry in parsed
    )


def _is_own_session(data: dict, own_session_id: str) -> bool:
    """state ファイルが自セッション所有かを判定する（Issue #3779）.

    - state に `session_id`（`stamp-issue-next-session.py` が記録）が無い
      （旧 state・stamp hook 未有効）→ 後方互換で True（従来どおりチェック対象にする）
    - 自セッションの `session_id` が不明（Stop hook payload に含まれない）
      → 判定不能のためブロック方向に倒し True（`_issue_is_parked` と同じ fail-closed 方針。
      本 hook の目的が「コミット・PR作成前の早期離脱防止」であるため）
    - 両方あり一致 → True。不一致 → False（別セッション所有・チェック対象から除外）
    """
    recorded = data.get("session_id")
    if not isinstance(recorded, str) or not recorded:
        return True
    if not own_session_id:
        return True
    return recorded == own_session_id


def _elapsed_since_started_seconds(data: dict) -> float | None:
    """`started_at` からの経過秒数を返す（Issue #3943）.

    `started_at` が無い・解釈できない場合は None を返す（呼び出し側は孤児判定しない）。
    """
    started_raw = data.get("started_at")
    if not isinstance(started_raw, str) or not started_raw:
        return None
    try:
        started_at = datetime.strptime(started_raw, _TIMESTAMP_FORMAT).replace(
            tzinfo=UTC
        )
    except ValueError:
        return None
    return (datetime.now(UTC) - started_at).total_seconds()


def _is_orphan_pre_session_state(data: dict, grace_seconds: int) -> bool:
    """session_id stamp 前クラッシュ・stamp 失敗による孤児 state かを判定する（Issue #3943・#3951）.

    以下の 2 条件をすべて満たす場合に True（孤児 state・ブロック対象から除外）:
    1. `session_id` が未記録
    2. `started_at` から `grace_seconds` 以上経過している

    `session_id` 記録済み・猶予時間内のいずれかに該当すれば False（従来どおりチェック対象）。

    Issue #3951: 当初（#3943）は対応する worktree（branch 名に `issue-<N>-` を含む）が
    存在しないことも条件に含めていたが、`init` 実行後にセッションがクラッシュし、
    再実行なしで別プロセスが worktree 作成（STEP 2）まで進めるケースが実際に観測された
    ため、worktree の有無による区別を廃止した。
    """
    recorded = data.get("session_id")
    if isinstance(recorded, str) and recorded:
        return False  # session_id 記録済み → 孤児ではない
    elapsed = _elapsed_since_started_seconds(data)
    # 猶予内・timestamp 解釈不能 → 従来どおりチェック対象、猶予超過 → 孤児
    return elapsed is not None and elapsed > grace_seconds


def _check_in_progress(
    repo_root: str,
    ttl_seconds: int,
    own_session_id: str,
    repeat_window_seconds: int = _DEFAULT_REPEAT_WINDOW_SECONDS,
    orphan_grace_seconds: int = _DEFAULT_ORPHAN_GRACE_SECONDS,
) -> int:
    """in-progress かつ TTL 内・自セッション所有の state を検出して stop をブロックする.

    Returns:
        int: ブロックする場合は 2（PR 未作成・PR 作成済みいずれも同一。Issue #3846:
             Claude Code の Stop hook 契約で Stop をブロックできるのは exit 2 のみのため）、
             それ以外は 0。例外は呼び出し元 main() が捕捉して 0 にフォールバック。
    """
    for state_path in _iter_state_files(repo_root):
        issue_num, data = _read_current_issue(state_path)
        if issue_num is None:
            continue
        if not _within_ttl(data, ttl_seconds):
            continue

        # 別セッション所有の state はブロック対象から除外する（Issue #3779）
        if not _is_own_session(data, own_session_id):
            continue

        # Issue #3943/#3951: session_id stamp 前クラッシュ・stamp 失敗による孤児 state は
        # ブロック対象から除外する（無関係な別セッションの Stop を永久ブロックしないため）。
        if _is_orphan_pre_session_state(data, orphan_grace_seconds):
            continue

        # Issue #3868: 直近ブロックからウィンドウ内の連続ブロックかを判定する
        # （ScheduleWakeup 待機中の同一フル文面連続出力を抑制するため）。
        # Issue #3882: この判定を gh 呼び出し（_issue_is_parked/_pr_exists）より前に
        # 行うことで、ウィンドウ内の連続ブロックでは gh timeout WARN 出力も抑制できる
        # （メインブロックメッセージの再掲判定と同一のマーカー・ウィンドウで同期させる）。
        # マーカーはブロック確定後に更新する（sliding window）。
        now = datetime.now(UTC)
        last_blocked_at = _read_last_blocked_at(repo_root, issue_num)
        is_repeat_block = (
            last_blocked_at is not None
            and (now - last_blocked_at).total_seconds() <= repeat_window_seconds
        )

        # park 済み（人間判断待ち）→ ブロックしない（Issue #3724 Scenario 2）
        if _issue_is_parked(issue_num, suppress_gh_timeout_warn=is_repeat_block):
            continue

        _record_blocked_at(repo_root, issue_num, now)

        if _pr_exists(issue_num, suppress_gh_timeout_warn=is_repeat_block):
            # PR 作成済み → マージ・後処理待ち（従来どおり exit 2 で stderr 注入）
            if is_repeat_block:
                sys.stderr.write(
                    f"require-issue-next-completion: Issue #{issue_num} は継続中です "
                    f"(PR 作成済み・マージ/後処理待ち)。中断せず続行してください。\n"
                )
            else:
                sys.stderr.write(
                    f"require-issue-next-completion: Issue #{issue_num} はまだ未完了です "
                    f"(PR 作成済み・マージ/後処理待ち)。\n"
                    f"未完了の遷移: マージ、後処理（worktree 削除・ブランチ削除・main sync）。\n"
                    f"マージ・後処理が完了するまで turn を終了せず "
                    f"/issue-next #{issue_num} を継続してください。\n"
                )
            return 2

        # PR 未作成 → コミット・push・PR作成待ち（Issue #3724 Scenario 3）
        # Issue #3846: Claude Code の Stop hook 契約では exit 2 のみが実際に Stop を
        # ブロックする（exit 1 は非ブロッキングのフック実行エラー扱い）ため exit 2 を返す。
        if is_repeat_block:
            sys.stderr.write(
                f"require-issue-next-completion: Issue #{issue_num} は継続中です "
                f"(PR 未作成・コミット/push/PR作成待ち)。中断せず続行してください。\n"
            )
        else:
            sys.stderr.write(
                f"require-issue-next-completion: Issue #{issue_num} はまだ未完了です "
                f"(PR 未作成・コミット/push/PR作成待ち)。\n"
                f"未完了の遷移: コミット、push、PR作成。\n"
                f"issue-next 実行中は一般規約「明示的に求められない限りコミットしない」より "
                f"継続契約（コミット・push・PR作成まで進める）が優先されます。\n"
                f"セッションを終了せず、コミット → push → PR作成 まで続行してください。\n"
            )
        return 2
    return 0


def main() -> int:
    # Issue #2957: stdin 読み取りを hook_io.read_stop_hook_input へ集約
    # （従来は readline() で 1 行のみ読んで payload を捨てていた）。
    # Issue #3779: 自セッションの session_id をセッション所有者判定に使うため取得する。
    payload = read_stop_hook_input()

    if not is_hook_enabled("require-issue-next-completion"):
        return 0

    # Issue #2958: toplevel 解決は `_lib/git_helpers.py` の `git_toplevel()` に委譲する
    repo_root = git_toplevel(timeout=5)
    if not repo_root:
        return 0

    own_session_id_raw = payload.get("session_id", "")
    own_session_id = own_session_id_raw if isinstance(own_session_id_raw, str) else ""

    try:
        return _check_in_progress(
            repo_root,
            _resolve_ttl_seconds(),
            own_session_id,
            _resolve_repeat_window_seconds(),
            _resolve_orphan_grace_seconds(),
        )
    except Exception:  # noqa: BLE001 — Stop hook はフェイルセーフで exit 0
        return 0


if __name__ == "__main__":
    sys.exit(main())
