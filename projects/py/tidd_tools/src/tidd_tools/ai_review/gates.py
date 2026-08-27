"""ai-review ``main()`` の gate 群（Issue #2964）.

``main()`` はバックエンドレビュー呼び出し前に SHA cache / review dedup / XXL size /
test status の 4 つの gate を直列に評価しており、各 gate は「``PR 番号`` を主入力に
``exit code`` を返すか、通過時は ``None`` を返して後続処理へ進む」という同型の
シグネチャを持つ。本モジュールは main() に直書きされていたこれら 4 ブロックを
関数として切り出し、main() を各ステージ呼び出しのオーケストレーションへ縮小する
（挙動変更なし）。

**設計方針（既存テスト互換）:** 各関数は ``handle_cache_hit`` / ``check_xxl_gate`` /
``post_review`` 等、``core.py`` 側で ``unittest.mock.patch.object(core, "...")`` に
より差し替え可能な依存をキーワード引数として受け取る。main() 側は bare name
（core モジュールの globals 経由で解決される名前）をそのまま渡すため、既存の
``patch.object(core, "handle_sha_cache_hit")`` 等のテストパッチは本モジュール分離後も
有効に機能する（gates.py は core.py を import しない・循環 import を避ける設計）。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tidd_tools.ai_review.review_dedup import ReviewCacheResult


def check_sha_cache_gate(
    pr_num: str,
    repo: str,
    app_token: str,
    head_sha: str,
    attempt: int,
    cache_usable: bool,
    stop_before_merge: bool,
    state_dir: Path,
    *,
    is_cache_hit: Callable[[Path, str], bool],
    handle_cache_hit: Callable[..., int],
) -> int | None:
    """Issue #2081: commit SHA 不変時キャッシュ gate.

    直前実行が同一 commit SHA で APPROVE 済みなら ``handle_cache_hit`` に委譲した
    exit code を返す。ヒットしなければ ``None`` を返し main() は通常フローを続行する。
    """
    if not (attempt == 1 and cache_usable and os.environ.get("AI_REVIEW_SKIP_SHA_CACHE") != "1"):
        return None
    if not is_cache_hit(state_dir, head_sha):
        return None
    return handle_cache_hit(pr_num, repo, app_token, head_sha, stop_before_merge, state_dir=state_dir)


def check_review_dedup_gate(
    pr_num: str,
    repo: str,
    app_token: str,
    head_sha: str,
    attempt: int,
    cache_usable: bool,
    stop_before_merge: bool,
    state_dir: Path,
    *,
    get_effective_review_token: Callable[[str], str],
    handle_cache_hit: Callable[..., int],
) -> int | None:
    """Issue #2419: 同一 SHA 重複レビュー投稿防止 gate.

    ``check_existing_review`` は呼び出しのたびに ``review_dedup`` モジュールから
    局所 import する（既存実装と同じ。``patch("...review_dedup.check_existing_review")``
    によるテストパッチをそのまま有効にするため）。
    """
    if not (attempt == 1 and cache_usable):
        return None

    from tidd_tools.ai_review.review_dedup import check_existing_review

    dedup_token = get_effective_review_token(app_token)
    dedup_skip = os.environ.get("AI_REVIEW_SKIP_REVIEW_CACHE") == "1"
    dedup_result: ReviewCacheResult | None = check_existing_review(
        pr_num=str(pr_num),
        repo=repo,
        head_sha=head_sha,
        token=dedup_token,
        skip_cache=dedup_skip,
    )
    if dedup_result is None:
        return None
    if dedup_result.verdict == "APPROVE":
        return handle_cache_hit(pr_num, repo, app_token, head_sha, stop_before_merge, state_dir=state_dir)
    # REQUEST_CHANGES: バックエンド再実行不要のまま exit 1
    # （--stop-before-merge は skill 側が 10 / 11 / 3 のみを解釈するため 11 に揃える）
    return 11 if stop_before_merge else dedup_result.exit_code


def check_no_new_commit_gate(
    pr_num: str,
    repo: str,
    head_sha: str,
    attempt: int,
    *,
    get_effective_review_token: Callable[[str], str],
    fetch_previous_review_sha: Callable[[str, str, str], str],
) -> int | None:
    """Issue #3636: リトライ 2 回目以降の同一 SHA 再レビュー gate.

    ``/issue-next`` のリトライループは「修正を push してから再実行」を前提にする。
    attempt >= 2 で前回レビュー以降に新コミットが無いまま再実行されると、同じ
    コードに対して同じ指摘が返り、スマートガードレールの「同一指摘 2 回連続」に
    よって修正機会を 1 回も消費せずに人間エスカレーションへ落ちる。

    前回レビュー時の head SHA（bot が GitHub に投稿済みのレビューの commit_id から
    解決）と現在の head SHA を比較し、一致していれば exit 2 で中断する。
    前回レビュー SHA を取得できない場合はフェイルオープンで通過する（既存 gate と
    同じ流儀）。

    ``AI_REVIEW_SKIP_NO_NEW_COMMIT_GATE=1`` で無効化できる（escape hatch）。
    """
    if attempt < 2:
        return None
    if os.environ.get("AI_REVIEW_SKIP_NO_NEW_COMMIT_GATE") == "1":
        return None
    if not head_sha:
        return None
    gate_token = get_effective_review_token("")
    prev_sha = fetch_previous_review_sha(pr_num, repo, gate_token)
    if not prev_sha:
        return None
    if prev_sha != head_sha:
        return None
    print(
        f"ERROR: 前回レビュー以降に新しいコミットがありません（head SHA: {head_sha}）。"
        "修正を push してから再実行してください。",
        file=sys.stderr,
    )
    return 2


def check_xxl_size_gate(
    pr_num: str,
    repo: str,
    state_dir: Path,
    app_token: str,
    *,
    check_xxl_gate: Callable[[str, str], tuple[bool, str]],
    post_review: Callable[[str, str, str, str, str], bool],
    xxl_message: str,
) -> int | None:
    """Issue #1296 / #2513: size/XXL gate.

    追加行数 >= 1000 の PR を自動で REQUEST_CHANGES にする。attempt に関わらず
    毎回チェックする（#2513: attempt == 1 限定条件を撤廃済み）。
    """
    if os.environ.get("AI_REVIEW_SKIP_SIZE_GATE") == "1":
        return None
    should_block, gate_reason = check_xxl_gate(pr_num, repo)
    print(f"==> {gate_reason}", file=sys.stderr)
    if not should_block:
        return None
    (state_dir / "verdict").write_text("REQUEST_CHANGES\n", encoding="utf-8")
    print(xxl_message, file=sys.stderr)
    # Issue #2513: bot アカウント（app token）の REQUEST_CHANGES review として投稿する。
    # plain comment（gh pr comment）だと GitHub の review 状態（CHANGES_REQUESTED）に
    # ならず merge をブロックできず、#2419 dedup（bot review 前提）にも乗らない。
    post_review(pr_num, repo, "REQUEST_CHANGES", xxl_message, app_token)
    return 1


def check_test_status_gate(
    pr_num: str,
    repo: str,
    *,
    get_effective_review_token: Callable[[str], str],
    measure_step: Callable[[str, str], AbstractContextManager[None]],
) -> int | None:
    """Issue #1982: backend review 呼び出し前の commit status gate.

    pytest/* / jest/* の commit status が FAILURE / ERROR のとき exit 5 で中断する
    （#3628: REQUEST_CHANGES の exit 1 と区別するための専用 exit code）。
    ``AI_REVIEW_SKIP_TEST_STATUS_GATE=1`` で無効化できる（bats 環境など）。
    """
    if os.environ.get("AI_REVIEW_SKIP_TEST_STATUS_GATE") == "1":
        return None

    from tidd_tools.ai_review.test_status_gate import check_test_statuses

    gate_token = get_effective_review_token("")
    with measure_step(pr_num, "commit-status-check"):
        gate_passed, gate_failed = check_test_statuses(pr_num, repo, token=gate_token)
    if gate_passed:
        return None
    failed_list = ", ".join(gate_failed)
    print(
        f"ERROR: テスト status gate: {failed_list} が FAILURE/ERROR のためレビューを中断しました。"
        "テストを修正してから再実行してください。",
        file=sys.stderr,
    )
    return 5


def check_parser_critical_gate(
    pr_num: str,
    repo: str,
    stop_before_merge: bool,
    state_dir: Path,
    *,
    get_effective_review_token: Callable[[str], str],
) -> int | None:
    """Issue #3630: backend review 呼び出し前の parser critical PR gate.

    PR の変更ファイルが parser critical（レビュー基盤または Issue バリデーション
    hook を変更）に該当する場合、``--stop-before-merge`` 無しの通常経路では
    exit 6 で中断する（secondary consensus なしの自動マージ防止）。

    parser critical と判定した場合は ``state_dir/parser-critical`` フラグファイルを
    作成する（``--stop-before-merge`` 指定時も作成。skill 側の分岐用）。

    ``AI_REVIEW_SKIP_PARSER_CRITICAL_GATE=1`` で無効化できる（素通し）。
    """
    if os.environ.get("AI_REVIEW_SKIP_PARSER_CRITICAL_GATE") == "1":
        return None

    from tidd_tools.ai_review.parser_critical import check_parser_critical_state

    gate_token = get_effective_review_token("")
    is_critical = check_parser_critical_state(pr_num, repo, gate_token)
    if not is_critical:
        return None

    # parser critical: フラグファイル作成（--stop-before-merge 指定時も作成）
    (state_dir / "parser-critical").write_text("parser-critical\n", encoding="utf-8")

    if stop_before_merge:
        return None
    from tidd_tools.ai_review.parser_critical import PARSER_CRITICAL_GATE_MESSAGE

    print(PARSER_CRITICAL_GATE_MESSAGE.replace("<PR番号>", pr_num), file=sys.stderr)
    return 6


def check_conflict_gate(
    pr_num: str,
    repo: str,
    *,
    get_effective_review_token: Callable[[str], str],
) -> int | None:
    """Issue #2989: backend review 呼び出し前のマージコンフリクト gate.

    PR のマージ可否（``mergeable``）が ``CONFLICTING``（コンフリクトあり）のとき
    exit 1 で中断する。``AI_REVIEW_SKIP_CONFLICT_GATE=1`` で無効化できる。
    """
    if os.environ.get("AI_REVIEW_SKIP_CONFLICT_GATE") == "1":
        return None

    from tidd_tools.ai_review.conflict_gate import check_conflict_state

    gate_token = get_effective_review_token("")
    gate_passed, mergeable_state = check_conflict_state(pr_num, repo, token=gate_token)
    if gate_passed:
        return None
    print(
        f"ERROR: マージコンフリクトgate: PR #{pr_num} はマージ可否が {mergeable_state} です。"
        "コンフリクトを解消してください。",
        file=sys.stderr,
    )
    return 1
