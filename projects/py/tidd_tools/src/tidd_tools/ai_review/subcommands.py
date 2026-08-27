"""``--post-comment`` / ``--continue-with-verdict`` / ``--verify-ai-confirm`` / ``--consensus-verdict``.

旧 ai-review.sh の冒頭で処理されていた 2 つのサブコマンドを Python に移植する。
``--verify-ai-confirm`` は Issue #1246 で追加された Tool Calling 自律検証。
``--consensus-verdict`` は Issue #2657 で追加した parser critical PR の secondary consensus 判定。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

from tidd_tools.ai_review.approve_flow import classify_unchecked as _classify
from tidd_tools.ai_review.approve_flow import run_approve_flow
from tidd_tools.ai_review.post_review import ensure_verdict_in_review_body
from tidd_tools.ai_review.post_review import post_review as _post_review
from tidd_tools.ai_review.state_dir import resolve_state_dir as _resolve_state_dir
from tidd_tools.ai_review.tokens import get_installation_token, get_non_interactive_fallback_token
from tidd_tools.ai_review.verify_ai_confirm import verify_ai_confirm_items
from tidd_tools.shared import gh_client
from tidd_tools.shared.errors import GhCommandError, SubprocessTimeoutError
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)

# Issue #2029: /tmp/agent-review-<N>.md に codex 生セッションログ（161KB 実例: PR #2027）が
# 保存された場合に PR コメントへ流出させないためのサイズ上限。GitHub の Issue/PR コメント
# 本文はハード上限 65536 文字のため、投稿 body（backend footer 込み）の文字数で判定する。
_MAX_REVIEW_COMMENT_CHARS = 65536
_COMMENT_VERDICT_RE = re.compile(
    r"^\s*(?:\*\*)?(?:VERDICT|verdict):(?:\*\*)?\s*(APPROVE|REQUEST_CHANGES)[.!?]?\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_CONSENSUS_APPROVE_RE = re.compile(r"^\s*consensus:\s*2/2\s+APPROVE\b", re.IGNORECASE | re.MULTILINE)


def _state_dir(pr_num: str) -> Path:
    return _resolve_state_dir(pr_num)


def _add_needs_human_merge_label(pr_num: str, repo: str, env: dict[str, str] | None = None) -> None:
    """PR に ``needs-human-merge`` ラベルを付与する（Issue #1297・#1329 で REST 化）.

    停止条件ファイルを含む PR は AI review APPROVE 後も人間マージが必要。GitHub UI 上で
    `is:pr label:needs-human-merge` フィルタで 1 秒抽出できるようにする（監査 §2-2）。

    Issue #1329: 旧実装は ``gh pr edit --add-label`` を使っていたが、gh CLI が内部で
    GraphQL 経由で ``login`` / ``name`` / ``slug`` フィールドを over-fetch し ``read:org``
    scope を要求するため、``repo`` scope のみ持つ GitHub App installation token では失敗
    していた。REST API endpoint ``POST /repos/{repo}/issues/{n}/labels`` は over-fetch
    しないため、``Issues: write``（``repo`` scope 相当）のみで通る。

    失敗しても Silent（ラベル未作成環境等）にせず、stderr に警告を残す（label-pr.py と
    同様の hook 失敗原則・Issue #1295）。
    """
    token = (env or {}).get("GH_TOKEN")
    try:
        gh_client.issue_label_add(pr_num, ["needs-human-merge"], repo, token=token)
    except FileNotFoundError as exc:
        print(
            f"==> WARN: needs-human-merge ラベル付与に失敗しました: {type(exc).__name__}",
            file=sys.stderr,
        )
        return
    except SubprocessTimeoutError:
        print(
            "==> WARN: needs-human-merge ラベル付与に失敗しました: TimeoutExpired",
            file=sys.stderr,
        )
        return
    except GhCommandError as exc:
        stderr_head = (exc.stderr or "").strip().splitlines()[:2]
        detail = " | ".join(stderr_head) if stderr_head else f"exit={exc.returncode}"
        print(
            f"==> WARN: needs-human-merge ラベル付与に失敗しました: {detail}",
            file=sys.stderr,
        )
        return
    print(
        f"==> PR #{pr_num} に 'needs-human-merge' ラベルを付与しました",
        file=sys.stderr,
    )


def _remove_needs_human_merge_label(pr_num: str, repo: str, env: dict[str, str] | None = None) -> None:
    """PR から ``needs-human-merge`` ラベルを除去する（Issue #2473）.

    自動マージ完了後に付与済みラベルを除去することで、GitHub UI の監査フィルタ
    ``is:pr label:needs-human-merge`` にマージ済み PR が残るノイズを防ぐ。

    Issue #1329 と同様の理由で REST API ``DELETE /repos/{repo}/issues/{n}/labels/{name}``
    を使う（``gh pr edit`` の GraphQL over-fetch を避けるため）。

    ラベルが未付与の場合（404 ``Label does not exist``）は auto-merge 成功時の通常の
    ケースであり異常ではないため、stderr に何も出さず正常系として扱う（Issue #4124）。
    それ以外の API エラー（権限不足・レート制限・ネットワーク等）はマージ完了を妨げず、
    stderr に WARN を出すだけに留める（``_add_needs_human_merge_label`` の失敗時挙動に
    合わせる）。
    """
    token = (env or {}).get("GH_TOKEN")
    try:
        gh_client.issue_label_remove(pr_num, "needs-human-merge", repo, token=token)
    except FileNotFoundError as exc:
        print(
            f"==> WARN: needs-human-merge ラベル除去に失敗しました: {type(exc).__name__}",
            file=sys.stderr,
        )
        return
    except SubprocessTimeoutError:
        print(
            "==> WARN: needs-human-merge ラベル除去に失敗しました: TimeoutExpired",
            file=sys.stderr,
        )
        return
    except GhCommandError as exc:
        if "Label does not exist" in (exc.stderr or ""):
            # Issue #4124: ラベル未付与は auto-merge 成功時の通常のケース。
            # needs-human-merge は exit 4 のときだけ付与されるため、素直に
            # 通った PR には最初から付いていない。異常として WARN を出さない。
            return
        stderr_head = (exc.stderr or "").strip().splitlines()[:2]
        detail = " | ".join(stderr_head) if stderr_head else f"exit={exc.returncode}"
        print(
            f"==> WARN: needs-human-merge ラベル除去に失敗しました: {detail}",
            file=sys.stderr,
        )
        return
    print(
        f"==> PR #{pr_num} から 'needs-human-merge' ラベルを除去しました",
        file=sys.stderr,
    )


def _backend_name_raw(pr_num: str) -> str:
    """``STATE_DIR/backend-name`` の生値を返す（Issue #1755）.

    存在しない・読み取り失敗・空値の場合は空文字を返す。
    reviewdog の ``backend_name=`` kwarg に渡すために生値がそのまま必要な用途で使う。
    """
    backend_file = _state_dir(pr_num) / "backend-name"
    if not backend_file.is_file():
        return ""
    try:
        return backend_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _backend_footer(pr_num: str) -> str:
    """``STATE_DIR/backend-name`` を読んで Reviewer フッターを返す.

    存在しない場合は空文字を返す（旧 sh と同じ動作）。
    ``append_backend_to_review_body()`` と同一の ``"> Reviewer:"`` フォーマットを使う
    （Issue #1688 でフォーマット統一）。

    ``backend-name`` ファイルの内容:
    - 新形式: ``"agy:gemini-3-pro"`` → ``"> Reviewer: agy (gemini-3-pro)"``
    - 旧形式: ``"agy"`` → ``"> Reviewer: agy"``（後方互換）
    """
    from tidd_tools.ai_review.reviewdog import append_backend_to_review_body  # noqa: PLC0415

    raw = _backend_name_raw(pr_num)
    if not raw:
        return ""
    return append_backend_to_review_body("", raw)


def _resolve_repo(env_repo: str | None) -> str:
    return gh_client.resolve_repo(env_repo)


def _post_comment_with_app_token(pr_num: str, repo: str, body: str, token: str) -> int:
    try:
        gh_client.pr_comment(pr_num, repo, body, token=token)
    except GhCommandError:
        return 1
    return 0


def _normalize_comment_body(body: str) -> str:
    """コメント投稿境界で監査可能な canonical VERDICT 行を付与する."""
    match = _COMMENT_VERDICT_RE.search(body)
    if match:
        verdict = match.group(1).upper()
    elif _CONSENSUS_APPROVE_RE.search(body):
        verdict = "APPROVE"
    else:
        verdict = "REQUEST_CHANGES"
    return ensure_verdict_in_review_body(body, verdict)


_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def _get_pr_head_sha(pr_num: str, repo: str) -> str | None:
    """PR の head SHA を取得する（Issue #2658）.

    ``gh pr view --json headRefOid`` で PR の現在の head SHA を取得する。
    取得失敗または空の場合は None を返す。
    """
    return gh_client.pr_head_sha(pr_num, repo) or None


# ── --post-comment ──────────────────────────────────────────────────────────


def post_comment(
    pr_num: str,
    body: str,
    *,
    reviewer: str | None = None,
    no_reviewer_footer: bool = False,
) -> int:
    """ボットアカウントで PR にコメントを投稿する.

    Args:
        pr_num: PR 番号。
        body: 投稿するコメント本文。
        reviewer: フッターに使う Reviewer 名（指定時は ``STATE_DIR/backend-name`` を無視する）。
            空文字列はエラー（Issue #2660）。
        no_reviewer_footer: ``True`` の場合はフッターを付加しない（Issue #2660）。
            ``reviewer`` と同時指定はエラー。
    """
    if not pr_num or not body:
        print("ERROR: --post-comment には PR番号 とコメント本文が必要です。", file=sys.stderr)
        return 1
    # --reviewer と --no-reviewer-footer の相互排他チェック（Issue #2660）
    if reviewer is not None and no_reviewer_footer:
        print(
            "ERROR: --reviewer と --no-reviewer-footer は同時に指定できません（相互排他）。",
            file=sys.stderr,
        )
        return 1
    # --reviewer に空文字は不可（Issue #2660）
    if reviewer is not None and reviewer == "":
        print(
            "ERROR: --reviewer に empty 文字列は指定できません。",
            file=sys.stderr,
        )
        return 1
    # フッターの決定（Issue #2660）
    if no_reviewer_footer:
        footer = ""
    elif reviewer is not None:
        from tidd_tools.ai_review.reviewdog import append_backend_to_review_body  # noqa: PLC0415

        footer = append_backend_to_review_body("", reviewer)
    else:
        footer = _backend_footer(pr_num)
    full_body = _normalize_comment_body(body) + footer
    try:
        repo = _resolve_repo(os.environ.get("REPO"))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    # Issue #1712: get_secret() RuntimeError（環境変数にも値がない）は "" に変換して既存フォールバックへ
    try:
        token = get_installation_token()
    except RuntimeError:
        token = ""
    # Issue #3181: App token 取得失敗時は $GITHUB_TOKEN → $GH_TOKEN の非対話トークンへ
    # フォールバックする（gh auth token への降格はしない）。
    if not token:
        token = get_non_interactive_fallback_token()
    if not token:
        print(
            "ERROR: GitHub App トークンの取得に失敗しました。"
            "APP_ID・INSTALLATION_ID・PRIVATE_KEY_PATH を確認してください。",
            file=sys.stderr,
        )
        return 1
    if _post_comment_with_app_token(pr_num, repo, full_body, token) != 0:
        print("ERROR: コメントの投稿に失敗しました。", file=sys.stderr)
        return 1
    print(f"==> ボットアカウントでコメントを投稿しました（PR #{pr_num}）", file=sys.stderr)
    return 0


# ── --continue-with-verdict ─────────────────────────────────────────────────


def emit_post_merge_schedule(pr_num: str, pr_body: str) -> None:
    """auto-merge 完了直後に [AI確認-post-merge] 項目の有無を判定し stderr に出力する（Issue #1964）.

    CronCreate は Claude Code セッション側 tool のため Python から直接呼べない。
    項目が存在する場合は Claude 向けの CronCreate 登録指示行を stderr に出力し、
    Claude Code がそれを読んで CronCreate を実行する設計とする。
    詳細: docs/reference/post-merge-verify-workflow.md#自動トリガー

    Args:
        pr_num: PR 番号（文字列）
        pr_body: PR ボディ全文
    """
    _, _, _, post_merge = _classify(pr_body)
    if not post_merge:
        print(f"skip: no post-merge items in PR #{pr_num}", file=sys.stderr)
        return
    print(f"scheduled: /verify-post-merge {pr_num} in 1h", file=sys.stderr)
    print(
        f"==> [AI確認-post-merge] 未消化項目が {len(post_merge)} 件あります。"
        f"CronCreate で 1 時間後に /verify-post-merge {pr_num} を登録してください。",
        file=sys.stderr,
    )


def _find_latest_backend_log(pr_num: str) -> Path | None:
    """STATE_DIR 配下の最新 backend ログを返す（Issue #2471 フォールバック用）.

    ``codex-review-*.log`` / ``agy-review-*.log`` をタイムスタンプ降順で探索し、
    最新のファイルを返す。存在しない場合は None。
    """
    state = _state_dir(pr_num)
    if not state.is_dir():
        return None
    candidates = sorted(
        [p for p in state.iterdir() if p.suffix == ".log" and p.stem.startswith(("codex-review-", "agy-review-"))],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _read_primary_review_body(pr_num: str) -> tuple[str, str, bool] | None:
    """``AGENT_REVIEW_DIR/agent-review-<N>.md`` を読み込んで (review_output, body, oversized) を返す.

    ファイルが存在しない場合、STATE_DIR 配下の最新 backend ログ（codex-review-*.log /
    agy-review-*.log）へフォールバックする（Issue #2471）。両方存在しない場合は None。

    読み込み失敗時は None（呼び出し元が ERROR ログを出す）。

    Issue #2909: ``body`` は ``_strip_thinking()``（core.py）で ``VERDICT:`` 行より前の
    reasoning テキストを除去してから組み立てる。通常フロー（core.py の ``main()``）は
    投稿前に ``_strip_thinking()`` を呼んでいるが、STATE_DIR 生ログへのフォールバック
    経路（``--continue-with-verdict`` 再開時）はこれを呼んでいなかったため、codex の
    chain-of-thought が PR コメントに混入していた。``review_output``（reviewdog inline
    コメント抽出・oversized 判定の基準等に使う生値）は通常フローと同様ストリップしない。

    Issue #2945: STATE_DIR フォールバックで拾ったログに ``VERDICT:`` 行が一切無い場合
    （backend クラッシュ時の生エラーログ等）は有効なレビュー本文として扱わず None を返す。
    ``agent-review-<N>.md``（primary）が存在する場合はこのチェックを行わない
    （primary ファイルは backend が正常終了した際にのみ書き込まれるため）。
    """
    # ADR 013 (Windows first-class support): AGENT_REVIEW_DIR 未指定時は
    # OS デフォルトの tempfile.gettempdir() に委ねる（Windows: %TEMP% / Linux: /tmp）。
    body_file = Path(os.environ.get("AGENT_REVIEW_DIR") or tempfile.gettempdir()) / f"agent-review-{pr_num}.md"
    is_fallback = False
    if not body_file.is_file():
        # Issue #2471: agent-review ファイルが存在しない場合は STATE_DIR の backend ログへフォールバック。
        # 通常の codex/agy backend が exit 4 を返した後の --continue-with-verdict 再開では
        # agent-review ファイルが書き込まれないため、state dir のログを使う。
        fallback = _find_latest_backend_log(pr_num)
        if fallback is None:
            print(
                f"ERROR: レビュー本文ファイルが見つかりません: {body_file} "
                f"（STATE_DIR フォールバックも見つかりません: {_state_dir(pr_num)}）",
                file=sys.stderr,
            )
            return None
        print(
            f"==> INFO: agent-review ファイルが見つかりません。STATE_DIR の backend ログを使います: {fallback}",
            file=sys.stderr,
        )
        body_file = fallback
        is_fallback = True
    try:
        review_output = body_file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: レビュー本文の読み込みに失敗しました: {exc}", file=sys.stderr)
        return None

    # Issue #2945: STATE_DIR フォールバックログは backend のクラッシュ生ログ（VERDICT: 行なし）
    # である可能性があるため、VERDICT: 行の存在を確認できない場合は投稿せずエラー終了する。
    if is_fallback and not any(line.startswith("VERDICT:") for line in review_output.splitlines()):
        print(
            f"ERROR: STATE_DIR フォールバックログに VERDICT 行が見つからないため、"
            f"生ログの投稿を中断しました: {body_file}",
            file=sys.stderr,
        )
        return None

    from tidd_tools.ai_review.core import _strip_thinking  # noqa: PLC0415

    body = _strip_thinking(review_output) + _backend_footer(pr_num)

    # Issue #2029: codex 生セッションログ等の巨大ファイルを PR コメントに流出させない
    # サイズガード。GitHub コメント上限（65536 文字）を投稿 body（footer 込み）の文字数で
    # 判定する。コメント投稿・reviewdog inline 投稿をスキップし、verdict 後続処理
    # （マージ判定 / リトライ判断）のみ継続する。
    oversized = len(body) > _MAX_REVIEW_COMMENT_CHARS
    if oversized:
        print(
            f"==> WARN: レビューファイルが大きすぎるため PR コメントをスキップします"
            f"（{len(body)} chars > {_MAX_REVIEW_COMMENT_CHARS} chars: {body_file}）。"
            "レビュー本文のみを保存する設計に反しています（生ログ混入の疑い・Issue #2029）。",
            file=sys.stderr,
        )
    return review_output, body, oversized


def _is_cwv_approved_for_sha(approved_flag: Path, head_sha: str | None) -> bool:
    """cwv-approved フラグが現在の head SHA に対して有効かどうかを返す（Issue #2658）.

    head_sha が None の場合（SHA 取得スキップ経路）は従来のファイル存在チェックのみを行う。
    head_sha が指定された場合は、フラグファイルの内容が有効な SHA 形式であり、
    かつ現在の head_sha と一致する場合のみ True を返す。
    フラグファイルが存在しない・内容が不正（空・SHA 形式でない）・SHA が異なる場合は False。
    """
    if not approved_flag.is_file():
        return False
    if head_sha is None:
        # SHA スキップ経路（continue_with_verdict 等）: 後方互換でファイル存在のみ判定
        return True
    try:
        recorded_sha = approved_flag.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    # SHA 形式でない（空・不正・旧形式の空ファイル）場合は古いフラグとして無視
    if not _SHA_RE.match(recorded_sha):
        return False
    return recorded_sha == head_sha


def _post_primary_review(
    pr_num: str,
    repo: str,
    token: str,
    review_output: str,
    body: str,
    oversized: bool,
    *,
    mark_approved: bool,
    as_formal_review: bool = True,
    head_sha: str | None = None,
) -> int:
    """primary review コメント（+ reviewdog inline）を投稿する共通ロジック（Issue #2314）.

    ``continue_with_verdict`` と ``post_primary_review_comment`` の両方から呼ばれる。
    ``mark_approved=True`` かつ ``cwv-approved`` フラグが既に同一 head SHA で存在する場合は
    再投稿をスキップする（APPROVE 冪等化・Issue #862 を踏襲）。

    Args:
        head_sha: PR の現在の head SHA（Issue #2658）。指定された場合は cwv-approved フラグの
            照合を SHA スコープで行い、SHA が変わった場合は古いフラグを無視して再投稿する。
            None の場合は従来通りファイル存在のみで判定する（後方互換）。
        as_formal_review: ``True``（デフォルト）の場合は ``post_review()`` 経由で
            ``gh pr review --approve/--request-changes`` を試みる（STEP 6 最終 verdict 用）。
            ``False`` の場合は ``gh pr comment`` のみで投稿する（STEP 5.5 consensus 確定前の
            primary early post 用・Issue #2523）。
    """
    # Issue #1755: fallback フローでも reviewdog inline コメントを投稿する。
    # 通常フロー (core.py::_handle_approve / _handle_request_changes) と同じパターンで
    # review_output（backend_footer 付加前の生 markdown）を渡す。
    # backend_name は STATE_DIR/backend-name の生値（"claude-code" / "agy:gemini-3-pro" 等）。
    # 例外時は fail-open で本文コメント投稿に進む（fallback フロー本体を止めない）。
    if not oversized:
        from tidd_tools.ai_review.reviewdog import run_inline_comment_review  # noqa: PLC0415

        backend_raw = _backend_name_raw(pr_num) or "agy"
        try:
            run_inline_comment_review(
                pr_num,
                repo,
                token,
                review_output,
                backend_name=backend_raw,
            )
        except Exception as exc:  # noqa: BLE001 — inline 失敗は fallback フロー本体を止めない
            print(
                f"==> WARN: reviewdog inline レビューに失敗しました（スキップして継続）: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    # APPROVE 冪等化（Issue #862 / Issue #2658 SHA スコープ拡張）
    state = _state_dir(pr_num)
    approved_flag = state / "cwv-approved"
    flag_active = _is_cwv_approved_for_sha(approved_flag, head_sha)
    skip_comment = oversized or (mark_approved and flag_active)

    # Issue #2523: skip_comment かつ as_formal_review=True（STEP 6 最終 verdict）の場合は、
    # コメント本文の再投稿はスキップするが、正式な gh pr review --approve のみは必ず発行する。
    # STEP 5.5 で cwv-approved フラグが立っていても STEP 6 で正式レビューが発行されていない
    # ため、consensus 確定を示す短い body で gh pr review --approve を発行する。
    # SKILL.md の記述（「STEP 6 でコメント再投稿をスキップし正式 gh pr review --approve のみ発行」）に準拠。
    if skip_comment:
        if not oversized and as_formal_review:
            print(
                f"==> cwv-approved フラグを検知しました。コメント再投稿をスキップし"
                f"正式レビューのみ発行します（PR #{pr_num}）。",
                file=sys.stderr,
            )
            verdict = "APPROVE" if mark_approved else "REQUEST_CHANGES"
            # consensus 確定を示す短い body（本文の重複を避ける）で正式レビューを発行する
            formal_body = f"consensus 確定（{verdict}）。詳細は先行投稿のコメントを参照してください。"
            posted = _post_review(
                pr_num,
                repo,
                verdict,
                formal_body,
                token,
                as_formal_review=True,
            )
            if not posted:
                print(
                    f"ERROR: 正式レビューの投稿に失敗しました（PR #{pr_num}）。",
                    file=sys.stderr,
                )
                return 1
            print(f"==> 正式レビューを発行しました（PR #{pr_num}）", file=sys.stderr)
        elif not oversized:
            print(
                f"==> cwv-approved フラグを検知しました。コメント投稿をスキップして継続します（PR #{pr_num}）。",
                file=sys.stderr,
            )
        return 0

    # Issue #2523: post_review() に一本化。
    # as_formal_review=True（STEP 6 最終 verdict）: gh pr review --approve/--request-changes を試み、
    #   失敗時のみ gh pr comment にフォールバックする。
    # as_formal_review=False（STEP 5.5 primary early post）: gh pr comment のみで投稿する。
    #   consensus 確定前に正式レビューを残さないことで、secondary が後で REQUEST_CHANGES を返しても
    #   bot の「Approved」正式レビューが GitHub 上に残らない。
    # verdict は mark_approved が True の場合 APPROVE、False の場合 REQUEST_CHANGES。
    verdict = "APPROVE" if mark_approved else "REQUEST_CHANGES"
    posted = _post_review(
        pr_num,
        repo,
        verdict,
        body,
        token,
        as_formal_review=as_formal_review,
    )
    if not posted:
        print(
            f"ERROR: レビューの投稿に失敗しました（PR #{pr_num}）。",
            file=sys.stderr,
        )
        return 1
    print(f"==> ボットアカウントでコメントを投稿しました（PR #{pr_num}）", file=sys.stderr)
    if mark_approved:
        state.mkdir(parents=True, exist_ok=True)
        # Issue #2658: cwv-approved フラグに head SHA を記録して SHA スコープにする。
        # 次回の照合で SHA が一致しない（= 新しいコミットが push された）場合は再投稿する。
        if head_sha:
            approved_flag.write_text(head_sha + "\n", encoding="utf-8")
        else:
            approved_flag.touch()
    return 0


def post_primary_review_comment(pr_num: str) -> int:
    """primary review コメントを secondary consensus チェックより先に投稿する（Issue #2314）.

    parser critical PR は STEP 5.5（secondary consensus チェック）が STEP 6
    （``--continue-with-verdict``）より前に実行されるが、従来 primary review コメントは
    STEP 6 内部でのみ投稿されていたため secondary review コメントより後に投稿されていた。
    verdict-extractor が APPROVE を返した直後（STEP 5.5 実行前）にこの関数を呼び、
    primary review コメントを先に投稿する。投稿後は ``cwv-approved`` フラグを立てるため、
    後続の ``--continue-with-verdict APPROVE`` はコメント再投稿をスキップする。

    Issue #2658: ``cwv-approved`` フラグを head SHA スコープにする。
    PR の現在の head SHA を取得し、SHA が変わった場合は古いフラグを無視して再投稿する。
    SHA 取得に失敗した場合はエラー終了する。
    """
    payload = _read_primary_review_body(pr_num)
    if payload is None:
        return 1
    review_output, body, oversized = payload

    try:
        repo = _resolve_repo(os.environ.get("REPO"))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Issue #2658: head SHA を取得して cwv-approved フラグの SHA スコープ照合に使う。
    # SHA 取得失敗は監査証跡の欠落リスクがあるためエラー終了する。
    head_sha = _get_pr_head_sha(pr_num, repo)
    if head_sha is None:
        print(
            f"ERROR: PR #{pr_num} の head SHA の取得に失敗しました。"
            "リポジトリへのアクセス権限または PR 番号を確認してください。",
            file=sys.stderr,
        )
        return 1

    # Issue #1712: get_secret() RuntimeError（環境変数にも値がない）は "" に変換して既存フォールバックへ
    try:
        token = get_installation_token()
    except RuntimeError:
        token = ""
    # Issue #3181: App token 取得失敗時は $GITHUB_TOKEN → $GH_TOKEN の非対話トークンへ
    # フォールバックする（gh auth token への降格はしない）。
    if not token:
        token = get_non_interactive_fallback_token()
    # Issue #4089: トークン欠落時は通常経路（post_review.py）と同じ degrade で継続する。
    # 空 token のまま _post_primary_review → post_review へ進むと gh pr review をスキップして
    # gh pr comment で VERDICT を投稿する（early return の hard fail はしない）。
    if not token:
        print(
            "WARN: GitHub App トークンを取得できませんでした。"
            "gh pr review をスキップして gh pr comment で VERDICT を投稿します。",
            file=sys.stderr,
        )

    # Issue #2523: STEP 5.5 の primary early post は consensus 確定前のため as_formal_review=False。
    # gh pr comment のみで投稿し、正式な gh pr review は残さない（consensus 確定後の STEP 6 で残す）。
    # Issue #2658: head_sha を渡して cwv-approved フラグの SHA スコープ照合を有効にする。
    return _post_primary_review(
        pr_num,
        repo,
        token,
        review_output,
        body,
        oversized,
        mark_approved=True,
        as_formal_review=False,
        head_sha=head_sha,
    )


def continue_with_verdict(verdict: str, pr_num: str) -> int:
    """Agent tool フォールバック完了後の後続処理を実行する."""
    if verdict not in {"APPROVE", "REQUEST_CHANGES"}:
        print("ERROR: VERDICT は APPROVE または REQUEST_CHANGES を指定してください。", file=sys.stderr)
        return 1
    if not pr_num:
        print("ERROR: 使い方: --continue-with-verdict <APPROVE|REQUEST_CHANGES> <PR番号>", file=sys.stderr)
        return 1

    state = _state_dir(pr_num)
    if (state / "escalated").is_file():
        print(
            f"ERROR: エスカレーション済みのため --continue-with-verdict は実行できません。"
            f"PR #{pr_num} は人間マージが必要です。",
            file=sys.stderr,
        )
        return 1

    payload = _read_primary_review_body(pr_num)
    if payload is None:
        return 1
    review_output, body, oversized = payload

    try:
        repo = _resolve_repo(os.environ.get("REPO"))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Issue #1712: get_secret() RuntimeError（環境変数にも値がない）は "" に変換して既存フォールバックへ
    try:
        token = get_installation_token()
    except RuntimeError:
        token = ""
    # Issue #3181: App token 取得失敗時は $GITHUB_TOKEN → $GH_TOKEN の非対話トークンへ
    # フォールバックする（gh auth token への降格はしない）。
    if not token:
        token = get_non_interactive_fallback_token()
    # Issue #4089: トークン欠落時は通常経路（post_review.py）と同じ degrade で継続する。
    # 空 token のまま _post_primary_review → post_review へ進むと gh pr review をスキップして
    # gh pr comment で VERDICT を投稿する（early return の hard fail はしない）。
    if not token:
        print(
            "WARN: GitHub App トークンを取得できませんでした。"
            "gh pr review をスキップして gh pr comment で VERDICT を投稿します。",
            file=sys.stderr,
        )

    rc = _post_primary_review(pr_num, repo, token, review_output, body, oversized, mark_approved=(verdict == "APPROVE"))
    if rc != 0:
        return rc

    if verdict == "REQUEST_CHANGES":
        print(
            "==> VERDICT: REQUEST_CHANGES → exit 1（呼び出し元がリトライを判断してください）",
            file=sys.stderr,
        )
        return 1

    # APPROVE: タスクチェック → 停止条件 → マージ
    # #3181: token が無いまま approve flow を続けると、内部の gh 呼び出しが
    # 個人の ambient OAuth セッションを使って自動マージするため、人間マージ待ちに倒す。
    # VERDICT コメントの投稿（上記 _post_primary_review）は tokenless degrade として許可する。
    if not token:
        print(
            "WARN: GitHub App/非対話トークンがないため自動マージを実行しません。"
            "VERDICT コメントを確認して人間がマージしてください（exit 4）。",
            file=sys.stderr,
        )
        return 4
    return _continue_approve(pr_num, repo, token)


def _continue_approve(pr_num: str, repo: str, token: str) -> int:
    """``--continue-with-verdict`` フォールバック経路の APPROVE 確定後処理.

    Issue #2941: 実処理は ``approve_flow.run_approve_flow`` へ統合済み。本関数は
    continue 経路向けの引数を固定した薄いラッパー（``core._finalize_approve`` と
    経路が分裂して片側修正漏れが起きる事故（#2036・#2074）を防ぐ）。

    Issue #2991: ``post_merge_summary=True``。agy/codex 両方利用不可のフォールバック
    （``ai-fallback-reviewer`` 経由の ``--continue-with-verdict``）でマージした場合も
    通常経路（``core._finalize_approve``）と同様に所要時間サマリを投稿する。
    ``ai-review-timing/<pr_num>.jsonl``（#2936 で書き込み撤去）の step マーカーが欠落していても
    ``merge_summary.py`` 側の「計測不可」フォールバック表示により行欠落なく投稿される。
    """
    return run_approve_flow(
        pr_num,
        repo,
        token,
        run_evidence_tick=False,
        run_test_status_post=True,
        resolve_token=False,
        use_measure_step=False,
        check_ci_status_after_gate=True,
        remove_label_on_merge_success=True,
        post_merge_summary=True,
    )


# ── --verify-ai-confirm （Issue #1246） ──────────────────────────────────────


def _resolve_repo_root() -> Path:
    """git rev-parse --show-toplevel でリポジトリルートを返す（失敗時は CWD）."""
    proc = run_subprocess(
        ["git", "rev-parse", "--show-toplevel"],
        capture=True,
        check=False,
        timeout=10,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return Path.cwd()


def verify_ai_confirm_command(pr_num: str) -> int:
    """PR ボディの ``[AI確認]`` 項目を Tool Calling で自律検証し、全て verified なら merge に進む.

    Returns:
        0: 全項目 verified で自動マージ完了
        4: ANTHROPIC_API_KEY 未設定 / 一部項目が unverified / 停止条件により人間マージ待ち
        1: 実行エラー（PR fetch 失敗など）
    """
    if not pr_num:
        print("ERROR: --verify-ai-confirm には PR番号 が必要です。", file=sys.stderr)
        return 1

    try:
        repo = _resolve_repo(os.environ.get("REPO"))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Issue #1712: get_secret() RuntimeError（環境変数にも値がない）は "" に変換して既存フォールバックへ
    try:
        token = get_installation_token()
    except RuntimeError:
        token = ""
    try:
        pr_data = gh_client.pr_view(pr_num, repo=repo, fields=("body",), token=token)
    except GhCommandError as exc:
        print(
            f"ERROR: PR #{pr_num} のボディを取得できませんでした（exit: {exc.returncode}）。",
            file=sys.stderr,
        )
        return 1
    pr_body = pr_data.get("body") or ""

    repo_root = _resolve_repo_root()
    # Issue #1304: verify_ai_confirm_items() は subprocess から呼ばれる前提のため常に skip する。
    # 実際の検証は /issue-next skill 側で Agent tool + ai-confirm-verifier subagent が担当する。
    result = verify_ai_confirm_items(pr_body, repo_root=repo_root)

    if result.skipped:
        # Issue #1304 Gherkin 準拠: session 外／subprocess 経由の呼び出しは exit 0 で
        # clean skip する。実際の [AI確認] 検証は /issue-next skill が exit 4（ai-review
        # 本体の戻り値）を受けて Agent tool + ai-confirm-verifier subagent を直接起動する
        # 設計に完全移行済み（.claude/skills/issue-next/SKILL.md 参照）。
        # したがって本 CLI subcommand は呼び出し元不在の legacy 経路であり、
        # 呼ばれた場合は警告付き exit 0 で clean 終了する。
        from tidd_tools.ai_review.verify_ai_confirm import _extract_ai_confirm_lines

        pending_items = _extract_ai_confirm_lines(pr_body)
        if pending_items:
            print(
                f"==> [AI確認] 項目が {len(pending_items)} 件残っていますが、"
                "検証は /issue-next skill 側の ai-confirm-verifier subagent が担当します。"
                " CLI 経由の skip 時は exit 0 で clean 終了します。",
                file=sys.stderr,
            )
        else:
            print(
                "==> [AI確認] 項目が残っていません。skip して正常終了します（exit 0）。",
                file=sys.stderr,
            )
        return 0

    if result.verified_items and result.updated_body != pr_body:
        # PR ボディを更新して verified 項目を [x] にする
        try:
            gh_client.pr_edit_body(pr_num, repo, result.updated_body, token=token)
        except GhCommandError as exc:
            print(
                f"ERROR: gh pr edit に失敗しました（exit: {exc.returncode}）。",
                file=sys.stderr,
            )
            return 1
        print(
            f"==> PR body を更新しました（verified 項目 {len(result.verified_items)} 件を [x] に）。",
            file=sys.stderr,
        )

    if result.unverified_items:
        print(
            "==> [AI確認] のうち検証できなかった項目があります。人間マージに委ねます（exit 4）。",
            file=sys.stderr,
        )
        for line in result.unverified_items:
            print(f"  - {line.strip()}", file=sys.stderr)
        return 4

    if not result.verified_items:
        print(
            "==> PR に [AI確認] 項目がありませんでした。人間マージに委ねます（exit 4）。",
            file=sys.stderr,
        )
        return 4

    # 全て verified → 停止条件チェック → マージ
    print(
        "==> 全 [AI確認] 項目を verified としてマークしました。マージ判定に進みます...",
        file=sys.stderr,
    )
    if token:
        return _continue_approve(pr_num, repo, token)
    print(
        "==> GitHub App トークン未取得のため自動マージできません。人間マージに委ねます（exit 4）。",
        file=sys.stderr,
    )
    return 4


# ── --consensus-verdict （Issue #2657） ──────────────────────────────────────


def _escalate_consensus(pr_num: str, title: str, message: str) -> int:
    """secondary consensus のエスカレーション処理共通ヘルパー（Issue #2948）.

    「repo解決 → GitHub App token取得 → needs-human-merge ラベル付与 → PRコメント投稿」という
    consensus_verdict() 内で 3 回コピーされていた処理を 1 箇所にまとめる。
    repo が解決できない場合はラベル付与・コメント投稿を行わずに 2 を返す。
    token 取得に失敗した場合はラベル付与・コメント投稿を個人アカウント権限のまま続行する
    （旧 3 箇所共通のフォールバック挙動）。

    Args:
        pr_num: PR 番号（文字列）
        title: コメント見出し（例: "secondary consensus 異常"）
        message: コメント本文の詳細説明

    Returns:
        2（エスカレーション終了コード）固定
    """
    try:
        repo = _resolve_repo(os.environ.get("REPO"))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    env = dict(os.environ)
    try:
        token = get_installation_token()
        if token:
            env["GH_TOKEN"] = token
    except RuntimeError:
        pass
    _add_needs_human_merge_label(pr_num, repo, env)
    comment_body = f"VERDICT: REQUEST_CHANGES\n\n## {title}（エスカレーション）\n\n{message}\n\n人間の判断が必要です。"
    # bot アカウントで投稿する（same-issue 経路との対称性・Issue #2675）
    post_comment(pr_num, comment_body, no_reviewer_footer=True)
    return 2


def consensus_verdict(pr_num: str, secondary_verdict: str, secondary_issues_json: str, attempt: int) -> int:
    """parser critical PR の secondary consensus 判定を行う（Issue #2657）.

    primary APPROVE + secondary REQUEST_CHANGES の consensus 不一致を処理する。
    1 回目の不一致はエスカレーションせずリトライ継続（exit 1）し、
    2 回目以降で同一指摘が繰り返された場合にエスカレーション（exit 2）する。

    Args:
        pr_num: PR 番号（文字列）
        secondary_verdict: secondary の verdict（"APPROVE" | "REQUEST_CHANGES"）
        secondary_issues_json: secondary の issues を JSON 配列文字列で渡す（例: '["[CRITICAL] ..."']）
        attempt: 試行回数（1 以上）

    Returns:
        0: secondary が APPROVE（consensus 通過）
        1: secondary が REQUEST_CHANGES かつリトライ継続（ラベル未付与）
        2: エスカレーション（同一指摘 2 回連続 or バックストップ上限 or issues 空）
    """
    from tidd_tools.ai_review.core import is_backstop_exceeded  # noqa: PLC0415
    from tidd_tools.ai_review.verdict import is_same_issues  # noqa: PLC0415

    if not pr_num:
        print("ERROR: --consensus-verdict には PR番号 が必要です。", file=sys.stderr)
        return 2

    if secondary_verdict not in {"APPROVE", "REQUEST_CHANGES"}:
        print(
            f"ERROR: secondary_verdict は APPROVE または REQUEST_CHANGES を指定してください"
            f"（got: {secondary_verdict!r}）。",
            file=sys.stderr,
        )
        return 2

    # secondary APPROVE の場合は consensus 通過（呼び出し元が STEP 4 へ進む）
    if secondary_verdict == "APPROVE":
        print(
            "==> consensus: 2/2 APPROVE（secondary も APPROVE）",
            file=sys.stderr,
        )
        return 0

    # secondary REQUEST_CHANGES: issues をパース
    try:
        secondary_issues_list: list[str] = json.loads(secondary_issues_json)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"ERROR: secondary_issues_json のパースに失敗しました: {exc}",
            file=sys.stderr,
        )
        return 2

    if not isinstance(secondary_issues_list, list):
        print(
            "ERROR: secondary_issues_json はリスト型である必要があります。",
            file=sys.stderr,
        )
        return 2

    # issues が空配列で REQUEST_CHANGES は即エスカレーション（異常系）
    if not secondary_issues_list:
        print(
            "==> secondary issues が空のまま REQUEST_CHANGES が返りました。secondary レビューの異常です。",
            file=sys.stderr,
        )
        return _escalate_consensus(
            pr_num,
            "secondary consensus 異常",
            "secondary issues が空のまま REQUEST_CHANGES が返りました。secondary レビューの異常です。",
        )

    # secondary issues を文字列として結合（is_same_issues が改行区切りリストを期待）
    curr_secondary_issues = "\n".join(secondary_issues_list)

    state = _state_dir(pr_num)

    # バックストップ上限チェック（primary 経路と共有・Issue #2948）
    exceeded, backstop = is_backstop_exceeded(attempt)
    if exceeded:
        print(
            f"==> attempt（{attempt}）が BACKSTOP_MAX_RETRIES（{backstop}）を超えています。"
            "強制エスカレーションします。",
            file=sys.stderr,
        )
        return _escalate_consensus(
            pr_num,
            "secondary consensus バックストップ超過",
            f"attempt（{attempt}）が BACKSTOP_MAX_RETRIES（{backstop}）を超えました。リトライで解決できていません。",
        )

    prev_secondary_issues_file = state / "prev-secondary-issues"

    # 2 回目以降かつ同一指摘ならエスカレーション
    if attempt >= 2 and is_same_issues(prev_secondary_issues_file, curr_secondary_issues):
        print(
            "==> 同じ secondary 指摘が 2 回連続で出ました（修正が根本的に解決できていません）。",
            file=sys.stderr,
        )
        return _escalate_consensus(
            pr_num,
            "secondary consensus 不一致",
            "同じ secondary 指摘が 2 回連続で出ました。修正が根本的に解決できていません。",
        )

    # 1 回目（または指摘内容が変わった場合）: リトライ継続
    # prev-secondary-issues に今回の issues を保存（次回の is_same_issues 判定用）
    try:
        state.mkdir(parents=True, exist_ok=True)
        prev_secondary_issues_file.write_text(curr_secondary_issues + "\n", encoding="utf-8")
        print(
            f"==> secondary 指摘を保存しました: {prev_secondary_issues_file}",
            file=sys.stderr,
        )
    except OSError as exc:
        print(
            f"==> WARN: secondary issues の保存に失敗しました: {exc}",
            file=sys.stderr,
        )

    print(
        "==> consensus 不一致（1 回目）: リトライします。secondary 指摘を修正して再実行してください。",
        file=sys.stderr,
    )
    return 1
