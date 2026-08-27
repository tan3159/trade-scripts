"""APPROVE 確定後マージフローの共通実装（Issue #2941）.

``core.py::_finalize_approve``（通常経路）と ``subcommands.py::_continue_approve``
（``--continue-with-verdict`` フォールバック経路）は、「PR body 取得 → 未チェック分類 →
``[手動]`` → ``[AI確認-post-merge]`` 変換 → Issue やること gate → CI status gate → merge」
というほぼ同一シーケンスを別々に実装しており、片側だけ修正されて経路間の挙動が食い違う
事故が構造的に再発していた（実績: #2036・#2074）。

本モジュールは両経路が共有する単一実装を提供する。経路固有の差分（evidence-tick の
有無・token 解決方針・measure_step でのラップ有無等）は :func:`run_approve_flow` の
キーワード引数として明示的にパラメータ化する。

timing 計測（save_timing）の呼び出し位置は #2924 のスコープであり本 Issue では変更しない。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from typing import Any

from tidd_tools.shared.checklist import classify_checkboxes


def classify_unchecked(pr_body: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """PR ボディから未チェック項目を分類する.

    post_merge はマージ後に cron が検証するため pre-merge gate（exit 1 / exit 4）
    の対象外（#1938・仕様: .claude/rules/test-plan-checklist.md）。
    分類ロジック本体は ``shared.checklist.classify_checkboxes()``（Issue #2942 で
    core.py・subcommands.py の重複実装と統合）に委譲する。

    Returns:
        (auto_uncovered, ai_confirm, manual, post_merge) のタプル
    """
    result = classify_checkboxes(pr_body)
    return result.auto, result.ai_confirm, result.manual, result.post_merge


def _convert_manual_to_post_merge(pr_num: str, repo: str, pr_body: str, manual: list[str], token: str) -> str:
    """PR 本文に残る ``[手動]`` 項目の自動変換を行わない（Issue #4214: 設計選択肢 A）.

    旧実装（#2941 で core.py::_finalize_approve と subcommands.py::_continue_approve の
    逐語重複を統合したもの）は ``[手動]`` を無条件に ``[AI確認-post-merge]`` へ変換して
    ``gh_client.pr_edit_body()`` 経由で PR を更新していた（#3787 で subprocess 直接呼び出し
    から置き換え）。しかしこの無条件変換は ``docs/reference/post-merge-verify-workflow.md``
    の判断ツリー上「人間固有の判断が必要」（構造的に検証不能）と分類されるべき項目まで
    機械アップグレードし、``## やること`` 全消化 gate をすり抜けて自動マージされる実害が
    consumer で複数件実測された（tan3159/mn-scripts#1453）。また、
    ``transfer_issue_test_items`` 側で人間が意図的に ``[手動]`` へ戻した項目を、この関数が
    ``tidd ai-review`` 実行のたびに再度 ``[AI確認-post-merge]`` へ上書きする問題もあった。

    安全側の対応として、本関数は PR 本文を変更せずそのまま返す
    （``gh_client.pr_edit_body()`` も呼び出さない）。``[手動]`` から ``[AI確認-post-merge]``
    へのアップグレードは人間が明示的に PR 本文を編集して行う運用とする。
    """
    return pr_body


def _make_issue_body_fetcher(repo: str, token: str) -> Callable[[int], str | None]:
    """Issue やること gate（yaru_merge_gate）用の Issue body 取得 closure を生成する.

    core.py::_finalize_approve と subcommands.py::_continue_approve に逐語重複していた
    ``_fetch_issue_body`` closure を統合したもの（#2941）。
    """
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token

    def _fetch_issue_body(issue_number: int) -> str | None:
        proc = subprocess.run(  # noqa: S603
            ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "body", "-q", ".body"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
            timeout=60,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout

    return _fetch_issue_body


def _maybe_run_evidence_tick(
    run_evidence_tick: bool,
    pr_num: str,
    repo: str,
    step: Callable[[str], contextlib.AbstractContextManager[None]],
) -> None:
    """evidence-based auto-tick (Issue #1534) を実行する（run_evidence_tick=False なら何もしない）.

    AI_REVIEW_YARU_AUTO_TICK=1 で opt-in。ハルシネーション対策として LLM が
    「delivered」と主張しても Python 側で evidence.quote が PR diff に literal に
    存在することを再検証してから tick する。失敗しても merge を止めない。

    Issue #3315: run_approve_flow の C901 baseline drain で extract method（複雑度分割）。
    """
    if not run_evidence_tick:
        return
    from tidd_tools.ai_review import yaru_auto_tick as _yaru_auto_tick

    try:
        with step("yaru-evidence-tick"):
            _yaru_auto_tick.run(str(pr_num), repo)
    except Exception as exc:  # noqa: BLE001 — auto-tick 失敗は merge を止めない
        print(
            f"==> WARN: yaru-auto-tick failed ({exc}). 続行して merge に進みます。",
            file=sys.stderr,
        )


def _maybe_post_test_statuses(
    run_test_status_post: bool,
    pr_num: str,
    repo: str,
    token: str,
) -> int | None:
    """test-plan status 投稿 (Issue #2074)。失敗時は早期 exit code（1）を返す.

    continue 経路（--continue-with-verdict）は core.py::main() の test-plan フローを
    通らないため、lint/pytest/jest の commit status が一切投稿されないまま merge
    されていた。共通関数 _post_test_statuses() で通常経路と同じ status を投稿する
    （run_test_status_post=False なら None を返し何もしない）。

    Issue #3315: run_approve_flow の C901 baseline drain で extract method（複雑度分割）。
    """
    if not run_test_status_post:
        return None
    from tidd_tools.ai_review.subcommands import _resolve_repo_root, _state_dir
    from tidd_tools.ai_review.test_statuses import _post_test_statuses

    test_status_exit = _post_test_statuses(pr_num, repo, _resolve_repo_root(), _state_dir(pr_num), 0, app_token=token)
    if test_status_exit != 0:
        print(
            "==> ERROR: lint/pytest/jest チェックが失敗したため自動マージしません。"
            "修正してから再度 push してください。",
            file=sys.stderr,
        )
        return 1
    return None


def _resolve_pr_body_or_exit(pr_num: str, repo: str, body_token: str) -> tuple[str | None, int | None]:
    """PR body を取得する。取得失敗（空含む）は安全側の exit 4 を返す（Issue #2181）.

    Issue #3315: run_approve_flow の C901 baseline drain で extract method（複雑度分割）。
    """
    from tidd_tools.ai_review.core import _pr_body

    pr_body_text, body_exit = _pr_body(pr_num, repo, body_token)
    if not pr_body_text:
        print(
            f"WARN: PR #{pr_num} のボディを取得できませんでした（exit: {body_exit}）。"
            "安全のため人間マージ待ちに倒します（exit 4）。",
            file=sys.stderr,
        )
        return None, 4
    return pr_body_text, None


def _report_unchecked_tasks(
    auto: list[str], ai_confirm: list[str], manual: list[str], post_merge: list[str]
) -> int | None:
    """未完了タスク（auto/ai_confirm/manual/post_merge）を報告し、必要なら早期 exit code を返す.

    post_merge はマージ後に cron が検証するため pre-merge gate（exit 1 / exit 4）
    の対象外（#1938・仕様: .claude/rules/test-plan-checklist.md）。

    Issue #4214（PR #4215 レビュー指摘）: `[手動]` は `_convert_manual_to_post_merge` が
    no-op のため PR body に残り続けるが、従来この関数に渡されておらず未完了のまま
    auto-merge まで進んでいた。`[AI確認]` と同様に人間確認が必要な項目のため exit 4
    （人間マージ待ち）に倒す。

    Issue #3315: run_approve_flow の C901 baseline drain で extract method（複雑度分割）。
    """
    if post_merge:
        print(
            "==> [AI確認-post-merge] タスクはマージ後に検証されるため auto-merge をブロックしません:",
            file=sys.stderr,
        )
        for line in post_merge:
            print(line, file=sys.stderr)
    if auto:
        print(
            "ERROR: PR ボディに未完了の自動検証タスク（- [ ]）が残っています。マージをブロックします。",
            file=sys.stderr,
        )
        print("==> 未完了タスク一覧:", file=sys.stderr)
        for line in auto:
            print(line, file=sys.stderr)
        print("==> 全タスクを完了（- [x]）にしてから再実行してください。", file=sys.stderr)
        return 1
    if ai_confirm:
        print(
            "==> PR ボディに未完了の [AI確認] タスクが残っています。Claude が検証してから [x] にしてください。",
            file=sys.stderr,
        )
        print("==> [AI確認] タスク一覧:", file=sys.stderr)
        for line in ai_confirm:
            print(line, file=sys.stderr)
        print(
            "==> AIレビューは APPROVE 済みです。[AI確認] タスクを Claude が検証してからマージしてください。",
            file=sys.stderr,
        )
        return 4
    if manual:
        print(
            "==> PR ボディに未完了の [手動] タスクが残っています。人間が確認してから [x] にしてください。",
            file=sys.stderr,
        )
        print("==> [手動] タスク一覧:", file=sys.stderr)
        for line in manual:
            print(line, file=sys.stderr)
        print(
            "==> AIレビューは APPROVE 済みです。[手動] タスクは人間確認が必要なため自動マージしません。",
            file=sys.stderr,
        )
        return 4
    return None


def _handle_gate_failure(
    gate_result: Any,
    pr_num: str,
    repo: str,
    token: str,
    resolve: Callable[[str], str],
) -> None:
    """Issue やること gate 不通過時の詳細出力・needs-human-merge ラベル付与を行う（Issue #1297）.

    Issue #3315: run_approve_flow の C901 baseline drain で extract method（複雑度分割）。
    """
    if not gate_result.fetch_error:
        print("==> Issue やること 未完了項目:", file=sys.stderr)
        for uc in gate_result.unchecked_items:
            print(f"  Issue #{uc['issue']}: - [ ] {uc['item']}", file=sys.stderr)
    from tidd_tools.ai_review.subcommands import _add_needs_human_merge_label

    label_token = resolve(token)
    label_env = dict(os.environ)
    if label_token:
        label_env["GH_TOKEN"] = label_token
    _add_needs_human_merge_label(str(pr_num), repo, env=label_env)
    print(
        "==> AIレビューは APPROVE 済みですが Issue やること 未完のため自動マージしません。",
        file=sys.stderr,
    )


def _ci_status_early_exit(
    check_ci_status_after_gate: bool,
    pr_num: str,
    repo: str,
    token: str,
) -> int | None:
    """CI status 未送信 gate（Issue #2036）。check_ci_status_after_gate=False なら None を返す.

    fallback / --continue-with-verdict 経路でも変更ファイルに必要な CI コンテキストが
    未送信のままマージされるのを防ぐ。通常経路は呼び出し元（_handle_approve /
    handle_sha_cache_hit）で既に確認済みのため対象外。

    Issue #3315: run_approve_flow の C901 baseline drain で extract method（複雑度分割）。
    """
    if not check_ci_status_after_gate:
        return None
    from tidd_tools.ai_review.subcommands import _resolve_repo_root
    from tidd_tools.ai_review.test_status_gate import check_missing_ci_statuses

    # Issue #4138: mermaid-lint/docs 判定に repo_root を渡し、内部 merge gate の
    # 通常経路（core.py::_check_commit_status_gate）と hook 側の判定と揃える。
    missing_ci = check_missing_ci_statuses(str(pr_num), repo, token, repo_root=_resolve_repo_root())
    if missing_ci:
        print(
            "==> AIレビューは APPROVE 済みですが CI status が未送信のため自動マージしません。",
            file=sys.stderr,
        )
        print("==> CI を実行してから再度 push してください。", file=sys.stderr)
        return 4
    return None


def _record_step6_boundary(pr_body_text: str, step: str) -> None:
    """マージ開始・完了境界（`step6-merge-start`/`step6-merged`）を自己記録する（Issue #3516）.

    PR body の ``closes #N`` から Issue 番号を解決し、対応する統一日誌（issue-<N>）へ
    point イベントを冪等記録する。``closes #N`` が見つからない場合は記録をスキップして
    処理を継続する（例外を投げない）。
    """
    from tidd_tools import timing_log
    from tidd_tools.shared.issue_body import extract_closes_issues

    issue_nums = extract_closes_issues(pr_body_text)
    if not issue_nums:
        return
    timing_log.record_event_once_safe(f"issue-{issue_nums[0]}", step, "point", "ai-review")


def run_approve_flow(
    pr_num: str,
    repo: str,
    token: str,
    *,
    run_evidence_tick: bool,
    run_test_status_post: bool,
    resolve_token: bool,
    use_measure_step: bool,
    check_ci_status_after_gate: bool,
    remove_label_on_merge_success: bool,
    post_merge_summary: bool,
) -> int:
    """APPROVE 確定後の共通後処理: evidence-tick(任意) → 未完了タスク → Issue やること gate → merge.

    ``core.py::_finalize_approve``（通常経路・SHA キャッシュヒット経路）と
    ``subcommands.py::_continue_approve``（``--continue-with-verdict`` フォールバック経路）の
    両方から呼ばれる単一実装（#2941）。経路固有の差分はキーワード引数で明示する:

    Args:
        pr_num: PR 番号
        repo: ``owner/repo``
        token: 呼び出し元が保持する GitHub token（GitHub App installation token 等）
        run_evidence_tick: True なら yaru evidence-based auto-tick（#1534）を実行する
            （通常経路のみ。continue 経路は fallback 実行のため対象外）
        run_test_status_post: True なら lint/pytest/jest の commit status 投稿
            （``_post_test_statuses``）を先頭で実行する（continue 経路のみ・#2074:
            continue 経路は core.py の test-plan フローを通らないため必要）
        resolve_token: True なら ``get_effective_review_token()`` で実効 token を都度解決する
            （通常経路）。False なら ``token`` 引数をそのまま使う（continue 経路）
        use_measure_step: True なら yaru-evidence-tick / issue-exhaustion-gate / auto-merge の
            各ステップを ``measure_step`` で計測する（通常経路のみ）
        check_ci_status_after_gate: True なら Issue やること gate 通過後・merge 前に
            CI status 未送信 gate（``check_missing_ci_statuses``）を再チェックする
            （continue 経路のみ・#2036: 通常経路は呼び出し元で既に確認済みのため不要）
        remove_label_on_merge_success: True なら merge 成功後に ``needs-human-merge``
            ラベルを除去する（continue 経路のみ・#2473）
        post_merge_summary: True なら merge 成功後に所要時間サマリを投稿する
            （通常経路のみ・#2790）

    Returns:
        exit code（0=auto-merge 完了 / 1=未完了タスクあり・test status 失敗 /
        4=人間マージ待ち）
    """
    from tidd_tools.ai_review.core import (
        _gh_pr_merge,
        _rescue_transfer_manual_items,
        get_effective_review_token,
        measure_step,
    )

    def _resolve(tok: str) -> str:
        return get_effective_review_token(tok) if resolve_token else tok

    @contextlib.contextmanager
    def _step(step_name: str) -> Iterator[None]:
        if use_measure_step:
            with measure_step(pr_num, step_name):
                yield
        else:
            yield

    _maybe_run_evidence_tick(run_evidence_tick, pr_num, repo, _step)

    # ── test-plan status 投稿 (Issue #2074) ─────────────────────────────
    test_status_exit = _maybe_post_test_statuses(run_test_status_post, pr_num, repo, token)
    if test_status_exit is not None:
        return test_status_exit

    # ── 未完了タスクチェック ────────────────────────────────────────────
    # Issue #2181: PR body 取得に失敗（または空）の場合は Issue やること gate を素通り
    # させず exit 4（人間マージ待ち）に倒す。
    body_token = _resolve(token)
    pr_body_text, body_exit_code = _resolve_pr_body_or_exit(pr_num, repo, body_token)
    if body_exit_code is not None:
        return body_exit_code
    assert pr_body_text is not None  # _resolve_pr_body_or_exit の契約により None なら早期 return 済み

    auto, ai_confirm, manual, post_merge = classify_unchecked(pr_body_text)
    unchecked_exit = _report_unchecked_tasks(auto, ai_confirm, manual, post_merge)
    if unchecked_exit is not None:
        return unchecked_exit

    # Issue #2026 第 2 段: Issue やること の [手動]/[AI確認] 未 tick 項目を転記する（転記漏れ救済）
    # 更新後の PR body を受け取り以降の emit_post_merge_schedule 等に渡す（stale body 防止）
    pr_body_text = _rescue_transfer_manual_items(pr_num, repo, pr_body_text, token)
    # Issue #4214（PR #4215 レビュー指摘 2 件目）: 転記救済で新規追加された未完了項目は
    # 転記前の分類（上の unchecked_exit チェック）では検出できないため、更新後の PR body
    # で再分類し、未完了項目が残っていれば auto-merge をブロックする。
    auto, ai_confirm, manual, post_merge = classify_unchecked(pr_body_text)
    unchecked_exit = _report_unchecked_tasks(auto, ai_confirm, manual, post_merge)
    if unchecked_exit is not None:
        return unchecked_exit
    # Issue #4214: PR 本文に直接 [手動] 項目が残っていても [AI確認-post-merge] へは
    # 変換しない（設計選択肢 A。_convert_manual_to_post_merge は現在 no-op）。
    if manual:
        pr_body_text = _convert_manual_to_post_merge(pr_num, repo, pr_body_text, manual, token)

    # ── Issue やること 全消化チェック (Issue #1756) ──────────────────────
    # 停止条件ファイル gate (旧) は廃止。ファイル種別は「AI が計画を完遂できたか」と
    # 相関しないため、代わりに Issue の ## やること checkbox 全消化を gate にする。
    from tidd_tools.ai_review.yaru_merge_gate import check_pr as _check_yaru_gate

    fetch_issue_body = _make_issue_body_fetcher(repo, token)

    with _step("issue-exhaustion-gate"):
        gate_result = _check_yaru_gate(pr_body_text or "", fetch_issue_body=fetch_issue_body)
    # feature (issue-1756) 準拠: skip 理由も stderr で観測可能にするため pass/fail 両方で出力する
    print(f"==> {gate_result.reason}", file=sys.stderr)
    if not gate_result.passed:
        _handle_gate_failure(gate_result, pr_num, repo, token, _resolve)
        return 4

    # ── CI status 未送信 gate（Issue #2036）───────────────────────────────
    ci_status_exit = _ci_status_early_exit(check_ci_status_after_gate, pr_num, repo, token)
    if ci_status_exit is not None:
        return ci_status_exit

    # ── 自動マージ ────────────────────────────────────────────────────────
    print("==> VERDICT: APPROVE → gh pr merge --squash --delete-branch を実行します...", file=sys.stderr)
    merge_token = _resolve(token)
    _record_step6_boundary(pr_body_text or "", "step6-merge-start")
    with _step("auto-merge"):
        merge_exit = _gh_pr_merge(pr_num, repo, merge_token)
    if merge_exit != 0:
        print(
            f"==> WARN: gh pr merge が失敗しました（終了コード: {merge_exit}）。手動でマージしてください。",
            file=sys.stderr,
        )
        return 4
    _record_step6_boundary(pr_body_text or "", "step6-merged")
    print(f"==> マージ完了（PR #{pr_num}）", file=sys.stderr)

    # Issue #2473: マージ完了後に needs-human-merge ラベルを除去する（失敗時は WARN のみ）
    if remove_label_on_merge_success:
        from tidd_tools.ai_review.subcommands import _remove_needs_human_merge_label

        remove_env = dict(os.environ)
        if merge_token:
            remove_env["GH_TOKEN"] = merge_token
        _remove_needs_human_merge_label(str(pr_num), repo, remove_env)

    print("==> VERDICT: APPROVE → exit 0（自動マージ完了）", file=sys.stderr)

    # Issue #1964: [AI確認-post-merge] 項目があれば CronCreate 登録指示を出力する
    from tidd_tools.ai_review.subcommands import emit_post_merge_schedule

    emit_post_merge_schedule(str(pr_num), pr_body_text or "")

    # Issue #2790: auto-merge 成功時に所要時間サマリを PR へ投稿する
    # /issue-next 経由・直接実行どちらの場合もサマリが投稿されることを保証する。
    # 投稿失敗・closes #N 未記載でも exit code を変えない（merge_summary 内で吸収）。
    # merge_token・repo を渡すのは、GitHub App token のみでマージできた経路では
    # ambient な gh 認証がなく、また CWD 側のリポジトリへ誤投稿しうるため。
    if post_merge_summary:
        from tidd_tools.merge_summary import post_merge_summary_from_pr_body

        post_merge_summary_from_pr_body(str(pr_num), pr_body_text or "", token=merge_token, repo=repo)

    return 0
