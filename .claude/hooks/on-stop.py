#!/usr/bin/env python3
"""Stop hook: Slack 通知判定・gone ブランチ削除・brief.md 生成を行う.

旧 on-stop.sh を 1:1 で踏襲する（Phase 4 / #1057 で Python 化）。
Issue #349 (Slack 入力待ち通知)・#1033 (PR-based ブランチ削除)・#1043 (Slack 通知デフォルト無効)。
Issue #1265: ネットワーク I/O を含む重い処理を日次に throttle する。
Issue #1175: 重い処理を並列化 + AI_REVIEW_STOP_HOOK_TIMEOUT で全体を打ち切る。
Issue #1557: ON_STOP_PROFILE=1 で各 phase の所要時間を stderr に出力する。
Issue #1561: `cache/issue-next-state.json` の孤児 state（worktree なし・PR なし）を検出して警告する。
Issue #1698: 孤児 state 検出時に exit 2 + stderr で auto-resume 指示を注入する。
  - Stop hook exit 2 は stop をブロックして stderr を Claude のコンテキストに注入する仕様を利用。
  - `resume_attempts` フィールドで 3 回超の無限ループを防ぐ（fail-safe: exit 0）。
Issue #2544: 孤児判定の前に Issue が CLOSED でないかを確認し、CLOSED なら state を削除して終了する。
  - PR 検出を --state open から --state all に変更し MERGED PR も孤児でないと判定する。
  - gh 呼び出し失敗時は false positive を避けるため孤児判定しない。
Issue #2955: 各サブ機能を config.json の独立キーで個別に on/off できるようにした
  （`_read_bool_hook_config` 共通ヘルパー・詳細は下記「サブ機能の個別 gating」）。
Issue #2967: サブ機能（Slack 通知・merge-summary 転記チェック・孤児 state 検出・
  ブランチ掃除・brief 生成）のロジックを `_lib/` 配下の各モジュールへ移設した（挙動変更なし）。
  本ファイルは throttle・timeout・thread 管理などの orchestration のみを担う。
  移設先: `_lib/slack_notify.py`・`_lib/stop_merge_summary_check.py`・
  `_lib/stop_orphan_state.py`・`_lib/branch_cleanup.py`・`_lib/brief_writer.py`。
  git/gh subprocess 実行部は `_lib/git_helpers.py`（`run_git_in_repo`/`run_gh`）に統一されている。
Issue #2957: stdin 読み取り（旧 `_lib/stop_payload.py`）は `_lib/hook_io.py::read_stop_hook_input()`
  へ統合した（Stop 系 3 hook 共通の isatty 対応 + WARN-only schema 検証バリアント）。

動作:
  1. config.json の "on-stop-slack": true なら stop_reason=input_required で Slack 通知（fire-and-forget）
  2. `cache/issue-next-state.json` の `current_issue` について、対応する worktree
     （branch 名に issue-<N>- を含む）と open PR（closes #<N> を本文に含む）の両方が
     無ければ exit 2 + stderr で "Orphan issue-next state" と auto-resume 指示を注入する
     （Issue #1698。旧 #1561 の stderr WARN は UI に表示されなかったため exit 2 に強化）。
     - `resume_attempts` が 3 以上の場合は fail-safe で exit 0 に落とし無限ループを防ぐ。
     - この検出は throttle の外で毎回走る（軽量で人間介入が必要なため）。
  3. 以下の重い処理は ON_STOP_THROTTLE_SECONDS（デフォルト 86400 秒 = 1 日）の
     間隔でのみ実行する。最終実行時刻は ON_STOP_LAST_CLEANUP_FILE
     （デフォルト ~/.cache/on-stop-last-cleanup）に保存する。
     3 phase を ThreadPoolExecutor で並列実行し、AI_REVIEW_STOP_HOOK_TIMEOUT
     （デフォルト 15 秒）で全体を打ち切る。
     - git fetch --prune した後、リモート削除済み（[gone]）ローカルブランチを強制削除
     - squash merge 後の MERGED PR 紐付きブランチも削除（OPEN PR・チェックアウト中ブランチは保持）
     - cache/brief.md を生成（ブランチ・直近コミット・Open PR / Issue）

hook は常に exit 0（セッション終了をブロックしない）。stdlib のみ使用。

サブ機能の個別 gating（Issue #2955）:
  config.json の以下のキーでサブ機能ごとに on/off できる。すべて `_read_bool_hook_config`
  （`hook_io.get_hook_config` を委譲）経由で読み、設定ファイルなし・キーなし・不正値の
  場合は下記デフォルトにフォールバックする（fail-safe）。
    - "on-stop-slack"          : Slack 通知（デフォルト false・opt-in）
    - "on-stop-orphan-detect"  : 孤児 issue-next state 検出（デフォルト true）
    - "on-stop-branch-cleanup" : gone/merged ブランチ掃除（デフォルト true・throttle 対象）
    - "on-stop-brief"          : cache/brief.md 生成（デフォルト true・throttle 対象）

Slack 通知の有効化:
  config.json（``~/.config/tidd_tools/config.json``）の ``"on-stop-slack"`` キーを
  true にすると有効。デフォルトは false（opt-in）。旧 env var ``ON_STOP_SLACK_ENABLED``
  は Issue #2497 で完全に廃止した（後方互換なし）。CLI 変更: ``tidd config enable on-stop-slack --machine``。

環境変数:
  ON_STOP_THROTTLE_SECONDS     重い処理の実行間隔秒数（デフォルト 86400）。
                               0 なら throttle 無効で毎回実行する。
                               非数値ならデフォルトにフォールバックする。
  ON_STOP_LAST_CLEANUP_FILE    最終実行タイムスタンプ保存先
                               （デフォルト ~/.cache/on-stop-last-cleanup）
  AI_REVIEW_STOP_HOOK_TIMEOUT  重い処理全体の壁時計タイムアウト秒数（デフォルト 15）。
                               超過時は未完了処理を打ち切って exit 0 で終わる。
                               非数値・0 以下ならデフォルトにフォールバックする。
  ON_STOP_PROFILE              1 なら各 phase の所要時間を stderr にログ出力する（Issue #1557）。
                               "on-stop: PROFILE: <phase>=<elapsed_ms>ms" 形式で出力。
                               デフォルト 0（無効）。実測ボトルネック特定用の opt-in。
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_DEFAULT_THROTTLE_SECONDS = 86400  # 1 日
_DEFAULT_STOP_HOOK_TIMEOUT_SECONDS = 15  # Issue #1175 target
_DIGITS_RE = re.compile(r"^[0-9]+$")

# Issue #2967: サブ機能ロジックは `_lib/` 配下へ移設した。on-stop.py の既存慣習
# （関数ローカルで `_lib` を sys.path 追加してから bare import する）とは別に、
# `_run_heavy_work` 等がテストから monkeypatch される名前（`_cleanup_gone_branches` 等）は
# モジュール top-level で import して on-stop.py の module 名前空間に束縛しておく必要がある
# （monkeypatch.setattr(module, "_cleanup_gone_branches", ...) が機能するため）。
sys.path.insert(0, str(Path(__file__).resolve().parent / "_lib"))

from branch_cleanup import (  # type: ignore[import-not-found]
    cleanup_gone_branches as _cleanup_gone_branches,
)
from branch_cleanup import (
    cleanup_merged_pr_branches as _cleanup_merged_pr_branches,
)
from branch_cleanup import (
    detect_default_branch as _detect_default_branch,
)
from brief_writer import write_brief as _write_brief  # type: ignore[import-not-found]
from hook_io import (
    read_stop_hook_input,  # type: ignore[import-not-found]  # Issue #2957
)
from slack_notify import notify_slack as _notify_slack  # type: ignore[import-not-found]
from stop_merge_summary_check import (  # type: ignore[import-not-found]
    check_merge_summary_in_transcript as _check_merge_summary_in_transcript,
)
from stop_orphan_state import (  # type: ignore[import-not-found]
    detect_orphan_issue_next_state as _detect_orphan_issue_next_state,
)


def _resolve_last_cleanup_file() -> Path:
    """throttle 用のタイムスタンプ保存先を解決する."""
    env_path = os.environ.get("ON_STOP_LAST_CLEANUP_FILE", "").strip()
    if env_path:
        return Path(env_path)
    return Path.home() / ".cache" / "on-stop-last-cleanup"


def _resolve_throttle_seconds() -> int:
    """throttle 秒数を環境変数から解決する.

    非数値ならデフォルト 86400 にフォールバックする（クラッシュしない）。
    """
    raw = os.environ.get("ON_STOP_THROTTLE_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_THROTTLE_SECONDS
    if not _DIGITS_RE.match(raw):
        return _DEFAULT_THROTTLE_SECONDS
    return int(raw)


def _should_run_cleanup(last_cleanup_file: Path, throttle_seconds: int) -> bool:
    """前回 cleanup から throttle_seconds 以上経過していれば True.

    タイムスタンプファイルが存在しない or 壊れている場合は初回とみなして True。
    throttle_seconds が 0 の場合は throttle 無効として常に True。
    """
    if throttle_seconds <= 0:
        return True
    if not last_cleanup_file.is_file():
        return True
    try:
        raw = last_cleanup_file.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    if not _DIGITS_RE.match(raw):
        return True
    last_ts = int(raw)
    now = int(time.time())
    return (now - last_ts) >= throttle_seconds


def _update_last_cleanup(last_cleanup_file: Path) -> None:
    """タイムスタンプファイルを「今」で更新する。失敗しても黙って続行."""
    try:
        last_cleanup_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        last_cleanup_file.write_text(f"{int(time.time())}\n", encoding="utf-8")
    except OSError:
        pass


def _resolve_stop_hook_timeout() -> int:
    """AI_REVIEW_STOP_HOOK_TIMEOUT を int で解決する（Issue #1175）.

    非数値・0 以下・空はデフォルト (_DEFAULT_STOP_HOOK_TIMEOUT_SECONDS) にフォールバック。
    """
    raw = os.environ.get("AI_REVIEW_STOP_HOOK_TIMEOUT", "").strip()
    if not raw or not _DIGITS_RE.match(raw):
        return _DEFAULT_STOP_HOOK_TIMEOUT_SECONDS
    val = int(raw)
    if val <= 0:
        return _DEFAULT_STOP_HOOK_TIMEOUT_SECONDS
    return val


def _profile_enabled() -> bool:
    """ON_STOP_PROFILE=1 が set されているか判定する（Issue #1557）."""
    return os.environ.get("ON_STOP_PROFILE", "0").strip() == "1"


def _log_profile(phase: str, elapsed_ms: float) -> None:
    """profile 有効時のみ phase 所要時間を stderr に出力する（Issue #1557）.

    出力形式: `on-stop: PROFILE: <phase>=<elapsed_ms>ms`（1 行）
    """
    sys.stderr.write(f"on-stop: PROFILE: {phase}={elapsed_ms:.1f}ms\n")


def _run_heavy_work(repo_root: str, brief: Path) -> None:
    """重い処理を並列実行する（Issue #1175）.

    `_cleanup_gone_branches` (fetch --prune + git branch -D) と
    `_cleanup_merged_pr_branches` (git branch -D) は共に refs を書き換えるため、
    並列に走らせると `.git/packed-refs` や個別 ref ロックで衝突しうる。よって
    この 2 phase は 1 スレッドに逐次化し、read-only の `_write_brief` とだけ
    並列実行する。個別 phase の例外は握りつぶす（Stop hook は exit 0 が原則）。

    Issue #1557: ON_STOP_PROFILE=1 なら各 phase の所要時間を stderr に出力する。
    Issue #2955: `on-stop-branch-cleanup` / `on-stop-brief` キーでそれぞれ個別に
    無効化できる（default True・従来どおり常時実行）。無効化された phase は
    ThreadPoolExecutor に submit せずスキップする。
    """
    profile = _profile_enabled()
    branch_cleanup_enabled = _read_bool_hook_config(
        "on-stop-branch-cleanup", default=True
    )
    brief_enabled = _read_bool_hook_config("on-stop-brief", default=True)
    default_branch = _detect_default_branch(repo_root)

    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001, S110 — Stop hook は必ず exit 0
            pass

    def _timed(phase: str, fn, *args) -> None:
        """profile 有効時は phase の所要時間を stderr に出力する."""
        if not profile:
            _safe(fn, *args)
            return
        start = time.monotonic()
        try:
            fn(*args)
        except Exception:  # noqa: BLE001, S110 — Stop hook は必ず exit 0
            pass
        finally:
            _log_profile(phase, (time.monotonic() - start) * 1000.0)

    def _sequential_branch_cleanups() -> None:
        _timed(
            "cleanup_gone_branches", _cleanup_gone_branches, repo_root, default_branch
        )
        _timed(
            "cleanup_merged_pr_branches",
            _cleanup_merged_pr_branches,
            repo_root,
            default_branch,
        )

    def _write_brief_task() -> None:
        _timed("write_brief", _write_brief, repo_root, brief)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="on-stop") as ex:
        futures = []
        if branch_cleanup_enabled:
            futures.append(ex.submit(_sequential_branch_cleanups))
        if brief_enabled:
            futures.append(ex.submit(_write_brief_task))
        for f in futures:
            try:
                f.result()
            except Exception:  # noqa: BLE001, S110 — Stop hook は必ず exit 0
                pass


def _read_bool_hook_config(key: str, *, default: bool) -> bool:
    """hooks-config（config.json）の bool キーを読む共通ヘルパー（Issue #2955）.

    on-stop.py の各サブ機能（Slack 通知・孤児 state 検出・ブランチ掃除・brief 生成）を
    個別に on/off するために使う。``hook_io.get_hook_config`` へ委譲し、設定ファイル
    なし・キーなし・不正 JSON・解決失敗時は ``default`` にフォールバックする
    （Stop hook は常に fail-safe で継続する）。
    """
    try:
        _lib_dir = Path(__file__).resolve().parent / "_lib"
        if str(_lib_dir) not in sys.path:
            sys.path.insert(0, str(_lib_dir))
        from hook_io import get_hook_config  # type: ignore[import-not-found]

        config_value = get_hook_config(key, default=default)
    except Exception:  # noqa: BLE001 — Stop hook は fail-safe
        return default
    return bool(config_value)


def _read_slack_enabled() -> bool:
    """Slack 通知の有効判定（Issue #2497: config.json 一元管理・env var 廃止）.

    ``~/.config/tidd_tools/config.json`` の ``"on-stop-slack"`` キーを
    ``_read_bool_hook_config`` 経由で読む。未設定または解決失敗時は False
    （default OFF・opt-in）。Issue #2955: 他サブ機能と同じ共通ヘルパーへ統合。

    旧 env var ``ON_STOP_SLACK_ENABLED`` は #2497 で完全に廃止した（後方互換なし）。
    """
    return _read_bool_hook_config("on-stop-slack", default=False)


def _detect_orphan_issue_next_state_gated(repo_root: str) -> int:
    """`on-stop-orphan-detect` が有効な場合のみ孤児 state 検出を実行する（Issue #2955）.

    default True（従来どおり常時実行）。無効化されている場合は何もせず 0 を返す。
    """
    if not _read_bool_hook_config("on-stop-orphan-detect", default=True):
        return 0
    try:
        return _detect_orphan_issue_next_state(repo_root)
    except Exception:  # noqa: BLE001 — Stop hook は fail-safe で exit 0
        return 0


def main() -> int:
    # Issue #2957: stdin 読み取りを hook_io.read_stop_hook_input へ集約
    # （isatty 対応・Stop schema 不一致は WARN のみで exit しない）。
    stop_payload = read_stop_hook_input()

    # Slack 通知判定
    if _read_slack_enabled():
        stop_reason = str(stop_payload.get("stop_reason") or "")
        webhook = (
            os.environ.get("CLAUDE_WEBHOOK_URL")
            or os.environ.get("SLACK_WEBHOOK_URL")
            or ""
        )
        if stop_reason == "input_required" and webhook:
            _notify_slack(webhook)
            print(
                "on-stop: slack 通知を送信しました (stop_reason=input_required)",
                file=sys.stderr,
            )
        else:
            # 有効化されていることを毎回可視化する（Issue #1994 受け入れ基準）
            print(
                f"on-stop: slack 通知は有効です（今回は送信条件を満たさず送信せず: "
                f"stop_reason={stop_reason or '(なし)'}, webhook={'設定済み' if webhook else '未設定'}）",
                file=sys.stderr,
            )

    # Issue #2466: merge-summary 転記チェックは throttle の外で毎回走る（軽量）。
    # git rev-parse より前に実行することで、ISSUE_NEXT_STATE_ROOT 環境変数のみで動作できる。
    # marker ファイルが存在し、transcript_path から直近 assistant メッセージを読み、
    # 合計行との厳密一致を確認する。不一致なら exit 2 で再指示を注入する。
    try:
        merge_summary_exit_code = _check_merge_summary_in_transcript(stop_payload)
    except Exception:  # noqa: BLE001 — Stop hook は fail-safe で exit 0
        merge_summary_exit_code = 0
    if merge_summary_exit_code != 0:
        return merge_summary_exit_code

    # リポジトリルート（Issue #2958: `_lib/git_helpers.py` の `git_toplevel()` に委譲）
    _lib_dir = Path(__file__).resolve().parent / "_lib"
    if str(_lib_dir) not in sys.path:
        sys.path.insert(0, str(_lib_dir))
    from git_helpers import git_toplevel  # type: ignore[import-not-found]

    repo_root = git_toplevel()
    if not repo_root:
        return 0

    # Issue #1698: 孤児 issue-next state 検出は throttle の外で毎回走る。
    # 軽量（state file の JSON parse + git worktree list + gh pr list --search 1 回）で、
    # 孤児 state を検出したら exit 2 + stderr で auto-resume 指示を注入する。
    # 3 回超の場合は fail-safe で exit 0 に落とす。
    # Issue #2955: `on-stop-orphan-detect` キーで個別 gating 可能（default True）。
    orphan_exit_code = _detect_orphan_issue_next_state_gated(repo_root)
    if orphan_exit_code != 0:
        return orphan_exit_code

    # Issue #1265: 重い処理（fetch --prune・gh 経由の PR 列挙・brief.md 生成）は
    # ON_STOP_THROTTLE_SECONDS ごとにしか実行しない。
    # Stop イベントは会話ターンごとに発火するため、毎回走らせると数秒〜10 秒の遅延になる。
    last_cleanup_file = _resolve_last_cleanup_file()
    throttle_seconds = _resolve_throttle_seconds()
    if not _should_run_cleanup(last_cleanup_file, throttle_seconds):
        return 0

    brief = Path(repo_root) / "cache" / "brief.md"

    # Issue #1175: 重い処理は daemon thread に隔離して全体タイムアウトで打ち切る。
    # 打ち切り時は残った subprocess はプロセス終了で kill される（daemon thread なので
    # Python interpreter の shutdown を止めない）。個別 subprocess の timeout も 8s に
    # 揃えているため、orphan 化しても最大 8s で自然消滅する。
    total_timeout = _resolve_stop_hook_timeout()
    done = threading.Event()

    def _worker() -> None:
        try:
            _run_heavy_work(repo_root, brief)
        finally:
            done.set()

    thread = threading.Thread(target=_worker, name="on-stop-worker", daemon=True)
    thread.start()
    completed = done.wait(timeout=total_timeout)

    # 全 phase 完了時のみタイムスタンプを更新する（部分完了時は次回もリトライさせる）。
    if completed:
        _update_last_cleanup(last_cleanup_file)
    else:
        sys.stderr.write(
            f"on-stop: WARN: heavy work exceeded {total_timeout}s "
            f"(AI_REVIEW_STOP_HOOK_TIMEOUT); skipped remaining work\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
