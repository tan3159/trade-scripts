"""ai-review メインフロー（旧 ai-review.sh ``main()`` を 1:1 で移植）.

終了コード:
- 0 → APPROVE 自動マージ完了
- 1 → REQUEST_CHANGES（修正後に再実行を要求）/ test-plan 失敗 / 未完了自動タスクあり等
- 2 → エスカレーション
- 3 → 全バックエンド利用不可
- 4 → APPROVE だが停止条件ファイルまたは [手動]/[AI確認] タスクあり
- 5 → テスト status gate による中断（pytest/* / jest/* の commit status が FAILURE/ERROR。Issue #3628）
- 6 → parser critical PR を --stop-before-merge 無しで呼んだ（Issue #3630）
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from tidd_tools.ai_review import consensus_cache

# Issue #2941: _classify_unchecked は approve_flow.classify_unchecked へ統合済み。
# 既存の import 元（tests/regressions/test_fix_1568.py 等）向けに後方互換 alias として re-export する。
from tidd_tools.ai_review.approve_flow import classify_unchecked as _classify_unchecked  # noqa: F401
from tidd_tools.ai_review.approve_flow import run_approve_flow
from tidd_tools.ai_review.backends import BackendResult, run_backend_review, validate_backend
from tidd_tools.ai_review.escalation import handle_escalation
from tidd_tools.ai_review.gates import (
    check_conflict_gate,
    check_no_new_commit_gate,
    check_parser_critical_gate,
    check_review_dedup_gate,
    check_sha_cache_gate,
    check_test_status_gate,
    check_xxl_size_gate,
)
from tidd_tools.ai_review.post_review import post_review
from tidd_tools.ai_review.reviewdog import (
    append_backend_to_review_body,
    run_inline_comment_review,
)
from tidd_tools.ai_review.size_gate import (
    XXL_REQUEST_CHANGES_MESSAGE,
    check_xxl_gate,
)
from tidd_tools.ai_review.state_dir import (
    StateDirMismatchError,
    remove_backend_unavailable_flag,
    resolve_state_dir,
    write_backend_unavailable_flag,
)

# Issue #2964: commit status 投稿サブシステム（_run_test_plan 以下）を
# ai_review/test_statuses.py へ分離した re-export（main() の呼び出し方は変更しない）。
from tidd_tools.ai_review.test_statuses import (
    _is_bats_env,
    _read_pytest_executed,
    _run_test_plan,
)
from tidd_tools.ai_review.timing import save_timing

# Issue #2941: measure_step / get_effective_review_token は approve_flow.py が
# core モジュール経由で local import する（mock.patch("...core.X", ...) による
# 既存テスト互換のため）。mypy strict の no_implicit_reexport 対応で明示的に re-export する。
from tidd_tools.ai_review.timing_steps import measure_step as measure_step
from tidd_tools.ai_review.tokens import (
    get_effective_review_token as get_effective_review_token,
)
from tidd_tools.ai_review.tokens import (
    get_installation_token,
    load_ai_review_secrets,
)
from tidd_tools.ai_review.verdict import (
    capture_canary_fixture,
    count_blocking_issues,
    extract_issues,
    has_issues_section,
    has_needs_human_review,
    is_blocking_issues_zero,
    is_nit_only,
    is_prev_blocking_resolved,
    is_same_issues,
    parse_verdict,
    validate_approve_authenticity,
)
from tidd_tools.loop_error_log import record as record_loop_error
from tidd_tools.shared import gh_client
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.paths import cache_dir as _cache_dir

logger = logging.getLogger(__name__)


def _vprint(*args: object) -> None:
    """``AI_REVIEW_VERBOSE=1`` のときのみ stderr へ出力する verbose ヘルパー（Issue #3429）.

    ai-review は Claude Code セッション内で同期実行されるため、中間進捗・キャッシュ
    内部状態・ループ内の件数ログはデフォルトでは LLM コンテキストに入れない。
    最終判定・exit code 理由・ユーザーアクション要求・主要フェーズ境界は ``print`` のまま
    常に出力し、本ヘルパーは分類 (3)（中間進捗等）専用にする。
    """
    if os.environ.get("AI_REVIEW_VERBOSE") == "1":
        print(*args, file=sys.stderr)


# Issue #2161: MAX_RETRIES 無条件上限廃止後、main() では使用しない。
# 外部スクリプトやテスト（_build_prev_blocking_section の max_retries 引数）への
# 後方互換として定義を残す。prompts.py の _DEFAULT_MAX_RETRIES は BACKSTOP 基準に更新済み。
DEFAULT_MAX_RETRIES = 3

# 無限ループ防止バックストップ（Issue #2161）:
# attempt >= max_retries による無条件エスカレーションを廃止し、
# is_same_issues / has_needs_human_review によるコンテンツベース判定に一本化した。
# ただし何らかの理由でコンテンツベース判定が機能しない場合に備え、
# 極めて高い試行回数で強制エスカレーションする安全弁を残す。
BACKSTOP_MAX_RETRIES = 20


def is_backstop_exceeded(attempt: int) -> tuple[bool, int]:
    """attempt が BACKSTOP_MAX_RETRIES（環境変数上書き可）を超えているか判定する（Issue #2948）.

    core.py の早期チェックと subcommands.py::consensus_verdict() で個別に実装されていた
    「環境変数 BACKSTOP_MAX_RETRIES を解決し attempt と比較する」ロジックを一本化する。

    Returns:
        (exceeded, backstop): exceeded は attempt が上限を超えているかどうか、backstop は
        環境変数 BACKSTOP_MAX_RETRIES による上書き後の実効上限値。
    """
    backstop = int(os.environ.get("BACKSTOP_MAX_RETRIES", str(BACKSTOP_MAX_RETRIES)))
    return attempt > backstop, backstop


def _resolve_repo_root() -> Path:
    """worktree でも main repo でも対象リポジトリのルートを返す.

    Issue #2224: `__file__` 起点は uv tool install（site-packages 配下）で
    クラッシュするため、CWD 優先の `shared.paths.resolve_repo_root` に統一する。
    """
    from tidd_tools.shared.paths import resolve_repo_root

    return resolve_repo_root()


def _resolve_repo(env_repo: str | None) -> str:
    """REPO 環境変数 → 未設定なら `gh repo view` で解決する（gh_client.resolve_repo に委譲・#2960）."""
    return gh_client.resolve_repo(env_repo)


def _resolve_backend(repo_root: Path) -> str:
    """環境変数 → ``~/.claude/ai-reviewer`` ファイル → ``auto`` の順で解決する."""
    env_value = os.environ.get("AI_REVIEW_BACKEND")
    if env_value:
        return env_value
    reviewer_file = Path(os.environ.get("AI_REVIEWER_FILE") or (Path.home() / ".claude" / "ai-reviewer"))
    if reviewer_file.is_file():
        try:
            content = reviewer_file.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        return content or "auto"
    return "auto"


def _rescue_transfer_manual_items(
    pr_num: str,
    repo: str,
    pr_body: str,
    app_token: str,
) -> str:
    """Issue #2026 第 2 段: merge gate で Issue やること の未 tick 項目を PR Test plan に転記する.

    closes #N から Issue body を取得し、## やること の [手動]/[AI確認] 未 tick 項目を
    [AI確認-post-merge] として PR Test plan に追記する。Issue 側の項目を tick し
    転記済みコメントを投稿する。失敗してもマージを止めない（best-effort）。

    Returns:
        転記後の PR body 文字列（転記なしの場合は元の pr_body をそのまま返す）。
        呼び出し元は返り値を pr_body_text に反映して emit_post_merge_schedule 等に渡す。
    """
    from tidd_tools.shared.issue_body import extract_closes_issues
    from tidd_tools.transfer_issue_test_items import (
        TransferItem,
        append_to_test_plan,
        build_transfer_comment,
        extract_transfer_targets,
        tick_issue_items,
    )

    closes = extract_closes_issues(pr_body)
    if not closes:
        return pr_body

    token = app_token or None

    # Issue ごとに転記対象を取得
    issue_item_pairs: list[tuple[int, str, list[TransferItem]]] = []
    for issue_num in closes:
        try:
            issue_data = gh_client.issue_view(issue_num, repo=repo, fields=("body",), token=token)
        except GhCommandError:
            continue
        issue_body = issue_data.get("body", "") or ""
        items = extract_transfer_targets(issue_body)
        if items:
            issue_item_pairs.append((issue_num, issue_body, items))

    if not issue_item_pairs:
        return pr_body

    all_items = [item for _, _, items in issue_item_pairs for item in items]

    # PR Test plan に追記
    updated_pr_body = pr_body
    try:
        updated_pr_body = append_to_test_plan(pr_body, all_items)
        gh_client.pr_edit_body(pr_num, repo, updated_pr_body, token=token)
        print(
            f"==> PR #{pr_num}: {len(all_items)} 件を元の prefix のまま PR Test plan に転記しました",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: PR Test plan への転記に失敗しました: {exc}", file=sys.stderr)
        return pr_body

    # Issue 側を tick してコメント投稿（Issue ごとの個別ログは集計 1 行に集約・#3429）
    for issue_num, issue_body, items in issue_item_pairs:
        try:
            updated_issue_body = tick_issue_items(issue_body, items)
            gh_client.issue_edit_body(issue_num, repo, updated_issue_body, token=token)
            comment = build_transfer_comment(items, pr_num)
            gh_client.issue_comment(issue_num, repo, comment, token=token)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: Issue #{issue_num} の tick/コメント投稿に失敗しました: {exc}", file=sys.stderr)

    print(f"==> {len(all_items)} 件転記完了", file=sys.stderr)

    return updated_pr_body


def _pr_body(pr_num: str, repo: str, token: str) -> tuple[str, int | None]:
    """PR ボディを取得する（`gh_client.pr_body` 経由・Issue #2960）.

    `gh_client.pr_body` は取得失敗時に WARN ログを出して空文字列を返す fail-soft 契約
    のため、旧実装が返していた非ゼロ終了コードは呼び出し元に伝播しない
    （常に ``exit_code=None`` を返す）。
    """
    body = gh_client.pr_body(pr_num, repo, token=token or None)
    return body, None


def _record_step5_boundary(pr_num: str, repo: str, token: str, step: str) -> None:
    """backend レビュー実行の開始・終了境界（`step5-airview-start`/`step5-airview-end`）を自己記録する（Issue #3516）.

    PR body を取得して ``closes #N`` から Issue 番号を解決し、対応する統一日誌
    （issue-<N>）へ point イベントを冪等記録する。``closes #N`` が見つからない・
    PR body 取得に失敗した場合は記録をスキップして処理を継続する（例外を投げない）。
    """
    from tidd_tools import timing_log
    from tidd_tools.shared.issue_body import extract_closes_issues

    pr_body_text, _ = _pr_body(pr_num, repo, token)
    issue_nums = extract_closes_issues(pr_body_text)
    if not issue_nums:
        return
    timing_log.record_event_once_safe(f"issue-{issue_nums[0]}", step, "point", "ai-review")


def _gh_pr_merge(pr_num: str, repo: str, token: str) -> int:
    """gh pr merge を実行する（`gh_client.pr_merge` 経由・Issue #2960）."""
    try:
        gh_client.pr_merge(pr_num, repo, token=token or None)
    except GhCommandError as exc:
        return exc.returncode
    return 0


def _commit_status_failed(repo: str, sha: str, token: str) -> bool:
    """Combined Status API で failure / error があるかを判定する（`gh_client.gh_raw` 経由・Issue #2960）."""
    out = gh_client.gh_raw(
        ["api", f"repos/{repo}/commits/{sha}/status", "--jq", ".statuses[] | {context: .context, state: .state}"],
        token=token or None,
    )
    if not out.strip():
        return False
    import json as _json

    failures: list[str] = []
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = _json.loads(stripped)
        except _json.JSONDecodeError:
            # gh --jq の出力が JSON でない場合は文字列マッチにフォールバックする
            if (
                '"state":"failure"' in stripped
                or '"state": "failure"' in stripped
                or '"state":"error"' in stripped
                or '"state": "error"' in stripped
            ):
                failures.append(line)
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("state") in ("failure", "error"):
            failures.append(line)
    if failures:
        for line in failures:
            print(line, file=sys.stderr)
        return True
    return False


def _fetch_head_sha(pr_num: str, repo: str) -> str:
    """PR の HEAD commit SHA を取得する（``gh_client.pr_head_sha`` → ``git rev-parse`` フォールバック・Issue #2960）."""
    sha = gh_client.pr_head_sha(pr_num, repo=repo)
    if sha.strip():
        return sha.strip()
    git_proc = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
        errors="replace",
    )
    return git_proc.stdout.strip() if git_proc.returncode == 0 else ""


def _check_commit_status_gate(pr_num: str, repo: str, app_token: str, *, cs_sha: str = "") -> tuple[bool, bool, str]:
    """CI commit status（未送信 / failure）を確認する.

    Returns:
        ``(commit_status_not_sent, commit_status_has_failure, cs_sha)`` のタプル。
    """
    cs_token = get_effective_review_token(app_token)
    if not cs_sha:
        cs_sha = _fetch_head_sha(pr_num, repo)

    from tidd_tools.ai_review.test_status_gate import check_missing_ci_statuses as _check_missing_ci

    commit_status_has_failure = False
    commit_status_not_sent = False
    if cs_sha and cs_token:
        # Issue #4138: mermaid-lint/docs 判定に repo_root を渡し、hook 側
        # （require-merge-ci-status.py::_detect_required_contexts）と同じ必須コンテキスト
        # 集合になるようにする（渡し漏れると内部 merge gate だけ mermaid-lint/docs を
        # 検知しない非対称な挙動になる）。
        missing = _check_missing_ci(str(pr_num), repo, cs_token, repo_root=_resolve_repo_root())
        if missing:
            commit_status_not_sent = True
            print(
                "==> 必要な CI status が未送信のためマージをスキップします",
                file=sys.stderr,
            )

    if not commit_status_not_sent:
        if cs_sha and cs_token:
            # 中間進捗ログ（#3429）: 確認開始・成功結果は verbose gate 対象
            _vprint(f"==> GitHub Commit Status を確認します（SHA: {cs_sha}）...")
            from tidd_tools.ai_review.test_status_gate import check_runs_failed as _check_runs_failed

            # Issue #3725: commit status API とは別の GitHub API オブジェクトである
            # native GitHub Actions CheckRun（`ci.yml` の `python` job 等）も検査する。
            # commit status が全て success でも CheckRun が failure ならブロックする。
            failed_check_runs = _check_runs_failed(cs_sha, repo, cs_token)
            if _commit_status_failed(repo, cs_sha, cs_token) or failed_check_runs:
                commit_status_has_failure = True
                print(
                    "==> Commit Status に failure が存在するためマージをスキップします",
                    file=sys.stderr,
                )
            else:
                _vprint("==> GitHub Commit Status: ✅ 全て成功（またはテスト未実行）")
        else:
            _vprint("==> GitHub Commit Status チェックをスキップ（SHA またはトークン未取得）")

    return commit_status_not_sent, commit_status_has_failure, cs_sha


def _finalize_approve(pr_num: str, repo: str, app_token: str) -> int:
    """APPROVE 確定後の共通後処理: evidence-tick → 未完了タスク → Issue やること gate → merge.

    ``_handle_approve``（通常パス）と ``handle_sha_cache_hit``（Issue #2081: SHA 不変
    キャッシュヒットパス）の両方から呼ばれる。review_output/review_body に依存しない。

    Issue #2941: 実処理は ``approve_flow.run_approve_flow`` へ統合済み。本関数は
    通常経路向けの引数を固定した薄いラッパー（``subcommands._continue_approve`` と
    経路が分裂して片側修正漏れが起きる事故（#2036・#2074）を防ぐ）。
    """
    return run_approve_flow(
        pr_num,
        repo,
        app_token,
        run_evidence_tick=True,
        run_test_status_post=False,
        resolve_token=True,
        use_measure_step=True,
        check_ci_status_after_gate=False,
        remove_label_on_merge_success=False,
        post_merge_summary=True,
    )


def handle_sha_cache_hit(
    pr_num: str,
    repo: str,
    app_token: str,
    head_sha: str,
    stop_before_merge: bool = False,
    state_dir: Path | None = None,
) -> int:
    """SHA 不変時のキャッシュヒットパス（Issue #2081）.

    直前実行と同一 commit SHA で APPROVE 済みの場合、pytest・ruff・mypy・backend レビュー
    呼び出しをスキップし、CI commit status と Issue やること gate のみ再評価する。

    stop_before_merge=True のときは _finalize_approve（マージ実行を含む）へ到達させず
    exit 10 を返す（Issue #2645）。

    Issue #2946: ``state_dir`` は main() から明示的に渡す。未指定の場合（単体呼び出し・
    既存テスト互換）は ``resolve_state_dir(pr_num)`` で解決する
    （``os.environ["STATE_DIR"]`` の直接読みを廃止し KeyError を防ぐ）。
    """
    if state_dir is None:
        state_dir = resolve_state_dir(pr_num)
    print(
        f"==> SHA 不変のため pytest/backend レビューをスキップしました（commit: {head_sha}）",
        file=sys.stderr,
    )
    commit_status_not_sent, commit_status_has_failure, _ = _check_commit_status_gate(
        pr_num, repo, app_token, cs_sha=head_sha
    )
    if commit_status_not_sent:
        # Issue #2131: consensus.json を削除してデッドロックを解除する。
        # このまま残すと次回実行でも handle_sha_cache_hit に入り exit 4 が無限に繰り返される。
        # キャッシュ内部状態のログは verbose gate 対象（#3429）。
        _vprint("==> commit status が未送信のため consensus キャッシュを破棄します。次回実行でフル実行へ移行します。")
        consensus_cache.delete_consensus(state_dir)
        print(
            "==> AIレビューは APPROVE 済みですが CI status が未送信のため自動マージしません。",
            file=sys.stderr,
        )
        print("==> CI を実行してから再度 push してください。", file=sys.stderr)
        return 4
    if commit_status_has_failure:
        print(
            "==> AIレビューは APPROVE 済みですが Commit Status に failure があるため自動マージしません。",
            file=sys.stderr,
        )
        print("==> テストを修正して再度 push してください。", file=sys.stderr)
        return 4
    if stop_before_merge:
        print("==> VERDICT: APPROVE（キャッシュヒット）", file=sys.stderr)
        print(
            "==> --stop-before-merge が指定されているためマージせずに終了します。",
            file=sys.stderr,
        )
        print(
            f"==> secondary consensus 実行後に `tidd ai-review --continue-with-verdict APPROVE {pr_num}`"
            " を実行してください。",
            file=sys.stderr,
        )
        return 10
    return _finalize_approve(pr_num, repo, app_token)


# ── メインフロー本体 ─────────────────────────────────────────────────────────


def _load_secrets_and_resolve_repo(repo_root: Path) -> tuple[str | None, int | None]:
    """起動時 secrets ロード（失敗は WARN 降格）と REPO 解決を行う.

    ai-review 用 secrets は環境変数のみから読み込む（Bitwarden 経由は廃止・#3182/#3212）。
    リポジトリ宣言型設定（.claude/ai-review-repo-config.sh）も読み込む。
    bats / pytest 内では副作用を避けるためスキップする。

    Returns:
        ``(repo, None)`` — 成功時。``(None, exit_code)`` — REPO 解決失敗時。
    """
    if not _is_bats_env() and os.environ.get("AI_REVIEW_SKIP_SECRETS_LOAD") != "1":
        try:
            load_ai_review_secrets(repo_root=repo_root)
        except Exception as exc:  # noqa: BLE001 — 起動時 secrets ロード失敗は WARN に降格
            print(f"WARN: secrets ロードに失敗しました: {exc}", file=sys.stderr)
    try:
        return _resolve_repo(os.environ.get("REPO")), None
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return None, 1


def _resolve_state_dir_paths(pr_num: str) -> tuple[Path, Path, Path, Path]:
    """STATE_DIR を解決し関連ファイルパスを返す（Issue #2946）.

    環境変数に他 PR の STATE_DIR（pr-<M>, M != pr_num）が残っている場合は
    StateDirMismatchError が送出されるため、既定の pr-<pr_num> にフォールバックする
    （#2529 が意図した「他 PR のキャッシュを汚す唯一の経路」を main() 経由でも遮断する）。

    Returns:
        ``(state_dir, prev_issues_file, prev_blocking_file, backend_name_file)``
    """
    try:
        state_dir = resolve_state_dir(pr_num)
    except StateDirMismatchError as exc:
        print(f"WARN: {exc} 既定の state dir にフォールバックします。", file=sys.stderr)
        state_dir = _cache_dir() / "ai-reviewer" / f"pr-{pr_num}"
    state_dir.mkdir(parents=True, exist_ok=True)
    # サブプロセス向けに STATE_DIR を export する
    os.environ["STATE_DIR"] = str(state_dir)
    return (
        state_dir,
        state_dir / "prev-issues",
        state_dir / "prev-blocking-count",
        state_dir / "backend-name",
    )


def _select_backend(pr_num: str, repo_root: Path) -> tuple[str | None, int | None]:
    """バックエンドを解決し検証する.

    Returns:
        ``(backend, None)`` — 成功時。``(None, exit_code)`` — 検証失敗時。
    """
    with measure_step(pr_num, "backend-selection"):
        backend = _resolve_backend(repo_root)
        try:
            validate_backend(backend)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return None, 1
    return backend, None


def _check_backstop_gate(pr_num: str, repo: str, attempt: int, repo_root: Path) -> int | None:
    """attempt がバックストップ上限を超えていれば早期エスカレーションする（Issue #2161）.

    バックストップ（BACKSTOP_MAX_RETRIES）を超えた attempt は誤用または無限ループと
    判断し早期エスカレーションする（安全弁）。
    """
    exceeded, backstop = is_backstop_exceeded(attempt)
    if not exceeded:
        return None
    msg = (
        f"attempt（{attempt}）がバックストップ上限（{backstop}）を超えています。"
        "これは誤用または無限ループの可能性があります。"
        f"tidd ai-review <PR> <attempt> の attempt は 1 〜 {backstop} の範囲で指定してください。"
    )
    print(f"WARN: {msg}", file=sys.stderr)
    return handle_escalation(
        pr_num,
        repo,
        msg,
        attempt,
        "",
        "",
        "",
        repo_root=repo_root,
    )


def _check_early_gates(
    pr_num: str,
    repo: str,
    attempt: int,
    stop_before_merge: bool,
    state_dir: Path,
    repo_root: Path,
    get_effective_review_token_fn: Callable[[str], str],
) -> int | None:
    """早期 gate（backstop・parser critical）を順に評価し、中断時は exit code を返す.

    Issue #3630: main() の循環的複雑度（C901）抑制のため、backend レビュー前に
    実行する 2 つの早期 gate を 1 箇所に集約する。通過時は None を返し、
    main() は通常フローを続行する。

    - ``_check_backstop_gate``（Issue #2161）: attempt 過大時の安全弁
    - ``check_parser_critical_gate``（Issue #3630）: parser critical PR の
      --stop-before-merge 無し呼び出しを exit 6 で中断
    """
    backstop_exit = _check_backstop_gate(pr_num, repo, attempt, repo_root)
    if backstop_exit is not None:
        return backstop_exit
    return check_parser_critical_gate(
        pr_num,
        repo,
        stop_before_merge,
        state_dir,
        get_effective_review_token=get_effective_review_token_fn,
    )


def _check_sha_gates(
    pr_num: str,
    repo: str,
    app_token: str,
    head_sha: str,
    attempt: int,
    cache_usable: bool,
    stop_before_merge: bool,
    state_dir: Path,
    get_effective_review_token_fn: Callable[[str], str],
) -> int | None:
    """SHA / attempt に依存する 3 gate を順に評価する（Issue #3636）.

    backend レビュー呼び出し前に head SHA と attempt に基づく重複系 gate を
    直列に評価し、中断時は exit code を返す（通過時は None）。

    - ``check_sha_cache_gate``（Issue #2081）: 同一 commit SHA で APPROVE 済み
      キャッシュの再利用
    - ``check_review_dedup_gate``（Issue #2419）: 同一 SHA への重複レビュー投稿防止
      （attempt == 1 のみ）
    - ``check_no_new_commit_gate``（Issue #3636）: リトライ 2 回目以降の同一 SHA
      再レビューを exit 2 で中断（新コミット無しの再実行防止）
    """
    sha_cache_exit = check_sha_cache_gate(
        pr_num,
        repo,
        app_token,
        head_sha,
        attempt,
        cache_usable,
        stop_before_merge,
        state_dir,
        is_cache_hit=consensus_cache.is_cache_hit,
        handle_cache_hit=handle_sha_cache_hit,
    )
    if sha_cache_exit is not None:
        return sha_cache_exit
    dedup_exit = check_review_dedup_gate(
        pr_num,
        repo,
        app_token,
        head_sha,
        attempt,
        cache_usable,
        stop_before_merge,
        state_dir,
        get_effective_review_token=get_effective_review_token_fn,
        handle_cache_hit=handle_sha_cache_hit,
    )
    if dedup_exit is not None:
        return dedup_exit
    from tidd_tools.ai_review.review_dedup import last_reviewed_sha

    no_new_commit_exit = check_no_new_commit_gate(
        pr_num,
        repo,
        head_sha,
        attempt,
        get_effective_review_token=get_effective_review_token_fn,
        fetch_previous_review_sha=last_reviewed_sha,
    )
    if no_new_commit_exit is not None:
        return no_new_commit_exit
    return None


def _acquire_app_token() -> str:
    """インストールトークンを取得する（失敗時は空文字で続行・Issue #1712）."""
    try:
        return get_installation_token()
    except RuntimeError:
        return ""


def _resolve_cache_usability(pr_num: str, repo: str, stop_before_merge: bool) -> tuple[str, bool]:
    """head SHA を取得し、SHA 不変キャッシュが利用可能か判定する.

    Issue #2645: --stop-before-merge は「レビュー本文が保存済み」を契約に含むため、
    本文ファイルが残っていないキャッシュヒットはフル実行へフォールバックする。
    Issue #2659: ファイルが存在しても現在の head SHA のものでなければ stale とみなす。

    Returns:
        ``(head_sha, cache_usable)``
    """
    head_sha = _fetch_head_sha(pr_num, repo)
    cache_usable = not stop_before_merge or _stop_before_merge_body_sha_matches(pr_num, head_sha)
    return head_sha, cache_usable


#: プロジェクトテスト失敗時に stderr へ表示するキャプチャ出力の末尾行数（Issue #3428）。
_PROJECT_TESTS_FAILURE_TAIL_LINES = 50


def _maybe_run_project_tests(pr_num: str, repo: str, repo_root: Path, attempt: int) -> int | None:
    """``AI_REVIEW_RUN_PROJECT_TESTS=1`` のとき初回試行でプロジェクトテストを実行する.

    Issue #3428: 出力は既定でキャプチャし、成功時は 1 行サマリのみ、失敗時は末尾
    ``_PROJECT_TESTS_FAILURE_TAIL_LINES`` 行のみを stderr に出力する（ai-review は
    Claude Code セッション内で同期実行されるため、素通しの大量ログが LLM コンテキストを
    浪費していた）。``TIDD_TEST_OUTPUT_VERBOSE=1`` で従来どおりの素通しに戻せる
    （#3426 / #3427 と同じ escape hatch）。

    Returns:
        テスト失敗時は 1。それ以外（未実行含む）は None。
    """
    if attempt != 1 or os.environ.get("AI_REVIEW_RUN_PROJECT_TESTS") != "1":
        return None
    run_script = repo_root / "scripts" / "run-project-tests.sh"
    if not run_script.is_file():
        return None
    print("==> プロジェクトテストを実行します（GAS/Python）...", file=sys.stderr)
    verbose = os.environ.get("TIDD_TEST_OUTPUT_VERBOSE") == "1"
    started = time.monotonic()
    # bash wrapper は内部で pytest / jest を呼ぶため子プロセスより大きい値 (900s)
    proc = subprocess.run(  # noqa: S603
        ["bash", str(run_script), str(pr_num)],
        env=dict(os.environ, REPO=repo),
        check=False,
        timeout=900,
        capture_output=not verbose,
        text=not verbose,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        print(
            "ERROR: プロジェクトテストが失敗しました。テストを修正してから再実行してください。",
            file=sys.stderr,
        )
        if not verbose:
            _print_project_tests_failure_tail(proc)
        return 1
    if not verbose:
        print(f"==> プロジェクトテスト passed（{elapsed:.1f}秒）", file=sys.stderr)
    return None


def _print_project_tests_failure_tail(proc: subprocess.CompletedProcess[str]) -> None:
    """プロジェクトテスト失敗時のキャプチャ出力（stdout+stderr）の末尾を stderr に出力する（Issue #3428）."""
    combined = (proc.stdout or "") + (proc.stderr or "")
    for line in combined.splitlines()[-_PROJECT_TESTS_FAILURE_TAIL_LINES:]:
        print(line, file=sys.stderr)


def _resolve_actual_backend(backend_name_file: Path, result: BackendResult) -> str:
    """実際に使用されたバックエンド名を解決する（backend-name ファイル優先）."""
    if backend_name_file.is_file():
        try:
            return backend_name_file.read_text(encoding="utf-8").strip() or "agy"
        except OSError:
            return "agy"
    return result.backend_name or "agy"


def _handle_backend_unavailable(pr_num: str, result: BackendResult) -> int:
    """全バックエンド利用不可（exit_code == 3）時のログ出力・loop-error 記録.

    Issue #2536: failure_kind で一時的（transient）と恒久的（permanent）を区別する。
    Issue #1750 Option C の「exit 3 は loop-error に記録しない」ルールを一時的区分
    （クォータ超過）のみに限定し、恒久的区分は記録する。Issue #2541: "disabled"
    （利用者による明示的無効化）も "permanent" に含まれない。
    """
    if result.failure_kind == "diff-too-large":
        # Issue #3743: PR diff が GitHub API の20000行上限（406）を超えた場合。
        # クォータ枯渇でも環境破損でもない「PR 構造上の制約」のため、QUOTA_EXCEEDED や
        # BACKEND_BROKEN と区別して報告する。
        error_summary = result.failure_reason or "PR diff が GitHub API の20000行上限を超えています"
        reason = f"DIFF_TOO_LARGE: {error_summary}"
        print(f"==> {reason}。終了コード 3 を返します。", file=sys.stderr)
    elif result.failure_kind == "parse-failure":
        error_summary = result.failure_reason or "すべてのバックエンドが VERDICT を返しませんでした"
        reason = f"REVIEW_UNAVAILABLE: {error_summary}"
        print(f"==> {reason}。終了コード 3 を返します。", file=sys.stderr)
    elif result.failure_kind == "permanent":
        # 恒久的な破損（コマンド未検出・stub・実行失敗）
        error_summary = result.failure_reason or "すべてのバックエンドが恒久的に利用不可（原因不明）"
        reason = error_summary
        print(f"==> BACKEND_BROKEN: {error_summary}。終了コード 3 を返します。", file=sys.stderr)
        # loop-error に記録して自走ループへ通知する
        record_loop_error(
            step="ai-review:backend-broken",
            error_message=error_summary,
            pr_number=pr_num,
            source="tidd_tools.ai_review.core",
        )
    else:
        # 一時的なクォータ超過（#1750 Option C: anomaly ではないため記録しない）
        reason = "QUOTA_EXCEEDED: すべてのバックエンド（agy・codex）が利用不可"
        print(
            "==> QUOTA_EXCEEDED: すべてのバックエンド（agy・codex）が利用不可です。終了コード 3 を返します。",
            file=sys.stderr,
        )
    # exit 3 の証跡フラグを残し、フォールバックレビュー起動を hook で機械強制する（#3629）
    write_backend_unavailable_flag(resolve_state_dir(pr_num), reason)
    fallback_cmd = (
        f"  uv run --project projects/py/tidd_tools python -m tidd_tools "
        f'ai-review --post-comment {pr_num} "<レビュー本文>"'
    )
    print(
        "==> 全バックエンド利用不可（exit 3）\n"
        "==> issue-next スキルが Agent tool（サブエージェント）でフォールバックレビューを実行します。\n"
        "==> レビュー完了後、以下のコマンドでボットアカウントに投稿できます:\n"
        "\n" + fallback_cmd,
        file=sys.stderr,
    )
    return 3


def _extract_review_output(backend: str, result: BackendResult) -> tuple[str | None, int | None]:
    """バックエンド実行結果からレビュー本文を取り出す.

    Issue #4082: exit 1 を REQUEST_CHANGES の単一意味へ戻す。exit 1 を返すのは
    「レビュー本文が得られた（VERDICT が含まれる）」場合のみとし、バックエンド障害
    （非ゼロ終了かつ VERDICT なし・空出力）は exit 3 を返す。固定モードの正規化は
    ``run_backend_review`` 側で行われるため、本関数に非ゼロ終了が到達するのは
    防御的経路のみ。

    Returns:
        ``(review_output, None)`` — 成功時。``(None, 3)`` — 失敗 / 空出力時。
    """
    review_output = result.output
    if not review_output:
        print(f"ERROR: {backend} からの出力が空です。", file=sys.stderr)
        return None, 3
    if result.exit_code != 0 and not parse_verdict(review_output):
        print("ERROR: すべてのレビューバックエンドが失敗しました。", file=sys.stderr)
        return None, 3
    return review_output, None


def _parse_verdict_or_none(pr_num: str, review_output: str) -> str | None:
    """レビュー本文から VERDICT 行を抽出する（失敗時は出力をログして None を返す）."""
    with measure_step(pr_num, "verdict-extraction"):
        verdict = parse_verdict(review_output)
    logger.debug("verdict regex 抽出: '%s'", verdict)
    if verdict:
        return verdict
    print("ERROR: VERDICT 行をパースできませんでした。", file=sys.stderr)
    sys.stderr.write("--- agy 出力 ---\n")
    sys.stderr.write(review_output)
    if not review_output.endswith("\n"):
        sys.stderr.write("\n")
    return None


def _apply_smart_guardrails(verdict: str, review_output: str, attempt: int, prev_issues_file: Path) -> str:
    """スマートガードレール: blocking 指摘ゼロ / 前回 blocking 解消済みなら APPROVE に昇格する."""
    if verdict == "REQUEST_CHANGES" and is_blocking_issues_zero(review_output):
        if is_nit_only(review_output):
            print(
                "==> MEDIUM/LOW 指摘のみです（advisory コメントとして残しますが修正ループは起こしません）。"
                "APPROVE として扱います。",
                file=sys.stderr,
            )
        else:
            print(
                "==> blocking 指摘（CRITICAL/HIGH）がゼロです。APPROVE として扱います。",
                file=sys.stderr,
            )
        return "APPROVE"

    if verdict == "REQUEST_CHANGES" and attempt >= 2:
        curr_issues = extract_issues(review_output)
        if is_prev_blocking_resolved(prev_issues_file, curr_issues):
            print(
                "==> [2回目以降] 前回のblocking指摘がすべて解消されています。"
                "新規 MEDIUM/LOW 指摘のみのため APPROVE として扱います。",
                file=sys.stderr,
            )
            return "APPROVE"

    return verdict


def _handle_request_changes(
    *,
    pr_num: str,
    repo: str,
    review_output: str,
    review_body: str,
    app_token: str,
    actual_backend: str,
    llm_duration: int,
    attempt: int,
    prev_issues_file: Path,
    prev_blocking_file: Path,
    repo_root: Path,
    state_dir: Path,
    stop_before_merge: bool,
    head_sha: str,
    pytest_executed: bool | None,
    started_at: datetime | None = None,
) -> int:
    """REQUEST_CHANGES 判定後の後続処理（エスカレーション判定・状態保存・投稿・--stop-before-merge）.

    呼び出し元（``main``）は verdict が "APPROVE" ではないことを確認済みのため、
    ``parse_verdict`` の契約（"APPROVE" | "REQUEST_CHANGES"）上ここでの verdict は
    常に "REQUEST_CHANGES" になる。
    """
    curr_issues = extract_issues(review_output)
    curr_blocking_count = count_blocking_issues(review_output)

    if has_needs_human_review(review_output):
        return handle_escalation(
            pr_num,
            repo,
            "AIレビューが解決不能と判断しました（[NEEDS_HUMAN_REVIEW]）",
            attempt,
            review_output,
            review_body,
            app_token,
            repo_root=repo_root,
            started_at=started_at,
        )

    # Issue #2161: attempt >= max_retries による無条件エスカレーションを廃止した。
    # 指摘内容が毎回異なる（=本物のバグが複数ある）場合は試行を継続する。
    # エスカレーションは is_same_issues（同一指摘2回連続）と has_needs_human_review のみ。
    # 無限ループは BACKSTOP_MAX_RETRIES（冒頭の早期チェック）で防止する。
    if attempt >= 2 and is_same_issues(prev_issues_file, curr_issues):
        return handle_escalation(
            pr_num,
            repo,
            "同じ指摘が2回連続で出ました（修正が根本的に解決できていません）",
            attempt,
            review_output,
            review_body,
            app_token,
            repo_root=repo_root,
            started_at=started_at,
        )

    # 状態保存（次回スマートガードレール用）
    curr_blocking_issues = "\n".join(
        line for line in curr_issues.splitlines() if not re.match(r"^\[(LOW|DEFERRED)\]", line)
    )
    if has_issues_section(review_output):
        prev_issues_file.write_text(curr_blocking_issues + "\n", encoding="utf-8")
        prev_blocking_file.write_text(f"{curr_blocking_count}\n", encoding="utf-8")

    inline_footer = run_inline_comment_review(
        pr_num,
        repo,
        app_token,
        review_output,
        backend_name=actual_backend,
    )
    if inline_footer:
        review_body = f"{review_body}\n\n> {inline_footer}"

    # ── --stop-before-merge: レビュー本文保存してマージ前に終了（REQUEST_CHANGES パス）──
    # Issue #2645: parser critical PR の secondary consensus 実行前に使用する。
    # Issue #2659: head_sha を渡して本文ファイルを SHA スコープにする。
    if stop_before_merge:
        dest = _save_review_body_for_stop_before_merge(pr_num, review_body, head_sha=head_sha)
        print("==> VERDICT: REQUEST_CHANGES", file=sys.stderr)
        print(
            "==> --stop-before-merge が指定されているためマージせずに終了します。",
            file=sys.stderr,
        )
        print(f"==> レビュー本文: {dest}", file=sys.stderr)
        # Issue #2924: 早期 return で save_timing() 呼び出し（後段）に到達しないため、
        # ここで明示的に記録する（#2936 で review_total_duration 記録は撤去済み）。
        save_timing(
            pr_num,
            "REQUEST_CHANGES",
            actual_backend,
            state_dir=state_dir,
            pytest_executed=pytest_executed,
            started_at=started_at,
        )
        return 11  # REQUEST_CHANGES を示す --stop-before-merge 専用 exit code

    # 投稿中の進捗ログは verbose gate 対象（#3429）: 完了のみ常時出力する
    _vprint("==> GitHub にレビューを投稿中...")
    post_review(pr_num, repo, "REQUEST_CHANGES", review_body, app_token)
    print("==> レビュー投稿完了", file=sys.stderr)
    save_timing(
        pr_num,
        "REQUEST_CHANGES",
        actual_backend,
        state_dir=state_dir,
        pytest_executed=pytest_executed,
        started_at=started_at,
    )
    print("==> 指摘内容を修正して push し、再度レビューを実行してください", file=sys.stderr)
    return 1


def _render_review_banner(backend: str) -> str:
    """バックエンド実行開始の stderr バナーを返す.

    Issue #4082: 固定モード（agy / agy-sonnet / codex / custom）にはフォールバック
    チェーンが存在しないため、チェーン表記を出力しない。auto モードのみチェーン順を
    明示する（実際に実行されるフォールバックチェーンを示す）。
    """
    if backend == "auto":
        return "==> auto でレビュー中（フォールバックチェーン: agy(gemini) → agy(sonnet) → codex → 終了コード 3）..."
    return f"==> {backend} でレビュー中..."


def main(pr_num: str, attempt: int, stop_before_merge: bool = False) -> int:
    """ai-review メインフロー.

    Args:
        pr_num: PR 番号
        attempt: 試行回数（1 始まり）
        stop_before_merge: True のとき verdict 確定・レビュー本文保存まで実行してマージ前に返す。
            parser critical PR の secondary consensus 実行前に使用する（Issue #2645）。
            終了コード: APPROVE=10, REQUEST_CHANGES=11, 全バックエンド利用不可=3（通常通り）。
    """
    repo_root = _resolve_repo_root()

    repo, err = _load_secrets_and_resolve_repo(repo_root)
    if err is not None:
        return err
    assert repo is not None  # err is None のとき repo は必ず設定される

    state_dir, prev_issues_file, prev_blocking_file, backend_name_file = _resolve_state_dir_paths(pr_num)
    # 前回 exit 3 で残った stale 証跡フラグを削除する（今回が exit 3 なら後段で再作成・#3629）
    remove_backend_unavailable_flag(state_dir)

    backend, err = _select_backend(pr_num, repo_root)
    if err is not None:
        return err
    assert backend is not None  # err is None のとき backend は必ず設定される

    # ── 早期 gate（Issue #2161 / #3630）─────────────────────────────────────
    # backstop（attempt 過大）と parser critical（--stop-before-merge 無しの
    # レビュー基盤変更 PR）を順に評価し、中断時は exit code を返す。
    early_exit = _check_early_gates(
        pr_num,
        repo,
        attempt,
        stop_before_merge,
        state_dir,
        repo_root,
        get_effective_review_token,
    )
    if early_exit is not None:
        return early_exit

    # codeql[py/clear-text-logging]: repo/backend are non-secret identifiers; no token is logged.
    print(
        f"==> AI レビュー開始: PR #{pr_num} ({repo}) [試行 {attempt}] [バックエンド: {backend}]",
        file=sys.stderr,
    )

    app_token = _acquire_app_token()

    # ── Issue #2081: commit SHA 不変時キャッシュ ─────────────────────────
    # 直前実行が同一 commit SHA で APPROVE 済みなら、pytest・ruff・mypy・backend レビュー
    # 呼び出しをスキップし、CI commit status と Issue やること gate のみ再評価する。
    # 新規 push（SHA 変化）時は AI_REVIEW_SKIP_SHA_CACHE=1 を使わずとも自動的にフル実行へフォールバックする。
    head_sha, cache_usable = _resolve_cache_usability(pr_num, repo, stop_before_merge)

    # ── Issue #2081 / #2419 / #3636 / #2964: SHA 系 gate ──────────────────
    # commit SHA 不変時キャッシュ・同一 SHA 重複レビュー投稿防止・リトライ 2 回目
    # 以降の同一 SHA 再レビューを順に評価する（詳細は _check_sha_gates 参照）。
    sha_gates_exit = _check_sha_gates(
        pr_num,
        repo,
        app_token,
        head_sha,
        attempt,
        cache_usable,
        stop_before_merge,
        state_dir,
        get_effective_review_token,
    )
    if sha_gates_exit is not None:
        return sha_gates_exit

    # ── Issue #1296 / #2513 / #2964: size/XXL gate ────────────────────────
    # 追加行数 >= 1000 の PR は自動で REQUEST_CHANGES にする（監査 §4-2 対応）。
    # Issue #2513: attempt == 1 限定の条件を撤廃。分割済みか allow-xxl マーカー
    # 追加済みという想定が機械強制されておらず、attempt >= 2 で gate が素通り
    # していたため、attempt に関わらず毎回チェックする。
    xxl_exit = check_xxl_size_gate(
        pr_num,
        repo,
        state_dir,
        app_token,
        check_xxl_gate=check_xxl_gate,
        post_review=post_review,
        xxl_message=XXL_REQUEST_CHANGES_MESSAGE,
    )
    if xxl_exit is not None:
        return xxl_exit

    # ── 毎回: test-plan 自動実行（Issue #2192: attempt に関わらず常に実行）─────
    # attempt == 1 の条件を外し、修正コミット後の再実行でも新しい head SHA に
    # pytest/Jest の commit status が投稿されるようにする。
    # Issue #2081（SHA 不変時キャッシュ）との整合: SHA キャッシュは attempt == 1 のみ
    # チェックするため、attempt >= 2 では常にフル実行に入る（キャッシュと衝突しない）。
    # Issue #2773: test-plan-gate を measure_step で囲み所要時間を計測する
    with measure_step(pr_num, "test-plan-gate"):
        exit_code = _run_test_plan(pr_num, repo, repo_root, state_dir)
    if exit_code != 0:
        print(
            "ERROR: テスト計画の自動検証が失敗しました。テスト計画を確認してから再実行してください。",
            file=sys.stderr,
        )
        (state_dir / "test-plan-status").write_text("test-plan-failed\n", encoding="utf-8")
        return 1
    (state_dir / "test-plan-status").write_text("test-plan-passed\n", encoding="utf-8")

    # pytest 実行有無を読み取る（Issue #2636: _post_test_statuses が pytest-executed.txt に記録）
    pytest_executed: bool | None = _read_pytest_executed(state_dir)

    project_tests_exit = _maybe_run_project_tests(pr_num, repo, repo_root, attempt)
    if project_tests_exit is not None:
        return project_tests_exit

    # ── テスト status gate（Issue #1982 / #2964）───────────────────────────
    # backend review 呼び出し前に GitHub commit status を確認する。
    # pytest/* / jest/* の status が FAILURE / ERROR のとき exit 5 で中断する
    # （#3628: REQUEST_CHANGES の exit 1 と区別する専用 exit code）。
    # AI_REVIEW_SKIP_TEST_STATUS_GATE=1 で無効化できる（bats 環境など）。
    status_gate_exit = check_test_status_gate(
        pr_num,
        repo,
        get_effective_review_token=get_effective_review_token,
        measure_step=measure_step,
    )
    if status_gate_exit is not None:
        return status_gate_exit

    # ── マージコンフリクト gate（Issue #2989）───────────────────────────────
    # backend review 呼び出し前に PR のマージ可否（mergeable）を確認する。
    # コンフリクトがある（CONFLICTING）とき exit 1 で中断する。
    # AI_REVIEW_SKIP_CONFLICT_GATE=1 で無効化できる。
    conflict_gate_exit = check_conflict_gate(
        pr_num,
        repo,
        get_effective_review_token=get_effective_review_token,
    )
    if conflict_gate_exit is not None:
        return conflict_gate_exit

    # ── バックエンド実行（auto はフォールバックチェーン込み）───────────────
    print(_render_review_banner(backend), file=sys.stderr)
    _record_step5_boundary(pr_num, repo, app_token, "step5-airview-start")
    start = time.time()
    # Issue #3553: verdict meta.started_at 用に backend レビュー実行の開始時刻を保持する。
    review_started_at = datetime.fromtimestamp(start, UTC)
    result = run_backend_review(
        pr_num,
        repo,
        app_token,
        backend=backend,
        repo_root=repo_root,
        attempt=attempt,
        prev_issues_file=prev_issues_file,
    )
    llm_duration = int(time.time() - start)
    _record_step5_boundary(pr_num, repo, app_token, "step5-airview-end")

    actual_backend = _resolve_actual_backend(backend_name_file, result)

    if result.exit_code == 3:
        return _handle_backend_unavailable(pr_num, result)

    review_output, err_code = _extract_review_output(backend, result)
    if err_code is not None:
        return err_code
    assert review_output is not None  # err_code is None のとき review_output は必ず設定される

    verdict = _parse_verdict_or_none(pr_num, review_output)
    if verdict is None:
        return 1

    print(f"==> VERDICT: {verdict}", file=sys.stderr)

    capture_canary_fixture(pr_num, actual_backend, review_output, verdict)

    # 思考テキストを除去: VERDICT 行以降のみをレビュー本文とする
    review_body = _strip_thinking(review_output)
    review_body = append_backend_to_review_body(review_body, actual_backend)

    verdict = _apply_smart_guardrails(verdict, review_output, attempt, prev_issues_file)

    if verdict == "APPROVE":
        return _finalize_approve_verdict(
            pr_num=pr_num,
            repo=repo,
            review_output=review_output,
            review_body=review_body,
            app_token=app_token,
            actual_backend=actual_backend,
            llm_duration=llm_duration,
            head_sha=head_sha,
            pytest_executed=pytest_executed,
            stop_before_merge=stop_before_merge,
            state_dir=state_dir,
            started_at=review_started_at,
        )

    return _handle_request_changes(
        pr_num=pr_num,
        repo=repo,
        review_output=review_output,
        review_body=review_body,
        app_token=app_token,
        actual_backend=actual_backend,
        llm_duration=llm_duration,
        attempt=attempt,
        prev_issues_file=prev_issues_file,
        prev_blocking_file=prev_blocking_file,
        repo_root=repo_root,
        state_dir=state_dir,
        stop_before_merge=stop_before_merge,
        head_sha=head_sha,
        pytest_executed=pytest_executed,
        started_at=review_started_at,
    )


def _finalize_approve_verdict(
    *,
    pr_num: str,
    repo: str,
    review_output: str,
    review_body: str,
    app_token: str,
    actual_backend: str,
    llm_duration: int,
    head_sha: str,
    pytest_executed: bool | None,
    stop_before_merge: bool,
    state_dir: Path | None,
    started_at: datetime | None = None,
) -> int:
    """APPROVE verdict の出口検証と後続処理への委譲（main() の分岐削減用切り出し）.

    Issue #2530: APPROVE が実レビュー由来であることを出口で検証する。
    llm_duration が下限未満またはレビュー本文が最小サイズ未満の場合は
    恒久的な環境破損として exit 3 にする（全バックエンド利用不可と同じ扱い）。
    """
    if not validate_approve_authenticity(review_output, llm_duration):
        error_summary = (
            f"APPROVE authenticity check failed: "
            f"llm_duration={llm_duration}s, review_bytes={len(review_output.encode('utf-8'))}"
        )
        reason = f"fake verdict: {error_summary}"
        print(
            f"==> BACKEND_BROKEN（fake verdict）: {error_summary}。終了コード 3 を返します。",
            file=sys.stderr,
        )
        record_loop_error(
            step="ai-review:backend-broken",
            error_message=error_summary,
            pr_number=pr_num,
            source="tidd_tools.ai_review.core",
        )
        # exit 3 の証跡フラグを残し、フォールバックレビュー起動を hook で機械強制する（#3629）
        write_backend_unavailable_flag(state_dir or resolve_state_dir(pr_num), reason)
        return 3

    return _handle_approve(
        pr_num=pr_num,
        repo=repo,
        review_output=review_output,
        review_body=review_body,
        app_token=app_token,
        actual_backend=actual_backend,
        llm_duration=llm_duration,
        cs_sha=head_sha,
        pytest_executed=pytest_executed,
        stop_before_merge=stop_before_merge,
        state_dir=state_dir,
        started_at=started_at,
    )


def _stop_before_merge_body_path(pr_num: str) -> Path:
    """--stop-before-merge のレビュー本文保存先（Issue #2645）.

    ``$AGENT_REVIEW_DIR/agent-review-<pr_num>.md``（環境変数未設定時は ``/tmp``）。
    """
    agent_review_dir = os.environ.get("AGENT_REVIEW_DIR") or "/tmp"
    return Path(agent_review_dir) / f"agent-review-{pr_num}.md"


_REVIEW_SHA_PREFIX = "<!-- review-sha: "
_REVIEW_SHA_SUFFIX = " -->"


def _stop_before_merge_body_sha_matches(pr_num: str, head_sha: str) -> bool:
    """本文ファイルが存在し、かつ現在の head SHA のレビュー結果であるか確認する（Issue #2659）.

    ファイルが存在しない場合・先頭行のメタデータが現在の head SHA と一致しない場合は False を返す。
    False の場合、cache_usable=False となり、フル実行へフォールバックする。
    """
    path = _stop_before_merge_body_path(pr_num)
    if not path.is_file():
        return False
    try:
        first_line = path.open(encoding="utf-8").readline().rstrip("\n")
    except OSError:
        return False
    # 先頭行: <!-- review-sha: <sha> -->
    if not first_line.startswith(_REVIEW_SHA_PREFIX):
        return False
    sha_in_file = first_line[len(_REVIEW_SHA_PREFIX) :].removesuffix(_REVIEW_SHA_SUFFIX)
    return sha_in_file == head_sha


def _save_review_body_for_stop_before_merge(pr_num: str, review_body: str, head_sha: str = "") -> Path:
    """--stop-before-merge 用にレビュー本文をファイルへ保存する（Issue #2645 / #2659）.

    保存先: ``$AGENT_REVIEW_DIR/agent-review-<pr_num>.md``（環境変数未設定時は ``/tmp``）。

    Issue #2659: 先頭行に ``<!-- review-sha: <head_sha> -->`` を付与して本文が
    どの head SHA のレビュー結果かを記録する。スキル側（parser-critical-pr.md）が
    ``cat`` で読む場合、先頭のメタデータ行は VERDICT: より前にあるため解析には影響しない。

    Returns:
        保存したファイルの Path。
    """
    dest = _stop_before_merge_body_path(pr_num)
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = review_body
    if head_sha:
        content = f"{_REVIEW_SHA_PREFIX}{head_sha}{_REVIEW_SHA_SUFFIX}\n{review_body}"
    dest.write_text(content, encoding="utf-8")
    return dest


def _strip_thinking(review_output: str) -> str:
    """VERDICT 行以降のみをレビュー本文として返す（旧 sh の sed '/^VERDICT:/,$p'）."""
    lines = review_output.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("VERDICT:"):
            return "\n".join(lines[idx:])
    return review_output


def _handle_approve(
    *,
    pr_num: str,
    repo: str,
    review_output: str,
    review_body: str,
    app_token: str,
    actual_backend: str,
    llm_duration: int,
    cs_sha: str = "",
    pytest_executed: bool | None = None,
    stop_before_merge: bool = False,
    state_dir: Path | None = None,
    started_at: datetime | None = None,
) -> int:
    """APPROVE パス: インラインコメント → Commit Status 確認 → 投稿 → タスクチェック → マージ.

    stop_before_merge=True のとき、レビュー本文保存まで実行してマージ前に返す（Issue #2645）。
    終了コード 10 = APPROVE（--stop-before-merge 専用）。

    Issue #2946: ``state_dir`` は main() から明示的に渡す。未指定の場合（単体呼び出し・
    既存テスト互換）は ``resolve_state_dir(pr_num)`` で解決する
    （``os.environ["STATE_DIR"]`` の直接読みを廃止し KeyError を防ぐ）。
    """
    if state_dir is None:
        state_dir = resolve_state_dir(pr_num)
    inline_footer = run_inline_comment_review(
        pr_num,
        repo,
        app_token,
        review_output,
        backend_name=actual_backend,
    )
    if inline_footer:
        review_body = f"{review_body}\n\n> {inline_footer}"

    # ── --stop-before-merge: レビュー本文保存してマージ前に終了 ──────────────
    # Issue #2645: parser critical PR の secondary consensus 実行前に使用する。
    # _finalize_approve（マージ実行を含む）には到達させない。
    # Issue #2659: head_sha を渡して本文ファイルを SHA スコープにする。
    if stop_before_merge:
        dest = _save_review_body_for_stop_before_merge(pr_num, review_body, head_sha=cs_sha)
        print("==> VERDICT: APPROVE", file=sys.stderr)
        print(
            "==> --stop-before-merge が指定されているためマージせずに終了します。",
            file=sys.stderr,
        )
        print(f"==> レビュー本文: {dest}", file=sys.stderr)
        print(
            f"==> secondary consensus 実行後に `tidd ai-review --continue-with-verdict APPROVE {pr_num}`"
            " を実行してください。",
            file=sys.stderr,
        )
        save_timing(
            pr_num,
            "APPROVE",
            actual_backend,
            state_dir=state_dir,
            pytest_executed=pytest_executed,
            started_at=started_at,
        )
        return 10  # APPROVE を示す --stop-before-merge 専用 exit code

    # ── scope-diff チェック（Issue #2597・判定のみ・非ブロッキング）─────────────
    from tidd_tools.ai_review import scope_diff_checker as _scope_diff_checker

    try:
        _scope_diff_checker.run(str(pr_num), repo)
    except Exception as exc:  # noqa: BLE001 — 非ブロッキング: 失敗しても review を止めない
        print(f"==> WARN: scope-diff-checker failed ({exc}). 続行します。", file=sys.stderr)

    # ── Commit Status チェック（未送信 / failure・Issue #2028・#1839・#2081 で共有関数化）───
    commit_status_not_sent, commit_status_has_failure, cs_sha = _check_commit_status_gate(
        pr_num, repo, app_token, cs_sha=cs_sha
    )

    # 投稿中の進捗ログは verbose gate 対象（#3429）: 完了のみ常時出力する
    _vprint("==> GitHub にレビューを投稿中...")
    post_review(pr_num, repo, "APPROVE", review_body, app_token)
    print("==> レビュー投稿完了", file=sys.stderr)
    save_timing(
        pr_num,
        "APPROVE",
        actual_backend,
        state_dir=state_dir,
        pytest_executed=pytest_executed,
        started_at=started_at,
    )

    if commit_status_not_sent:
        print(
            "==> AIレビューは APPROVE 済みですが CI status が未送信のため自動マージしません。",
            file=sys.stderr,
        )
        print("==> CI を実行してから再度 push してください。", file=sys.stderr)
        return 4

    if commit_status_has_failure:
        print(
            "==> AIレビューは APPROVE 済みですが Commit Status に failure があるため自動マージしません。",
            file=sys.stderr,
        )
        print("==> テストを修正して再度 push してください。", file=sys.stderr)
        return 4

    # Issue #2081 / #2131: commit status POST 成功を確認してから consensus.json を書く。
    # POST 失敗前に書くと次回実行でキャッシュヒットして exit 4 無限ループに陥る（#2131 デッドロック）。
    consensus_cache.write_consensus(state_dir, cs_sha, "APPROVE")

    return _finalize_approve(pr_num, repo, app_token)
