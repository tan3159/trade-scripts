"""`tidd pre-flight` サブコマンド（Issue #1997）.

PR 作成前にローカルで機械的チェックを実行し、`tidd ai-review` での
REQUEST_CHANGES → 修正 → push → 再レビューの往復を削減する。

チェック内容（既存ロジックを再利用・重複実装しない）:

1. lint 一式（ruff format / ruff lint / mypy / context budget / gherkin-lint /
   prettier/css / stylelint/css）
   — `ai_review.test_statuses._post_lint_statuses` を sha/token 空（can_post=False）で呼び、
   GitHub Commit Status 投稿なしのローカル実行として再利用する
2. feat/fix ブランチの `.feature` + step_defs 存在チェック
   — `test_plan` の判定 regex（`_FEATURE_REQUIRED_PATH_RE` 等・#1464/#1855/#1962 と同基準）を再利用する
3. 変更プロジェクトの pytest / Jest 実行
   — `test_plan._run_pytest` / `test_plan._run_jest` を再利用する

このほか、`[AI確認]` 未消化項目（Issue 本文 or PR ボディの `## Test plan`）を stdout へ
事前提示する（Issue #4074・ブロックはしない。詳細: `.claude/rules/test-plan-checklist.md`）。

変更ファイルは PR ではなく `git diff --name-only origin/main` から取得する
（pre-flight は PR 作成前に実行されるため）。

終了コード:
- 0 → 全チェック GREEN（stdout に "pre-flight: all checks passed"）
- 1 → 失敗あり（stderr に失敗したチェック名・プロジェクト名を出力）
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

import yaml

from tidd_tools import (
    context_budget,
    issue_next_state,
    preflight_markers,
    pytest_tmpdir,
    test_plan,
    timing_log,
    tmp_retry,
)
from tidd_tools.ai_review.size_gate import XXL_LINE_THRESHOLD as _AI_REVIEW_XXL_LINE_THRESHOLD
from tidd_tools.ai_review.timing_steps import measure_step
from tidd_tools.shared.branch_ref import extract_issue_number as _extract_issue_number
from tidd_tools.shared.branch_ref import parse_branch as _parse_branch
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.paths import cache_dir as _shared_cache_dir

# pre-flight 自己記録ファイルのキャッシュパス（Issue #2637・#2936 で書き込み撤去。
# 移行前履歴の読みフォールバック用に Issue 別パスのみ残す）
_PREFLIGHT_RECORD_DIR = "pre-flight"


def _preflight_per_issue_record_path(issue_key: str) -> Path:
    """Issue 別 pre-flight 自己記録ファイルのパスを返す（Issue #2637）.

    `~/.cache/ai-dev-handbook/pre-flight/issue-<N>.jsonl`

    並行 worktree で複数 Issue を同時実行する場合でも記録が混入しない。
    """
    return _shared_cache_dir() / _PREFLIGHT_RECORD_DIR / f"{issue_key}.jsonl"


def _read_last_complete_preflight_record(issue_key: str) -> dict[str, object] | None:
    """直前の pre-flight 完了レコード（`exit_code` を持つ）を返す（Issue #2918）.

    #3295: 統一イベントログ（timing_log）の `step3-preflight-end` レコードを第一ソースとし、
    旧 `pre-flight/issue-N.jsonl` は移行前履歴のフォールバックとして読む。
    どちらにも完全レコードが無い場合は `None` を返す（fail-open）。
    """
    from tidd_tools import timing_log

    try:
        events = timing_log.read_events(issue_key)
    except (OSError, ValueError):
        events = []
    for event in reversed(events):
        if event.get("step") == "step3-preflight-end" and event.get("source") == "pre-flight":
            meta = event.get("meta") or {}
            if meta.get("exit_code") is not None:
                return {"exit_code": meta["exit_code"], "tree_hash": meta.get("tree_hash")}

    path = _preflight_per_issue_record_path(issue_key)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    last: dict[str, object] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("exit_code") is not None:
            last = record
    return last


def _is_repeat_success_skip(issue_key: str | None, tree_hash: str) -> bool:
    """直前の pre-flight 実行と同一 tree hash・exit_code=0 であれば True を返す（Issue #2918）.

    True の場合、呼び出し元はチェックを一切実行せず即座に exit 0 で終了してよい。
    issue_key が解決できない、tree_hash が空、直近の完全レコードが存在しない・読み込みに
    失敗する、tree_hash が不一致、exit_code が非 0 のいずれかであれば False（fail-open）。
    """
    if not issue_key or not tree_hash:
        return False
    record = _read_last_complete_preflight_record(issue_key)
    if record is None:
        return False
    return record.get("tree_hash") == tree_hash and record.get("exit_code") == 0


def _is_feat_or_fix_branch(branch: str) -> bool:
    """ブランチ名が `feat/issue-<N>-*` / `fix/issue-<N>-*` 形式か（Issue #3550）.

    `shared.branch_ref.parse_branch()` は 7 type すべてにマッチするため、
    RED/GREEN ステップを持つ feat/fix のみへの絞り込みは呼び出し側で行う
    （`shared/branch_ref.py` docstring 参照）。
    """
    if not branch:
        return False
    parsed = _parse_branch(branch)
    return parsed is not None and parsed[0] in ("feat", "fix")


# doc-only 判定: 変更ファイルが docs/, *.md, CLAUDE.md のみで構成されるか
#
# この正規表現が doc-only 判定の単一の真実源。配布テンプレート
# `templates/workflow/.pre-commit-config.yaml.jinja` の pytest local hook にも同じ判定を
# 表す `exclude:` 文字列があり（#4170）、値はここと完全一致させること。ずれた場合は
# `tests/regressions/test_fix_4171_doc_only_single_source.py` が CI で検知する（#4171）。
_DOC_ONLY_RE = re.compile(r"^(docs/.*|[^/]*\.md|CLAUDE\.md)$")

# doc-update ゲート（Issue #2580）
# docs 更新を要求する対象パス（いずれかにマッチすれば「実装変更あり」と判定）
_DOC_UPDATE_REQUIRED_RE = re.compile(r"^(projects/py/[^/]+/src/|projects/gas/[^/]+/(?!tests/)|\.claude/hooks/)")
# テスト・regressions のみの変更は対象外（実装変更なしとみなす）
_TESTS_ONLY_RE = re.compile(r"^(projects/[^/]+/[^/]+/tests/|tests/regressions/)")
# docs 側として認めるパス
_DOC_SIDE_RE = re.compile(r"^(docs/|\.claude/rules/|CLAUDE\.md$)")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "pre-flight",
        help="PR 作成前チェック（lint・.feature/step_defs 存在・変更プロジェクトのテスト）をローカル実行する",
        description=__doc__,
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def _should_skip_repeat_success(repo_root: Path, issue_key: str | None, branch: str, tree_hash: str) -> bool:
    """直前と同一 tree hash・exit_code=0 の完全レコードがあれば省略可否を判定する（#2918・#3187 C901 対応）.

    diff-size チェック（Issue #3081）は working tree の内容ではなく
    `git diff --numstat origin/main` に依存するため、tree hash が前回成功時と
    一致していても origin/main が更新されていれば結果が変わりうる。
    impl-delegation 委譲証跡チェック（Issue #3153）も同様に working tree の内容では
    なく calls.jsonl（Issue 単位の外部ログ）に依存するため、tree hash 一致だけでは
    証跡の有無を保証できない。スキップを確定する前に必ず両方を再チェックし、
    いずれかが失敗するならスキップせず通常のチェックフローへフォールバックする
    （diff-size は PR #3088 レビュー指摘・impl-delegation は PR #3171 レビュー指摘）。
    スキップ確定時、working tree が clean であれば SHA マーカー（`preflight-pytest-<sha>.json`）を
    補完する（Issue #3459）。
    """
    if not _is_repeat_success_skip(issue_key, tree_hash):
        return False
    skip_probe_failures: list[str] = []
    _check_diff_size(repo_root, skip_probe_failures)
    _check_impl_delegation_evidence(branch, issue_key, skip_probe_failures)
    if not skip_probe_failures:
        print(
            "==> pre-flight: 直前と同一内容（tree hash 一致）で GREEN 確認済みのためチェックを省略します (#2918)",
            file=sys.stderr,
        )
        # Issue #3459: repeat-success-skip 経路では SHA マーカーが書かれず、
        # require-preflight-marker.py（push gate）が HEAD SHA マーカーを要求して
        # push を誤ブロックしていた。スキップ確定時、clean tree なら SHA マーカーを補完する。
        if _is_working_tree_clean(repo_root):
            head_sha = preflight_markers._git_head_sha(repo_root)
            if head_sha:
                preflight_markers.write_preflight_pytest_marker(repo_root, head_sha)
        return True
    print(
        "==> pre-flight: tree hash は一致していますが、"
        f"{'/'.join(skip_probe_failures)} チェックが失敗したためスキップせず通常のチェックを実行します "
        "(#3081 / #3153)",
        file=sys.stderr,
    )
    return False


def _handle_diff_error(started_at: str, issue_key: str | None, tree_hash: str) -> int:
    """`git diff --name-only origin/main` 失敗時の記録・exit code を返す（#2756・#3187 C901 対応）."""
    print(
        "ERROR: pre-flight: git diff --name-only origin/main が失敗しました。チェックを実行できません。",
        file=sys.stderr,
    )
    ended_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if issue_key is not None:
        timing_log.record_event_safe(
            issue_key,
            "step3-preflight-end",
            "end",
            "pre-flight",
            meta={
                "started_at": started_at,
                "ended_at": ended_at,
                "exit_code": 1,
                "tree_hash": tree_hash,
                "failures": ["git diff 失敗"],
                "checks": [],
            },
        )
    return 1


def _handle_no_diff(started_at: str, issue_key: str | None, tree_hash: str) -> int:
    """origin/main との差分がない場合の記録・step3-preflight-end mark・exit code を返す（#3187 C901 対応）."""
    print("==> pre-flight: origin/main との差分がありません。チェックをスキップします。", file=sys.stderr)
    print("pre-flight: all checks passed")
    ended_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if issue_key is not None:
        timing_log.record_event_safe(
            issue_key,
            "step3-preflight-end",
            "end",
            "pre-flight",
            meta={
                "started_at": started_at,
                "ended_at": ended_at,
                "exit_code": 0,
                "tree_hash": tree_hash,
                "skipped": True,
                "checks": [],
            },
        )
    return 0


def _run_static_analysis_checks(
    repo_root: Path,
    changed: list[str],
    branch: str,
    issue_key: str | None,
    timing_id: str | None,
    failures: list[str],
) -> tuple[list[str], int]:
    """pre-flight の静的解析チェック一式（lint〜impl-delegation証跡）を実行する（#3187 C901 対応）.

    `failures` は呼び出し側と共有し、検出した失敗理由を直接追記する。
    戻り値は (実施チェック名一覧, diff 行数)。
    """
    # sha/token を空にすることで can_post=False となり、Commit Status 投稿なしの
    # 純ローカル実行になる（投稿は従来どおり ai-review の _post_test_statuses 経路が担う）。
    from tidd_tools.ai_review.test_statuses import _post_lint_statuses

    # 実際に実行された（スキップされなかった）チェック名を集める（Issue #2859）。
    # `merge-summary report` の「検証・テスト」行の owner 欄に反映するため。
    lint_ran_checks: list[str] = []
    lint_exit = _post_lint_statuses(
        "\n".join(changed), "", "", "", repo_root, 0, timing_id=timing_id, ran_checks=lint_ran_checks
    )
    if lint_exit != 0:
        failures.append("lint (ruff/mypy/gherkin-lint)")

    # Issue #3337: .rulesync/ 配下（正本）に変更があるのに rulesync が未導入だと
    # health-check 側のドリフト検証が WARN skip になり無言で GREEN 扱いされるため、
    # pre-flight で明示的に失敗させる（導入済みなら health-check が検証する）。
    _check_rulesync_drift_required(changed, failures, repo_root=repo_root)

    # context budget gate: _post_lint_statuses の context budget check は failure Commit Status
    # で gate する設計のため exit code を格上げしない。pre-flight には Commit Status gate がないので、
    # HARD 閾値は test_plan.run() と同じ context_budget.run_gate()（#1883）で、
    # WARN 閾値は CI と同じ detect-rule-bloat 判定（#1914）で exit 1 を保証する。
    with measure_step(timing_id, "preflight.context-budget"):
        budget_trigger_hit = any(context_budget.is_gate_trigger(f, repo_root) for f in changed)
        if budget_trigger_hit:
            lint_ran_checks.append("context-budget")
        if budget_trigger_hit and context_budget.run_gate(repo_root) != 0:
            print("ERROR: pre-flight: context budget 超過を検出しました (#1883)", file=sys.stderr)
            failures.append("context-budget")
        elif _context_budget_warn_exceeded(repo_root, changed):
            if "context-budget" not in lint_ran_checks:
                lint_ran_checks.append("context-budget")
            print(
                "ERROR: pre-flight: context budget 超過（WARN 閾値）を検出しました。"
                "CI では context-budget/rules の failure Commit Status となり auto-merge がブロックされます (#1914)",
                file=sys.stderr,
            )
            failures.append("context-budget")

    feature_error = _check_feature_step_defs(repo_root, changed, branch)
    if feature_error:
        print(feature_error, file=sys.stderr)
        failures.append("feature-check")

    # Issue #2578: `tidd docs-sync` の生成結果と commit 済み docs/reference/tidd-cli-reference.md を比較する。
    _check_docs_sync_drift(repo_root, failures)
    # Issue #2580: src 変更に対応する docs 変更チェック
    _check_doc_update_required(repo_root, changed, failures)
    # Issue #2579: hooks.md 一覧表と .claude/hooks/ 実装の drift チェック
    _check_hooks_md_drift(repo_root, failures)
    # Issue #2768: HTML 生成ファイル変更時の RENDERER_VERSION bump チェック
    _check_renderer_version_bump(repo_root, changed, failures)
    # Issue #3059: jscpd による重複コード検出
    _check_jscpd_duplication(repo_root, failures)
    # Issue #3058: vulture による未使用コード検出
    _check_vulture_deadcode(repo_root, failures)
    # Issue #3081: PR diff サイズチェック
    diff_lines = _check_diff_size(repo_root, failures)
    # Issue #3153: impl-delegation 委譲証跡チェック
    _check_impl_delegation_evidence(branch, issue_key, failures)
    # Issue #3439: 依存更新バージョンの公開日 cooldown ゲート
    _check_npm_cooldown(repo_root, changed, failures)
    # Issue #3417: templates/workflow/.claude/** の root との同期漏れチェック（全ファイル走査）
    _check_sync_template(repo_root, failures)
    # Issue #3418: regression テストの命名規則チェック
    _check_regression_test_naming(repo_root, failures)
    # Issue #4039: 配布物内の docs/ パス参照の機械検出
    _check_distributed_docs_references(repo_root, failures)
    # Issue #4077: 未追跡の決定ジャーナル検査（非ブロッキング WARN）
    _check_untracked_decision_journals(repo_root)

    return lint_ran_checks, diff_lines


def _check_rulesync_drift_required(
    changed_files: list[str], failures: list[str], repo_root: Path | None = None
) -> None:
    """rulesync 正本・生成物の変更時にドリフト検証を pre-flight で実効化する（#3337/#3711）.

    `rulesync generate --check` による生成物ドリフト検証は rulesync が解決できないと
    health-check 側で WARN skip されるため、正本（`.rulesync/`・`rulesync.jsonc`）や
    生成物（`AGENTS.md`・`.claude/rules/`）に変更がある PR では未導入のまま通過させない。

    #3711: consumer の `AGENTS.md` は rulesync 生成物になったため、`AGENTS.md` を
    手編集した PR（Gherkin 異常系）は `rulesync generate --check` が差分を検出して
    pre-flight を失敗させる。rulesync 導入済みなら health-check と同じ
    `_check_rulesync_drift` を実行して実際のドリフトを failures に追加する。
    解決方法は health-check と同じ `node_modules/.bin/rulesync` 優先 → PATH フォールバック
    （Issue #3411）。`repo_root` 省略時は `test_plan._resolve_repo_root()` で解決する。
    """
    from tidd_tools.health_check import _check_rulesync_drift, _resolve_rulesync_bin

    # .claude/rules/*.yaml 等の rulesync 非管理ファイルまで trigger に含めると
    # rulesync 未導入環境で偽陽性になるため、正本（.rulesync/）と生成物 AGENTS.md のみ対象。
    if not any(f.startswith(".rulesync/") or f == "rulesync.jsonc" or f == "AGENTS.md" for f in changed_files):
        return
    if repo_root is None:
        repo_root = test_plan._resolve_repo_root()
    if _resolve_rulesync_bin(repo_root):
        # rulesync 導入済み: 実際のドリフト検証を実行する（health-check と同じ契約・#3711）
        failures.extend(_check_rulesync_drift(repo_root))
        return
    print(
        "ERROR: pre-flight: rulesync 正本/生成物（.rulesync/・AGENTS.md 等）に変更がありますが"
        " rulesync が未インストールのためドリフト検証を実行できません（`npm ci` を実行して"
        " rulesync を導入し `rulesync generate` で生成物を更新してください・#3337）",
        file=sys.stderr,
    )
    failures.append("rulesync-drift (rulesync 未インストール)")


_DOC_ONLY_SKIP_REASON = "doc-only"
_STATIC_CHECK_FAIL_SKIP_REASON = "static-check-failed"  # Issue #3453: 静的チェック失敗による pytest fail-fast skip

# Issue #3468: 「pytest 未実行だが pre-flight は成功」を意味する skip 理由の一覧。
# ここに含まれる理由で pre-flight が exit 0 のときは push gate 用マーカーを書く。
# 除外している理由: "マーカー hit"（既存マーカーがあるため書き直し不要）・
# `_STATIC_CHECK_FAIL_SKIP_REASON`（失敗確定時は書かない・呼び出し元で failures 判定済み）。
_PYTEST_NOT_RUN_SKIP_REASONS = frozenset(
    {
        _DOC_ONLY_SKIP_REASON,
        "py プロジェクト未検出",
        "pyproject.toml 未検出",
    }
)


def _write_skipped_pytest_markers(repo_root: Path, reason: str) -> None:
    """pytest 未実行のまま pre-flight が成功したことをマーカーに記録する（Issue #3458・#3468）.

    push gate（`require-preflight-marker.py`）は「HEAD SHA に成功マーカーがあること」を
    要求するため、doc-only や py プロジェクト未検出等でマーカーを書かないと push が
    永久にブロックされる。`pytest_skipped_reason` 付きのマーカーは pytest キャッシュ
    としては hit しない。
    """
    head_sha = preflight_markers._git_head_sha(repo_root)
    tree_hash = preflight_markers._git_tree_hash(repo_root)
    if tree_hash:
        preflight_markers.write_preflight_pytest_tree_marker(
            repo_root, tree_hash, head_sha, pytest_skipped_reason=reason
        )
    if _is_working_tree_clean(repo_root) and head_sha:
        preflight_markers.write_preflight_pytest_marker(repo_root, head_sha, pytest_skipped_reason=reason)


@dataclasses.dataclass(frozen=True)
class _PytestJestOutcome:
    """`_run_pytest_and_jest` の実行結果（#3187 C901 対応）."""

    pytest_ran: bool  # pytest が実際に実行されて成功したか
    # pytest プロセスが実際に起動されたか（成功/失敗問わず）。`checks` フィールドは実施有無のみを
    # 表すため、失敗して SystemExit した場合も True にする必要がある（PR #2869 レビュー指摘）。
    pytest_invoked: bool
    pytest_skip_reason: str | None  # pytest 未実行の理由（None = 実行済みまたは対象外）
    pytest_marker_path: str | None  # マーカー hit 時のマーカーファイル相対パス（Issue #2800）
    pytest_marker_written_at: str | None  # マーカー hit 時のマーカーファイル書き込み時刻（Issue #2800）
    jest_ran: bool  # Jest が実際に実行対象になったか（Issue #2859・checks フィールド用）
    # `tmp_retry.diagnose_with_bounded_retry()` の exit_code（`EXIT_OK` 以外のみ設定）。
    # `run_cli` が一時領域不足による回復不能（`tmp_retry.EXIT_UNRECOVERABLE`）を通常の
    # 失敗（exit 1）と区別して `tidd pre-flight` プロセス自体の exit code へ伝播するために使う
    # （Issue #4149・レビュー指摘: PR #4151）。
    tmp_retry_exit_code: int | None = None


def _run_pytest_and_jest(
    repo_root: Path,
    changed: list[str],
    timing_id: str | None,
    failures: list[str],
    *,
    issue_key: str | None = None,
) -> _PytestJestOutcome:
    """変更プロジェクトの pytest / Jest を実行し tree hash マーカーを書く（#3187 C901 対応）.

    pytest 実行結果の追跡は Issue #2778（JSONL への checks/pytest_skip_reason 記録）のため。
    `issue_key`（Issue #4149）は unattended 判定（`issue_next_state.is_unattended_for_issue`）
    にのみ使う。
    """
    if _is_doc_only(changed):
        print("preflight.pytest: skipped (doc-only)", flush=True)
        print("preflight.jest: skipped (doc-only)", flush=True)
        return _PytestJestOutcome(False, False, _DOC_ONLY_SKIP_REASON, None, None, False)

    ctx = test_plan.RunContext(
        pr_num="pre-flight",
        repo=None,
        repo_root=repo_root,
        state_dir=None,
        dry_run=True,
        json_output=False,
    )
    py_projects = test_plan._detect_projects(changed, "projects/py/")
    gas_projects = test_plan._detect_projects(changed, "projects/gas/")
    jest_ran = bool(gas_projects)
    _cleanup_pytest_tmpdir()

    # Issue #4143: 実装と無関係な一時領域枯渇（tmpfs の容量・inode 不足）が pytest の
    # `OSError: [Errno 28] No space left on device` としてテスト失敗に紛れ込み
    # `classify-test-failure` が判定不能になる問題を防ぐため、重い pytest を起動する前に
    # 診断する。診断が失敗（inode 不足・repo-local フォールバックも容量不足）した場合は
    # pytest / Jest を起動せず即座に failures へ理由を積む（1〜4 分の無駄な実行を避ける）。
    # py/GAS いずれのプロジェクトにも該当しない変更（例: `.github/workflows/ci.yml` のみ）は
    # `_run_pytest`/`_run_jest` がそもそも起動しないため、診断自体もスキップする
    # （無関係な変更まで一時領域不足で pre-flight を落とさないため。レビュー指摘: PR #4146）。
    # ただし `.claude/`・`templates/`・`.rulesync/` 変更は `py_projects` が空でも
    # `test_plan._run_pytest` 内の `_maybe_run_dist_check` 経由で配布物検証テスト（slow
    # 込み・重い）を起動しうるため、その経路も診断対象に含める（レビュー指摘: PR #4146）。
    if py_projects or gas_projects or test_plan._dist_check_triggered(changed):
        # Issue #4149: TMPDIR 切り替え（#4143）・stale artifact 掃除（#4144）を尽くしても
        # 回復できない場合、unattended 実行時のみ bounded retry + backoff を行い、上限到達
        # 後も needs-human-input を付与せず再選定可能にする契約（exit code 3 + JSON）を
        # `tmp_retry` に委譲する。attended 実行時は従来どおり 1 回限りの掃除・再診断のみ。
        _unattended = issue_next_state.is_unattended_for_issue(_extract_issue_number(issue_key) if issue_key else None)
        _retry_outcome = tmp_retry.diagnose_with_bounded_retry(unattended=_unattended, stderr=sys.stderr)
        if _retry_outcome.exit_code != tmp_retry.EXIT_OK:
            failures.append(f"一時領域診断 ({_retry_outcome.diagnosis.reason})")
            return _PytestJestOutcome(
                False, False, "一時領域不足", None, None, False, tmp_retry_exit_code=_retry_outcome.exit_code
            )

    pytest_ran = False
    pytest_invoked = False
    pytest_skip_reason: str | None = None
    pytest_marker_path: str | None = None
    pytest_marker_written_at: str | None = None
    try:
        with measure_step(timing_id, "preflight.pytest"):
            outcome = test_plan._run_pytest(ctx, changed, [])
        pytest_ran = outcome.ran
        pytest_invoked = outcome.ran
        pytest_skip_reason = outcome.skip_reason
        pytest_marker_path = outcome.marker_path
        pytest_marker_written_at = outcome.marker_written_at
    except SystemExit:
        failures.append(f"pytest ({', '.join(py_projects)})")
        # `_run_pytest()` が SystemExit を投げるのは pytest プロセスを起動した後の失敗
        # （テスト失敗・タイムアウト）経路のみ（py プロジェクト未検出等の skip 経路は
        # SystemExit を投げず PytestOutcome を返す）。よって pytest は実施済みとして扱う。
        pytest_invoked = True

    # pytest が「実際に実行されて成功」した場合のみ tree hash マーカーを書く（Issue #2778）。
    # マーカー hit・py プロジェクト未検出などの skip 経路ではマーカーを書かない。
    # 理由: 未実行のまま「成功済み」マーカーを書くと、ai-review が後続の pytest を
    # スキップしてしまう（Issue #2778 原因）。
    if pytest_ran:
        head_sha = preflight_markers._git_head_sha(repo_root)
        tree_hash = preflight_markers._git_tree_hash(repo_root)
        if tree_hash:
            preflight_markers.write_preflight_pytest_tree_marker(repo_root, tree_hash, head_sha)
        # 従来の SHA マーカーは clean tree のときのみ書く（Issue #2311）。dirty tree で
        # 書くと HEAD SHA が実テスト対象（未コミットの変更を含む内容）と異なる commit を
        # 指してしまい、無関係な別の dirty 変更を誤って cache hit させる恐れがある。
        if _is_working_tree_clean(repo_root) and head_sha:
            preflight_markers.write_preflight_pytest_marker(repo_root, head_sha)
    # Issue #3504: `_run_pytest` の tree-hash marker hit による skip 経路では SHA マーカーが
    # 書かれず、push gate（require-preflight-marker.py）が HEAD SHA マーカーを要求して
    # push を誤ブロックする。典型例: dirty tree で pytest を実行して tree marker のみ書かれた後、
    # commit して clean になった状態で再度 pre-flight を実行するケース。skip 理由が「マーカー hit」で
    # clean tree なら SHA マーカーを補完する（_should_skip_repeat_success の #3459 修正と同パターン）。
    # SHA marker hit 経路は既存マーカーがあるため補完は実質 no-op だが、clean tree 条件を守れば安全。
    elif pytest_skip_reason == "マーカー hit" and _is_working_tree_clean(repo_root):
        head_sha = preflight_markers._git_head_sha(repo_root)
        if head_sha:
            preflight_markers.write_preflight_pytest_marker(repo_root, head_sha)

    try:
        with measure_step(timing_id, "preflight.jest"):
            test_plan._run_jest(ctx, changed, [])
    except SystemExit:
        failures.append(f"Jest ({', '.join(gas_projects)})")

    return _PytestJestOutcome(
        pytest_ran, pytest_invoked, pytest_skip_reason, pytest_marker_path, pytest_marker_written_at, jest_ran
    )


def run_cli(args: argparse.Namespace) -> int:  # noqa: ARG001  # 共通フラグのみで固有引数なし
    _started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    repo_root = test_plan._resolve_repo_root()

    # ブランチ名から issue 番号を解決する（Issue #2522）。
    # ブランチ名が feat/issue-<N>-* 形式のときのみ解決する。解決できない場合は silent skip。
    branch = _current_branch(repo_root)
    issue_key = _resolve_issue_key_from_branch(branch)

    # Issue #4074: [AI確認] 項目を PR 作成前に stdout へ提示する（レビュー結果に依存しない
    # 項目は APPROVE を待たずに検証できるようにするため）。gh 取得失敗時も警告のみで
    # 他のチェック結果には影響しない（failures には追加しない）。
    # #4075 レビュー指摘: repeat-success-skip 経路（直後の early return）は tree hash が
    # 前回成功時と同一かどうかのみで判定するため、Issue/PR 側の [AI確認] 未消化状態が
    # コード変更を伴わずに残っていても検知されない。skip 判定より前に必ず実行する。
    _check_ai_confirm_items(repo_root, branch, issue_key)

    # 直前と同一 tree hash・exit_code=0 の完全レコードがあれば、チェックを一切実行せず
    # 即座に exit 0 で終了する（同一内容に対する重複実行の防止・Issue #2918）。
    # 新規の JSONL レコードは書き込まない（merge-summary の「検証・テスト」行が
    # 重複ラウンドとして描画されないようにするため）。
    _current_tree_hash = preflight_markers._git_tree_hash(repo_root)
    if _should_skip_repeat_success(repo_root, issue_key, branch, _current_tree_hash):
        print("pre-flight: all checks passed")
        return 0

    if issue_key is not None:
        # 統一イベントログへ記録する（Issue #2934 やること1項目目・#3340）。
        timing_log.record_event_safe(
            issue_key, "step3-preflight-start", "start", "pre-flight", meta={"started_at": _started_at}
        )

    changed = _changed_files(repo_root)

    # git diff 失敗: None は「差分なし」ではなく「git エラー」（Issue #2756）
    if changed is None:
        return _handle_diff_error(_started_at, issue_key, _current_tree_hash)

    if not changed:
        return _handle_no_diff(_started_at, issue_key, _current_tree_hash)

    print(f"==> pre-flight: 変更ファイル {len(changed)} 件をチェックします", file=sys.stderr)

    failures: list[str] = []

    # timing_id: ai-review-timing（#2100）on 時、pre-flight 各ステップを記録する識別子
    # （PR がまだ存在しないためブランチ名を使う。#2101）。ブランチ名を解決できない場合は
    # measure_step 側で記録をスキップする。
    timing_id = branch or None

    _lint_ran_checks, _diff_lines = _run_static_analysis_checks(
        repo_root, changed, branch, issue_key, timing_id, failures
    )
    # 静的チェックの失敗が確定した時点で pytest / Jest（1〜4分）を実行せず fail-fast する
    # （Issue #3453）。静的チェックは数秒で終わるため、この失敗が判明した後に pytest を
    # 待たされるのは実測で無駄と特定できている（17 回 / 21.5 分）。
    if failures:
        print("pre-flight: 静的チェック失敗のため pytest / Jest をスキップしました", file=sys.stderr)
        _pytest_jest = _PytestJestOutcome(False, False, _STATIC_CHECK_FAIL_SKIP_REASON, None, None, False)
    else:
        _pytest_jest = _run_pytest_and_jest(repo_root, changed, timing_id, failures, issue_key=issue_key)

    if failures:
        print("pre-flight: FAILED — " + " / ".join(failures), file=sys.stderr)
        # Issue #4149: unattended 実行時に一時領域不足の再試行上限へ到達し回復不能と
        # 判定された（`tmp_retry.EXIT_UNRECOVERABLE`）場合、`needs-human-input` を要する
        # 通常の失敗（exit 1）に丸めず `tidd pre-flight` プロセス自体の exit code へ
        # そのまま伝播する。伝播しないと呼び出し元（issue-next skill 等）が
        # skip-and-later-reselect 契約を検知できない（レビュー指摘: PR #4151）。
        if _pytest_jest.tmp_retry_exit_code == tmp_retry.EXIT_UNRECOVERABLE:
            _exit_code = tmp_retry.EXIT_UNRECOVERABLE
        else:
            _exit_code = 1
    else:
        print("pre-flight: all checks passed")
        _exit_code = 0
        if _pytest_jest.pytest_skip_reason in _PYTEST_NOT_RUN_SKIP_REASONS:
            _write_skipped_pytest_markers(repo_root, _pytest_jest.pytest_skip_reason)

    _ended_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # 実行チェック一覧（旧 legacy 記録の checks 相当・#2859）: pytest → jest → lint の順
    _checks: list[str] = []
    if _pytest_jest.pytest_invoked:
        _checks.append("pytest")
    if _pytest_jest.jest_ran:
        _checks.append("jest")
    for _c in _lint_ran_checks:
        if _c not in _checks:
            _checks.append(_c)
    # step3-preflight-end を自動記録する（Issue #2522）
    if issue_key is not None:
        # 統一イベントログへ記録する（Issue #2934 やること1項目目・#3340）。
        timing_log.record_event_safe(
            issue_key,
            "step3-preflight-end",
            "end",
            "pre-flight",
            meta={
                "started_at": _started_at,
                "ended_at": _ended_at,
                "exit_code": _exit_code,
                "tree_hash": _current_tree_hash,
                "diff_lines": _diff_lines,
                "failures": failures if failures else None,
                "checks": _checks,
                "pytest_skip_reason": _pytest_jest.pytest_skip_reason,
                "marker_path": _pytest_jest.pytest_marker_path,
                "marker_written_at": _pytest_jest.pytest_marker_written_at,
            },
        )
    return _exit_code


def _cleanup_pytest_tmpdir(
    root: Path | None = None, *, threshold: int | None = None, stderr: TextIO | None = None
) -> int:
    """pytest 起動前に一時ディレクトリの総容量ガードを走らせる（Issue #2834）.

    掃除は best-effort。失敗しても pre-flight 本体を止めないよう例外を握りつぶし、
    常に 0 を返す。
    """
    try:
        pytest_tmpdir.cleanup(root, threshold=threshold, stderr=stderr)
    except OSError as exc:
        print(f"WARN: pytest 一時ディレクトリの掃除をスキップしました: {exc}", file=stderr or sys.stderr)
    return 0


def _is_doc_only(changed: list[str]) -> bool:
    """変更ファイルが docs/・*.md・CLAUDE.md のみで構成されているか判定する.

    True の場合、preflight.pytest / preflight.jest をスキップする（Issue #2186）。
    """
    return bool(changed) and all(_DOC_ONLY_RE.match(f) for f in changed)


def _changed_files(repo_root: Path) -> list[str] | None:
    """origin/main との差分ファイル一覧を返す（未コミットの変更も含む）.

    git diff が失敗した場合（非ゼロ終了・FileNotFoundError・タイムアウト）は None を返す。
    呼び出し元は None を受け取った場合、差分なしと解釈せず失敗として処理すること（Issue #2756）。
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "origin/main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _is_working_tree_clean(repo_root: Path) -> bool:
    """未コミットの変更（staged/unstaged/untracked）がないか判定する（Issue #2311）."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and not proc.stdout.strip()


def _context_budget_warn_exceeded(repo_root: Path, changed: list[str]) -> bool:
    """detect-rule-bloat.py の WARN「context budget 超過」を CI と同じ判定で検出する.

    `ai_review.test_statuses._post_lint_statuses` は WARN 超過で context-budget/rules の
    failure Commit Status を投稿して auto-merge をブロックする（#1914・#831）。
    pre-flight で同判定を再現しないと WARN 超過が CI で初めて赤になるため、
    対象ファイル・anchor 解決・超過判定文字列を core と同一にしている。
    """
    budget_targets = [f for f in changed if f == "CLAUDE.md" or re.match(r"^\.claude/rules/[^/]+\.md$", f)]
    bloat_hook = repo_root / ".claude" / "hooks" / "detect-rule-bloat.py"
    if not budget_targets or not bloat_hook.is_file():
        return False
    anchor = next(
        (repo_root / f for f in budget_targets if (repo_root / f).is_file()),
        repo_root / "CLAUDE.md",
    )
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(anchor)}})
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, str(bloat_hook)],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"WARN: pre-flight: detect-rule-bloat.py の実行に失敗しました: {exc}", file=sys.stderr)
        return False
    return proc.returncode != 0 or "context budget 超過" in (proc.stdout + proc.stderr)


def _current_branch(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _resolve_issue_key_from_branch(branch: str) -> str | None:
    """ブランチ名から `issue-<N>` キーを解決する（Issue #2522・#3551）.

    `docs/conventions.md` の 7 type（feat/fix/docs/refactor/build/ci/research）いずれかの
    `<type>/issue-<N>-*` 形式のブランチ名から解決する（#3551 で feat/fix 限定を撤廃）。
    `.feature` 必須チェック（`_check_feature_step_defs()`）は feat/fix のみに絞り込む
    別述語 `_is_feat_or_fix_branch()` を使うため、本関数の対象拡大の影響を受けない。
    解決できない場合（非 issue ブランチ・ブランチ名未取得等）は None を返す。
    """
    if not branch:
        return None
    parsed = _parse_branch(branch)
    if parsed is None:
        return None
    return f"issue-{parsed[1]}"


def _check_feature_step_defs(repo_root: Path, changed_files: list[str], branch: str) -> str | None:
    """feat/fix ブランチの `.feature` + step_defs 存在チェック（PR 作成前版）.

    `test_plan._check_feature_file_required`（#1464/#1550/#1855/#1962）と同基準だが、
    PR が存在しないため type とIssue 番号はブランチ名（`<type>/issue-<N>-<slug>`）から導出する。

    Returns:
        `None` : チェック対象外 or カバレッジ十分
        エラー文字列: `.feature` / step_defs が不足している（`tidd extract-feature` を促す）
    """
    if not _is_feat_or_fix_branch(branch):
        return None
    if not test_plan._feature_required_for_files(changed_files):
        return None

    # hook 契約系（REQUIRED ファイルが全て .claude/hooks/）は .feature 不要（#1855）
    required_files = [f for f in changed_files if test_plan._FEATURE_REQUIRED_PATH_RE.match(f)]
    if required_files and all(test_plan._HOOK_PATH_RE.match(f) for f in required_files):
        return None

    issue_num_opt = _extract_issue_number(branch)
    if issue_num_opt is None:
        # Issue 番号を特定できない場合は PR 側 gate（tidd test-plan）に委ねる
        return None
    issue_num = str(issue_num_opt)

    # Issue #4078: 変更ファイルから対象 project（複数可）を解決する。いずれか 1 つで
    # .feature + step_defs が揃えば通過させる（旧: tidd_tools 固定）。
    candidate_projects = test_plan._resolve_projects_for_changed_files(changed_files)
    for project in candidate_projects:
        feature_path = repo_root / "projects" / "py" / project / "tests" / "features" / f"issue-{issue_num}.feature"
        step_defs_path = repo_root / "projects" / "py" / project / "tests" / "step_defs" / f"test_issue_{issue_num}.py"
        if feature_path.is_file() and step_defs_path.is_file():
            return None

    # tests/test_*.py が変更に含まれれば .feature 不要（#1962）
    if any(test_plan._TESTS_TEST_PY_RE.match(f) for f in changed_files):
        return None

    primary_project = candidate_projects[0]
    return (
        f"ERROR: pre-flight: feat/fix ブランチに .feature / step_defs が揃っていません (Issue #1997)\n"
        f"       期待パス: projects/py/{primary_project}/tests/features/issue-{issue_num}.feature\n"
        f"                 projects/py/{primary_project}/tests/step_defs/test_issue_{issue_num}.py\n"
        f"       生成コマンド: `tidd extract-feature {issue_num}`\n"
        f"       または PR 変更ファイルに projects/py/<project>/tests/test_*.py を含めてください (Issue #1962)\n"
    )


def _check_docs_sync_drift(repo_root: Path, failures: list[str]) -> None:
    """CLI リファレンス drift 検知チェック（Issue #2578）.

    `tidd docs-sync` の再生成結果と commit 済み `docs/reference/tidd-cli-reference.md` を比較する。
    不一致の場合は stderr にメッセージを出力し failures に追加する。

    リファレンスファイルが存在しない場合: このリポジトリが tidd_tools 開発リポジトリでない
    環境（テスト用 tmp_path / consumer リポジトリ等）では drift チェックをスキップする。
    存在確認には `projects/py/tidd_tools/` ディレクトリの有無を用いる。

    vendor 配布（Issue #3979）で consumer にも `projects/py/tidd_tools/pyproject.toml`
    が配置されるようになったため、pyproject.toml の存在だけでは tidd_tools 開発
    リポジトリと区別できない。`_check_hooks_md_drift()`（Issue #2579）と同様に、
    チェック対象ファイル自体（`docs/reference/tidd-cli-reference.md`）の存在確認も
    追加し、consumer では drift チェックをスキップする（PR #3985 レビュー指摘）。

    生成自体が失敗（例外）した場合はサイレントにスキップする（fail-safe）。
    """
    from tidd_tools import docs_sync

    reference_path = repo_root / docs_sync._DEFAULT_OUTPUT_RELPATH
    # tidd_tools 開発リポジトリでなければ drift チェックをスキップ（テスト環境対応）。
    # pyproject.toml の存在を確認することで `_create_feature_and_step_defs` が作る
    # tests/features/ ディレクトリだけが存在する tmp_path 環境と区別する。
    tidd_tools_pyproject = repo_root / "projects" / "py" / "tidd_tools" / "pyproject.toml"
    if not tidd_tools_pyproject.is_file():
        return
    if not reference_path.is_file():
        return  # vendor 配布 consumer 等、リファレンス doc を持たない環境はスキップ

    try:
        has_drift = docs_sync.check_drift(reference_path)
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARN: pre-flight: CLI リファレンス drift チェックの実行に失敗しました: {exc}",
            file=sys.stderr,
        )
        return

    if has_drift:
        print(
            f"ERROR: pre-flight: {docs_sync._DEFAULT_OUTPUT_RELPATH} が最新の CLI 定義と一致しません。\n"
            "       `tidd docs-sync` を実行して再生成し、コミットしてください。",
            file=sys.stderr,
        )
        failures.append("docs-sync drift")


def _read_commit_message_for_marker(repo_root: Path) -> str:
    """最新コミットメッセージを返す（バイパスマーカー読み取り用・Issue #2580）.

    pre-flight は PR 作成前に実行されるため gh pr view を使えない。
    代わりに HEAD コミットメッセージからマーカーを読む（best-effort）。
    取得失敗時は空文字列を返す。
    """
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%B", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _extract_hook_filenames_from_md(hooks_md_path: Path) -> set[str]:
    """hooks.md の hook 一覧（早見表）テーブルから hook ファイル名を抽出する.

    テーブル行の `` [`xxx.py`](#...) `` パターンから `.py` ファイル名のみを抽出する。
    セクション見出しや通常テキスト中の `.py` 参照は除外する（角括弧 + バッククォート形式限定）。
    """
    # テーブルのリンク形式 [`xxx.py`](#anchor) のみ抽出
    md_hook_link_re = re.compile(r"\[`([a-z][a-zA-Z0-9_-]+\.py)`\]")
    result: set[str] = set()
    try:
        text = hooks_md_path.read_text(encoding="utf-8")
    except OSError:
        return result
    for match in md_hook_link_re.finditer(text):
        result.add(match.group(1))
    return result


def _collect_hook_files(hooks_dir: Path) -> set[str]:
    """hooks_dir 内の hook 本体ファイル名を集合で返す.

    hook 本体の判定基準（#2579）:
    - ``hooks_dir`` 直下の ``*.py`` ファイル
    - ``_lib/`` 配下は除外（ヘルパーモジュール）
    - ファイル名が ``_`` で始まるものは除外（内部ユーティリティ）

    この基準で誤分類が出る場合は ``_HOOKS_HELPER_EXPLICIT_EXCLUDE`` に
    ファイル名を追加してマニフェストで除外する。
    """
    if not hooks_dir.is_dir():
        return set()
    result: set[str] = set()
    for p in hooks_dir.iterdir():
        if p.is_dir():
            continue  # _lib/ などのサブディレクトリは除外
        if p.suffix != ".py":
            continue
        if p.name.startswith("_"):
            continue  # _ で始まるファイルは内部ユーティリティとみなす
        result.add(p.name)
    return result


def _check_hooks_md_drift_for_paths(
    hooks_dir: Path,
    hooks_md_path: Path,
    failures: list[str],
) -> None:
    """hooks_dir と hooks_md_path を直接受け取って drift チェックを実行する（テスト用エントリポイント）.

    通常は ``_check_hooks_md_drift(repo_root, failures)`` 経由で呼ばれる。
    パスを外部から注入できるため、テストで任意の fixtures を渡せる。
    """
    doc_hooks = _extract_hook_filenames_from_md(hooks_md_path)
    impl_hooks = _collect_hook_files(hooks_dir)

    missing_in_doc = impl_hooks - doc_hooks  # 実装あり・doc なし（未記載）
    excess_in_doc = doc_hooks - impl_hooks  # doc あり・実装なし（過剰）

    if not missing_in_doc and not excess_in_doc:
        return  # drift なし → PASS

    if missing_in_doc:
        print(
            "ERROR: pre-flight: hooks.md の一覧表に未記載の hook 本体があります (#2579)\n"
            "       docs/reference/hooks.md の「hook 一覧（早見表）」テーブルに追記してください:\n"
            + "\n".join(f"         - {name}" for name in sorted(missing_in_doc)),
            file=sys.stderr,
        )
    if excess_in_doc:
        print(
            "ERROR: pre-flight: hooks.md の一覧表に実装ファイルが存在しない hook が含まれています (#2579)\n"
            "       実装ファイルを追加するか、一覧表のエントリを削除してください:\n"
            + "\n".join(f"         - {name}" for name in sorted(excess_in_doc)),
            file=sys.stderr,
        )
    failures.append("hooks-md drift")


def _check_hooks_md_drift(repo_root: Path, failures: list[str]) -> None:
    """hooks.md 一覧表と .claude/hooks/ 実装の drift チェック（Issue #2579）.

    tidd_tools 開発リポジトリでなければスキップ（`projects/py/tidd_tools/pyproject.toml`
    の存在確認で判定）。

    hook 本体の判定基準:
    - ``.claude/hooks/`` 直下の ``*.py``
    - ``_lib/`` 配下・``_`` 始まりのファイルは除外
    """
    tidd_tools_pyproject = repo_root / "projects" / "py" / "tidd_tools" / "pyproject.toml"
    if not tidd_tools_pyproject.is_file():
        return  # handbook 外環境はスキップ

    hooks_dir = repo_root / ".claude" / "hooks"
    hooks_md_path = repo_root / "docs" / "reference" / "hooks.md"
    if not hooks_md_path.is_file():
        return  # hooks.md が存在しない環境はスキップ

    _check_hooks_md_drift_for_paths(hooks_dir, hooks_md_path, failures)


def _check_doc_update_required(repo_root: Path, changed_files: list[str], failures: list[str]) -> None:
    """src 変更に対応する docs 変更がなければ failures に追加する（Issue #2580）.

    対象パス（いずれかに変更があれば docs 更新を要求）:
    - ``projects/py/<proj>/src/`` 配下
    - ``projects/gas/<proj>/`` 配下（tests/ 除く）
    - ``.claude/hooks/`` 配下

    例外（テストのみ・regressions のみの変更は要求しない）:
    - ``projects/*/*/tests/`` 配下のみの変更
    - ``tests/regressions/`` 配下のみの変更

    docs 側として認めるパス:
    - ``docs/`` 配下
    - ``.claude/rules/`` 配下
    - ``CLAUDE.md``

    バイパス: コミットメッセージに ``<!-- no-doc-update: <理由> -->`` があれば PASS。
    理由が空の場合はバイパス無効。
    """
    # 実装変更ファイルのみ抽出（テスト・regressions は除外）
    impl_files = [f for f in changed_files if _DOC_UPDATE_REQUIRED_RE.match(f) and not _TESTS_ONLY_RE.match(f)]
    if not impl_files:
        return  # 対象パスの変更なし → チェック不要

    # docs 側の変更が存在するか
    has_doc_change = any(_DOC_SIDE_RE.match(f) for f in changed_files)
    if has_doc_change:
        return  # docs 変更あり → PASS

    # バイパスマーカー確認（コミットメッセージから読む）
    commit_msg = _read_commit_message_for_marker(repo_root)
    # 理由必須: <!-- no-doc-update: <理由> --> の形式のみ有効
    # re.DOTALL を使わず 1 行内でマッチ（空理由が後続コメントを飲み込むバグ防止）
    bypass_re = re.compile(r"<!--\s*no-doc-update\s*:\s*((?:(?!-->).)+?)\s*-->")
    m = bypass_re.search(commit_msg)
    if m and m.group(1).strip():
        return  # 理由ありバイパスマーカー → PASS

    # 失敗: stderr に変更ファイル一覧とバイパスマーカー案内を出力
    files_list = "\n".join(f"         - {f}" for f in impl_files)
    print(
        f"ERROR: pre-flight: src 変更に対応する docs/ または .claude/rules/ の変更がありません (#2580)\n"
        f"       変更対象ファイル:\n{files_list}\n"
        f"       docs/ または .claude/rules/ を更新するか、docs 更新不要な理由を\n"
        f"       コミットメッセージに記載してください:\n"
        f"         <!-- no-doc-update: <理由> -->",
        file=sys.stderr,
    )
    failures.append("doc-update")


# ── RENDERER_VERSION bump チェック（Issue #2768）───────────────────────────

# HTML 生成に関わるファイル（変更が検出されたら RENDERER_VERSION の bump を要求する）
_RENDERER_VERSION_TRIGGER_FILES: frozenset[str] = frozenset(
    [
        "projects/py/publish/src/publish/core.py",
        "scripts/publish.py",
    ]
)

# RENDERER_VERSION 定数を抽出する正規表現
_RENDERER_VERSION_RE = re.compile(r"^RENDERER_VERSION\s*:\s*int\s*=\s*(\d+)", re.MULTILINE)


def _get_previous_renderer_version(repo_root: Path) -> int | None:
    """git show origin/main:projects/py/publish/src/publish/core.py から旧 RENDERER_VERSION を取得する.

    changed_files が origin/main 差分で検出されるのと同様に、比較元も origin/main から読む。
    取得できない（新規ファイル・git エラー等）場合は None を返す。
    """
    try:
        proc = subprocess.run(
            ["git", "show", "origin/main:projects/py/publish/src/publish/core.py"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    m = _RENDERER_VERSION_RE.search(proc.stdout)
    if not m:
        return None
    return int(m.group(1))


def _get_current_renderer_version(repo_root: Path) -> int | None:
    """現在の core.py から RENDERER_VERSION を取得する.

    取得できない場合は None を返す。
    """
    core_py = repo_root / "projects" / "py" / "publish" / "src" / "publish" / "core.py"
    try:
        text = core_py.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _RENDERER_VERSION_RE.search(text)
    if not m:
        return None
    return int(m.group(1))


def _check_renderer_version_bump(repo_root: Path, changed_files: list[str], failures: list[str]) -> None:
    """HTML 生成ファイルの変更時に RENDERER_VERSION が bump されているか確認する（Issue #2768）.

    対象ファイル（_RENDERER_VERSION_TRIGGER_FILES）のいずれかが変更され、
    かつ RENDERER_VERSION が前コミットから変わっていない場合は exit 1 でブロックする。

    バイパス: コミットメッセージに ``<!-- no-renderer-version-bump: <理由> -->`` があれば PASS。
    """
    # 対象ファイルの変更があるか確認
    triggered = [f for f in changed_files if f in _RENDERER_VERSION_TRIGGER_FILES]
    if not triggered:
        return

    prev_version = _get_previous_renderer_version(repo_root)
    current_version = _get_current_renderer_version(repo_root)

    # 前コミットに RENDERER_VERSION が存在しない（新規導入）場合はスキップ
    if prev_version is None:
        return

    # 現在のバージョンが取得できない場合はスキップ（フェイルセーフ）
    if current_version is None:
        return

    # バージョンが変わっていれば OK
    if current_version != prev_version:
        return

    # バイパスマーカー確認
    commit_msg = _read_commit_message_for_marker(repo_root)
    bypass_re = re.compile(r"<!--\s*no-renderer-version-bump\s*:\s*((?:(?!-->).)+?)\s*-->")
    m = bypass_re.search(commit_msg)
    if m and m.group(1).strip():
        return

    # 失敗: stderr に bump を促すメッセージを出力
    files_list = "\n".join(f"         - {f}" for f in triggered)
    print(
        f"ERROR: pre-flight: HTML 生成ファイルが変更されましたが RENDERER_VERSION が bump されていません (#2768)\n"
        f"       変更ファイル:\n{files_list}\n"
        f"       projects/py/publish/src/publish/core.py の RENDERER_VERSION を 1 増やしてください。\n"
        f"       HTML 出力に影響しないコメント修正等であれば以下でバイパスできます:\n"
        f"         <!-- no-renderer-version-bump: <理由> -->",
        file=sys.stderr,
    )
    failures.append("renderer-version")


# ── jscpd による重複コード検出（Issue #3059）───────────────────────────────


def _check_jscpd_duplication(repo_root: Path, failures: list[str]) -> None:
    """jscpd で `projects/py/*/src` のコピペ重複を検出する（Issue #3059）.

    `.jscpd.json`（threshold 1%・minTokens 70・対象 `projects/py/*/src`）を使い、
    threshold 超過（非 0 exit）であれば failures に追加する。

    gherkin-lint（Issue #1899）と同じ理由で `npx --yes` は使わない
    （PR のたびに npm レジストリへアクセスしレジストリ障害で silent 失敗するため）。
    ローカルインストール済みの `node_modules/.bin/jscpd` を直接実行する。

    `.jscpd.json` が存在しない環境（handbook 外の consumer リポジトリ等）は
    チェック対象外としてスキップする。`node_modules/.bin/jscpd` が見つからない
    場合（`npm ci` 未実行）は WARN を出して skip する（gherkin-lint と同基準）。
    """
    jscpd_config = repo_root / ".jscpd.json"
    if not jscpd_config.is_file():
        return  # handbook 外環境はスキップ

    jscpd_bin = repo_root / "node_modules" / ".bin" / "jscpd"
    if not jscpd_bin.is_file():
        print(
            "WARN: node_modules/.bin/jscpd が見つかりません。"
            "リポジトリルートで `npm ci` を実行してください（jscpd チェックを skip・Issue #3059）",
            file=sys.stderr,
        )
        return

    print("==> jscpd 重複コードチェックを実行します...", file=sys.stderr)
    try:
        proc = subprocess.run(  # noqa: S603
            [str(jscpd_bin), "--config", str(jscpd_config)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"WARN: pre-flight: jscpd の実行に失敗しました: {exc}", file=sys.stderr)
        return

    if proc.returncode == 0:
        print("==> jscpd: OK", file=sys.stderr)
        return

    print(
        f"ERROR: pre-flight: jscpd が重複コードの threshold 超過を検出しました (#3059)\n{proc.stdout}{proc.stderr}",
        file=sys.stderr,
    )
    failures.append("jscpd")


# ── vulture による未使用コード検出（Issue #3058）───────────────────────────────


def _check_vulture_deadcode(repo_root: Path, failures: list[str]) -> None:
    """vulture で `projects/py/tidd_tools/src` + `projects/py/publish/src` の未使用コードを検出する（Issue #3058）.

    `projects/py/vulture_whitelist.py`（baseline whitelist・#2807 で決定した ratchet 方式）を
    スキャン対象に含めることで、baseline 生成時点で検出済みだった 51 件は vulture が
    「使用済み」として扱い、whitelist に登録されていない新規の未使用コードのみを検出する。

    `projects/py/vulture_whitelist.py` が存在しない環境（handbook 外の consumer リポジトリ・
    baseline 未生成の tmp_path 環境等）はチェック対象外としてスキップする
    （jscpd の `.jscpd.json` 存在確認と同じ判定方式・Issue #3059）。

    信頼度は vulture の既定値（60）のまま変更しない（Issue #3058 やること）。
    """
    whitelist_path = repo_root / "projects" / "py" / "vulture_whitelist.py"
    if not whitelist_path.is_file():
        return  # handbook 外環境・baseline 未生成環境はスキップ

    tidd_tools_dir = repo_root / "projects" / "py" / "tidd_tools"
    targets = [
        p
        for p in (
            tidd_tools_dir / "src",
            repo_root / "projects" / "py" / "publish" / "src",
            whitelist_path,
        )
        if p.exists()
    ]

    print("==> vulture 未使用コードチェックを実行します...", file=sys.stderr)
    try:
        proc = subprocess.run(  # noqa: S603
            ["uv", "run", "--project", str(tidd_tools_dir), "vulture", *(str(p) for p in targets)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"WARN: pre-flight: vulture の実行に失敗しました: {exc}", file=sys.stderr)
        return

    if proc.returncode == 0:
        print("==> vulture: OK", file=sys.stderr)
        return

    print(
        f"ERROR: pre-flight: vulture が未使用コードを検出しました (#3058)\n{proc.stdout}{proc.stderr}\n"
        "       誤検知の場合は projects/py/vulture_whitelist.py に理由コメント付きで追記してください。",
        file=sys.stderr,
    )
    failures.append("vulture")


# ── PR diff サイズチェック（Issue #3081・#3994）─────────────────────────────

# lock ファイル（カウント対象外）: uv.lock・package-lock.json・その他 *.lock
_LOCK_FILE_RE = re.compile(r"(^|/)(uv\.lock|package-lock\.json|[^/]+\.lock)$")

_DIFF_SIZE_WARN_THRESHOLD = 500
# Issue #3994: ai-review の size/XXL gate（`ai_review/size_gate.py`）と同じ値を
# 単一の真実源（`XXL_LINE_THRESHOLD`）から import する。ハードコードで別々に持つと、
# 片方だけ変更されて「pre-flight は通るが ai-review では落ちる」非対称が再発する。
_DIFF_SIZE_BLOCK_THRESHOLD = _AI_REVIEW_XXL_LINE_THRESHOLD

# Issue #3994: escape hatch マーカーは PR ボディを単一の置き場所とする
# （`allow-single-commit`・`allow-test-update`・`no-doc-update` と揃える）。
# 正式名は `allow-xxl`（ai-review の size/XXL gate と共通）。旧名 `allow-large-pr`
# （pre-flight がコミットメッセージから読んでいた頃の名前）は後方互換で受理する。
_ALLOW_XXL_RE = re.compile(r"<!--\s*allow-xxl\s*:\s*((?:(?!-->).)+?)\s*-->")
_ALLOW_LARGE_PR_RE = re.compile(r"<!--\s*allow-large-pr\s*:\s*((?:(?!-->).)+?)\s*-->")
_SIZE_MARKER_RES = (_ALLOW_XXL_RE, _ALLOW_LARGE_PR_RE)


def _fetch_current_pr_body(repo_root: Path) -> str:
    """現在のブランチに紐づく PR の body を取得する（escape hatch 判定用・Issue #3994）.

    `tidd pre-flight` は通常 `gh pr create` の前に実行されるため、多くの場合まだ PR は
    存在せず空文字列を返す（fail-safe。この場合 diff-size gate は escape hatch なしで
    ブロックされ、実装者は PR 作成後に marker を追加して pre-flight を再実行するか、
    緊急スキップ環境変数 `PRE_FLIGHT_SKIP_DIFF_SIZE=1` を使う）。
    「周回」運用（pre-flight 再実行）で PR が既に作成済みの場合は、その body から
    escape hatch マーカーを読み取れる。
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", "--json", "body", "--jq", ".body"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _git_diff_numstat_total(repo_root: Path) -> int:
    """`git diff --numstat origin/main...HEAD`（三点・マージベース相対）の
    additions+deletions 合計行数を返す（Issue #3081・#3821）.

    二点diff（`git diff origin/main`）は、複数セッション並行稼働環境で自分の
    ブランチがマージした後に別セッションがさらに origin/main へマージすると、
    origin/main が自分のブランチのマージ後に得た無関係な変更の逆差分まで
    合計してしまう（#3821）。マージベース相対の三点diffを使うことで、
    自分のブランチが実際に加えた変更のみをカウントする。

    バイナリファイル（numstat が additions/deletions に `-` を返す行）と
    lock ファイル（``uv.lock``・``package-lock.json``・``*.lock``）はカウントから除外する。
    git 実行に失敗した場合（非ゼロ終了・FileNotFoundError・タイムアウト）は 0 を返す（fail-safe）。
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--numstat", "origin/main...HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    if proc.returncode != 0:
        return 0

    total = 0
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        additions_str, deletions_str, path = parts
        if additions_str == "-" or deletions_str == "-":
            continue  # バイナリファイル（numstat は additions/deletions を "-" で示す）
        if _LOCK_FILE_RE.search(path):
            continue  # lock ファイル
        try:
            total += int(additions_str) + int(deletions_str)
        except ValueError:
            continue
    return total


def _check_diff_size(repo_root: Path, failures: list[str]) -> int:
    """origin/main との diff 合計行数をチェックする（Issue #3081・#3994）.

    - 1000 行超: failures に "diff-size" を追加して exit 非 0 にする
    - 501〜1000 行: stderr に警告のみ出力（exit code に影響しない）
    - 500 行以下: 何もしない

    escape hatch（Issue #3994）: PR ボディに ``<!-- allow-xxl: <理由> -->``
    （理由必須。旧名 ``<!-- allow-large-pr: <理由> -->`` も後方互換で受理する）が
    あれば 1000 行超でもブロックしない。マーカーは ai-review の size/XXL gate
    （`ai_review/size_gate.py`）と同じ PR ボディを単一の置き場所とし、
    `allow-single-commit`・`allow-test-update`・`no-doc-update` と揃える。
    コミットメッセージのマーカーはもう受理しない（PR がまだ存在せず PR ボディに
    書けない場合は、緊急スキップ環境変数 `PRE_FLIGHT_SKIP_DIFF_SIZE=1` を使うか、
    PR 作成後に marker を追加して pre-flight を再実行する）。

    緊急スキップ: 環境変数 ``PRE_FLIGHT_SKIP_DIFF_SIZE=1`` でチェック全体をスキップする
    （既存 ``AI_REVIEW_SKIP_*`` 系と同じ流儀）。

    Returns:
        除外適用後の diff 合計行数（int）。`_write_preflight_record()` の `diff_lines` に
        渡すために呼び出し元へ返す。スキップ時は 0 を返す。
    """
    if os.environ.get("PRE_FLIGHT_SKIP_DIFF_SIZE") == "1":
        return 0

    diff_lines = _git_diff_numstat_total(repo_root)

    if diff_lines <= _DIFF_SIZE_WARN_THRESHOLD:
        return diff_lines

    if diff_lines <= _DIFF_SIZE_BLOCK_THRESHOLD:
        print(
            f"WARN: pre-flight: PR diff が {diff_lines} 行です（{_DIFF_SIZE_WARN_THRESHOLD} 行超）。"
            "分割を検討してください。詳細: docs/reference/pr-splitting-guide.md (#3081)",
            file=sys.stderr,
        )
        return diff_lines

    pr_body = _fetch_current_pr_body(repo_root)
    for marker_re in _SIZE_MARKER_RES:
        m = marker_re.search(pr_body)
        if m and m.group(1).strip():
            return diff_lines  # escape hatch 有効（理由あり）→ ブロックしない

    print(
        f"ERROR: pre-flight: diff-size — PR diff が {diff_lines} 行あり "
        f"{_DIFF_SIZE_BLOCK_THRESHOLD} 行を超えています (#3081・#3994)\n"
        "       docs/reference/pr-splitting-guide.md を参照して垂直分割を検討してください。\n"
        "       「分割してはいけないケース」に該当する場合は PR ボディに以下を追加してください\n"
        "       （コミットメッセージのマーカーはもう受理されません）:\n"
        "         <!-- allow-xxl: <理由> -->\n"
        "       PR がまだ存在しない場合は、PR 作成後に marker を追加して pre-flight を\n"
        "       再実行するか、緊急スキップ環境変数 PRE_FLIGHT_SKIP_DIFF_SIZE=1 を使ってください。",
        file=sys.stderr,
    )
    failures.append("diff-size")
    return diff_lines


# ── impl-delegation 委譲証跡チェック（Issue #3153）───────────────────────────

# RED/GREEN ステップを持つのは feat/fix のみ（refactor/docs は別 phase を使うため対象外）
_IMPL_DELEGATION_REQUIRED_PHASES = ("red", "green")


def _read_recorded_phases(calls_log_path: Path, issue_number: str) -> set[str]:
    """propose-step 呼び出しログ（calls.jsonl・Issue #3152）から対象 Issue の記録済み phase を返す.

    ファイル未存在・読み込み失敗時は空集合を返す。JSON パース失敗行はスキップして
    後続の行の読み取りを続ける（fail-open）。
    """
    try:
        text = calls_log_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    phases: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and str(record.get("issue")) == issue_number:
            phase = record.get("phase")
            if isinstance(phase, str):
                phases.add(phase)
    return phases


def _check_impl_delegation_evidence(branch: str, issue_key: str | None, failures: list[str]) -> None:
    """impl-delegation 有効時、RED/GREEN 両 phase の propose-step 呼び出し証跡を検証する（Issue #3153）.

    `impl-delegation: true` かつ `impl-backend` が config.json に設定されている場合のみ対象
    （opt-in 機能のため、無効・未設定時はチェック自体をスキップする）。feat/fix ブランチのみ対象
    （RED/GREEN ステップを持つのは feat/fix のみ）。

    Issue #3696: backend が現在の環境で解決不能（例: worktree に API キーが供給されない）場合は
    delegation 未使用扱いにしてチェック自体をスキップする（毎回 escape hatch を設定する運用を不要にする）。

    対象 Issue の `~/.cache/ai-dev-handbook/propose-step/calls.jsonl` に phase `red` / `green`
    両方の呼び出し記録が存在しなければ failures に `"impl-delegation"` を追加する。

    escape hatch: 環境変数 `IMPL_DELEGATION_SKIP_CHECK=1` でチェック全体をスキップする。
    """
    if os.environ.get("IMPL_DELEGATION_SKIP_CHECK") == "1":
        return
    if not _is_feat_or_fix_branch(branch):
        return  # feat/fix ブランチのみ対象

    from tidd_tools import propose_step

    if not propose_step._is_impl_delegation_enabled():
        return
    backend = propose_step._read_impl_backend()
    if backend is None:
        return
    # Issue #3696: backend が現在の環境で解決不能（例: worktree に API キーが
    # 供給されない）場合は delegation 未使用扱いにして委譲証跡チェック自体を
    # スキップする（毎回 IMPL_DELEGATION_SKIP_CHECK=1 を設定する運用を不要にする）。
    if not propose_step._is_backend_resolvable(backend):
        return
    if issue_key is None:
        return  # Issue 番号を特定できない場合は PR 側 gate に委ねる

    issue_number_opt = _extract_issue_number(issue_key)
    if issue_number_opt is None:
        return
    issue_number = str(issue_number_opt)

    recorded = _read_recorded_phases(propose_step._calls_log_path(), issue_number)
    missing = [p for p in _IMPL_DELEGATION_REQUIRED_PHASES if p not in recorded]
    if not missing:
        return

    print(
        "ERROR: pre-flight: impl-delegation が有効ですが、propose-step の呼び出しログに "
        f"phase {'/'.join(missing)} の記録がありません (#3153)\n"
        f"       Issue #{issue_number} で `tidd propose-step --phase red --issue {issue_number}` / "
        f"`tidd propose-step --phase green --issue {issue_number} --test-output <FILE>` を実行してください。\n"
        "       意図的にスキップする場合は環境変数 IMPL_DELEGATION_SKIP_CHECK=1 を設定してください。",
        file=sys.stderr,
    )
    failures.append("impl-delegation")


# ── npm-cooldown（依存更新バージョンの公開日ゲート・Issue #3439）───────────────

# ChainDrop（2026-08-04）型の npm サプライチェーン攻撃では、汚染バージョンの公開から
# レジストリ除去まで数時間〜数日のラグがある。公開直後（7 日未満）のバージョンを
# 機械的にブロックし、依存更新のたびの手動確認を不要にする。
_NPM_COOLDOWN_DAYS = 7
_NPM_COOLDOWN_SKIP_ENV = "AI_PREFLIGHT_SKIP_NPM_COOLDOWN"
_NPM_COOLDOWN_ALLOWLIST_RELPATH = Path(".claude") / "rules" / "npm-cooldown-allowlist.yaml"


def _load_npm_cooldown_allowlist(repo_root: Path) -> frozenset[str]:
    """`.claude/rules/npm-cooldown-allowlist.yaml` を読み込み許可済み `name@version` 集合を返す（Issue #3498）.

    CI 稼働実績のある既知パッケージをパッケージ単位で cooldown 例外扱いするための許可リスト。
    ファイル不存在・YAML パース失敗・`packages` キー不在/非リストの場合は空集合を返す
    （許可リストなしとして扱い、既存の cooldown ブロックの厳格性は変えない）。
    """
    path = repo_root / _NPM_COOLDOWN_ALLOWLIST_RELPATH
    if not path.is_file():
        return frozenset()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    packages = data.get("packages")
    if not isinstance(packages, list):
        return frozenset()
    return frozenset(str(p) for p in packages if p)


def _read_package_lock_at_ref(repo_root: Path, ref: str) -> dict[str, object] | None:
    """`git show <ref>:package-lock.json` の内容を dict として返す.

    ファイルが ref 上に存在しない・JSON パース失敗・git 実行失敗の場合は None を返す
    （呼び出し側は「base に存在しない＝全パッケージが新規追加」として扱ってよい）。
    """
    try:
        proc = subprocess.run(
            ["git", "show", f"{ref}:package-lock.json"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _package_name_from_lockfile_key(key: str) -> str:
    """lockfile v3 の `packages` キー（node_modules パス）からパッケージ名を抽出する.

    ``node_modules/@scope/foo`` や入れ子の ``node_modules/bar/node_modules/@scope/baz``
    のようなパスから、最後の ``node_modules/`` 以降（スコープ含む）を取り出す。
    """
    idx = key.rfind("node_modules/")
    if idx == -1:
        return key
    return key[idx + len("node_modules/") :]


def _npm_lockfile_added_or_updated_versions(repo_root: Path, changed_files: list[str]) -> list[tuple[str, str]]:
    """package-lock.json の base（origin/main）との diff から追加・更新されたパッケージ@バージョンを抽出する.

    ``package-lock.json`` が changed_files に含まれない場合、または現在の lockfile の
    読み取り・パースに失敗した場合は空リストを返す（fail-open。他の pre-flight チェックが
    処理する対象外・パース不能なケースはブロックしない設計に合わせる）。
    """
    if "package-lock.json" not in changed_files:
        return []

    lock_path = repo_root / "package-lock.json"
    try:
        current = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(current, dict):
        return []

    base = _read_package_lock_at_ref(repo_root, "origin/main")
    base_packages = base.get("packages", {}) if isinstance(base, dict) else {}
    if not isinstance(base_packages, dict):
        base_packages = {}
    current_packages = current.get("packages", {})
    if not isinstance(current_packages, dict):
        return []

    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, entry in current_packages.items():
        if not key or not isinstance(entry, dict):
            continue  # "" はルートプロジェクト自体
        version = entry.get("version")
        if not isinstance(version, str):
            continue  # link/workspace エントリなど version を持たないもの

        old_entry = base_packages.get(key)
        old_version = old_entry.get("version") if isinstance(old_entry, dict) else None
        if old_version == version:
            continue  # 変更なし

        pair = (_package_name_from_lockfile_key(key), version)
        if pair in seen:
            continue
        seen.add(pair)
        result.append(pair)
    return result


def _npm_view_publish_time(name: str, version: str) -> datetime | None:
    """`npm view <name>@<version> time --json` からバージョンの公開日時を取得する.

    実行失敗（コマンド未検出・タイムアウト・非 0 終了）・出力パース失敗・該当バージョンが
    出力に含まれない場合は None を返す（呼び出し側は fail-open としてブロックしない）。
    """
    try:
        proc = subprocess.run(  # noqa: S603, S607
            ["npm", "view", f"{name}@{version}", "time", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    published = data.get(version)
    if not isinstance(published, str):
        return None
    try:
        return datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return None


def _check_npm_cooldown(repo_root: Path, changed_files: list[str], failures: list[str]) -> None:
    """依存更新バージョンの公開日 cooldown ゲート（Issue #3439）.

    package-lock.json の base（origin/main）との diff から追加・更新されたバージョンを抽出し、
    公開から ``_NPM_COOLDOWN_DAYS`` 日未満のものが 1 つでもあれば failures に "npm-cooldown" を
    追加する。

    escape hatch: 環境変数 ``AI_PREFLIGHT_SKIP_NPM_COOLDOWN=1``（緊急 audit 対応用）。
    package-lock.json の変更がある場合のみ stderr に skip した旨の警告を出す。

    npm view の実行に失敗したバージョンは fail-open（ブロックしない）とする。ネットワーク
    障害・レジストリ一時不調で pre-flight 全体を止めないための設計判断。
    """
    if os.environ.get(_NPM_COOLDOWN_SKIP_ENV) == "1":
        if "package-lock.json" in changed_files:
            print(
                f"WARN: pre-flight: npm-cooldown チェックを {_NPM_COOLDOWN_SKIP_ENV}=1 により"
                "skip しました。ChainDrop 型サプライチェーン攻撃への露出リスクがあるため、"
                "緊急対応後は速やかに公開日を確認してください (#3439)",
                file=sys.stderr,
            )
        return

    candidates = _npm_lockfile_added_or_updated_versions(repo_root, changed_files)
    if not candidates:
        return

    allowlist = _load_npm_cooldown_allowlist(repo_root)
    now = datetime.now(UTC)
    threshold = timedelta(days=_NPM_COOLDOWN_DAYS)
    blocked: list[str] = []
    for name, version in candidates:
        if f"{name}@{version}" in allowlist:
            continue  # 許可リスト登録済み（CI 稼働実績あり）は cooldown 例外扱い（#3498）
        published_at = _npm_view_publish_time(name, version)
        if published_at is None:
            continue  # npm view 取得不能は fail-open
        if now - published_at < threshold:
            blocked.append(f"{name}@{version}（公開: {published_at.strftime('%Y-%m-%dT%H:%M:%SZ')}）")

    if not blocked:
        return

    print(
        f"ERROR: pre-flight: npm-cooldown — 公開から {_NPM_COOLDOWN_DAYS} 日未満のパッケージが"
        "含まれています (#3439)\n"
        + "\n".join(f"         - {b}" for b in blocked)
        + "\n       ChainDrop 型サプライチェーン攻撃対策のため、公開直後バージョンの取り込みを"
        "ブロックしています。\n"
        f"       緊急 audit 対応等でどうしても必要な場合は環境変数 {_NPM_COOLDOWN_SKIP_ENV}=1 を"
        "設定してください。",
        file=sys.stderr,
    )
    failures.append("npm-cooldown")


# ── sync-template drift チェック（Issue #3417）───────────────────────────────

_SYNC_TEMPLATE_SKIP_ENV = "TIDD_SKIP_SYNC_TEMPLATE_CHECK"


def _check_sync_template(repo_root: Path, failures: list[str]) -> None:
    """root .claude/** と templates/workflow/.claude/** の同期漏れチェック（Issue #3417）.

    `check_template_drift` は PR diff に含まれるファイルしか比較しないため、diff に
    含まれない過去の乖離を検知できない。`sync_template.find_sync_targets` は
    templates/workflow/.claude/** の配布対象ファイル全件を走査するため、diff 非依存で
    乖離を検知できる。

    escape hatch: 環境変数 ``TIDD_SKIP_SYNC_TEMPLATE_CHECK=1`` でチェック全体をスキップする。
    """
    if os.environ.get(_SYNC_TEMPLATE_SKIP_ENV) == "1":
        return

    from tidd_tools import sync_template

    targets = sync_template.find_sync_targets(repo_root)
    if not targets:
        return

    print(
        f"ERROR: pre-flight: templates/workflow/.claude/** が root と乖離しています "
        f"({len(targets)} 件・#3417)\n"
        + "\n".join(f"         - {t}" for t in targets)
        + "\n       `tidd sync-template` を実行して同期し、コミットしてください。\n"
        f"       意図的な差分の場合は templates/workflow/_copier/sync-exempt.yaml へ追加してください。\n"
        f"       緊急時は環境変数 {_SYNC_TEMPLATE_SKIP_ENV}=1 でスキップできます。",
        file=sys.stderr,
    )
    failures.append("sync-template drift")


# ── regression テスト命名チェック（Issue #3418）───────────────────────────────

# 命名規則: tests/regressions/ 配下の新規 regression テストは
# `test_fix_<N>_<slug>.py`（N は Issue 番号・slug は [a-z0-9_]+・例: test_fix_3401_bypass_audit_path.py）。
# slug を必須化することで、ファイル名からどの振る舞いを守るテストか読み取れるようにする。
_REGRESSION_TEST_PATH_RE = re.compile(r"(?:^|/)tests/regressions/test_[^/]+\.py$")
_REGRESSION_FIX_TEST_NAME_RE = re.compile(r"^test_fix_\d+_[a-z0-9_]+\.py$")


def _added_regression_test_files(repo_root: Path) -> list[str]:
    """origin/main...HEAD で新規追加（--diff-filter=A）された regressions テストファイルを返す.

    ``git diff --name-only --diff-filter=A origin/main...HEAD`` で**新規追加**された
    ``*/tests/regressions/test_*.py`` のみを抽出する。既存ファイル（modified 等）は
    ``--diff-filter=A`` により対象外になる（リネーム強制しない・#3418）。
    git 実行に失敗した場合は空リストを返す（fail-open）。
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=A", "origin/main...HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and _REGRESSION_TEST_PATH_RE.search(line.strip())
    ]


def _check_regression_test_naming(repo_root: Path, failures: list[str]) -> None:
    """tests/regressions/ 配下の新規テストファイルの命名規則チェック（Issue #3418）.

    新規追加された ``test_fix_*`` ファイル（slug なし = ``test_fix_<N>.py`` 等）が
    ``test_fix_<N>_<slug>.py`` に従っていない場合、stderr に違反一覧と命名例を出力し、
    failures に ``"regression-test-naming"`` を追加する。``test_fix_*`` 以外の prefix
    （test_feat_/test_refactor_/test_build_ 等）は本規則の対象外。

    escape hatch は設けない（新規ファイルのみが対象で誤爆余地がないため・#3418）。
    """
    invalid_paths: list[str] = []
    for rel_path in _added_regression_test_files(repo_root):
        filename = Path(rel_path).name
        # 命名規則は test_fix_* 限定（test_feat_* 等は対象外）
        if not filename.startswith("test_fix_"):
            continue
        if _REGRESSION_FIX_TEST_NAME_RE.match(filename):
            continue
        invalid_paths.append(rel_path)

    if not invalid_paths:
        return

    files_list = "\n".join(f"         - {rel_path}" for rel_path in sorted(invalid_paths))
    print(
        f"ERROR: pre-flight: regressions テストの命名規則に違反しています (#3418)\n"
        "       命名規則: test_fix_<N>_<slug>.py\n"
        "         （N: Issue 番号・slug: [a-z0-9_]+・例: test_fix_3401_bypass_audit_path.py）\n"
        f"       違反ファイル:\n{files_list}",
        file=sys.stderr,
    )
    failures.append("regression-test-naming")


# ── 配布物内の docs パス参照チェック（Issue #4039）─────────────────────────────


def _check_distributed_docs_references(repo_root: Path, failures: list[str]) -> None:
    """配布物内の docs/ パス参照が配布済みか・baseline 登録済みかを検証する（Issue #4039・#4070）.

    `templates/workflow/` 配下の hook・rule・skill・agent 定義から参照される docs/ パスが
    `templates/workflow/docs/` に実在せず、かつ baseline データファイル
    （`projects/py/tidd_tools/src/tidd_tools/data/docs-reference-baseline.yaml`）にも
    未登録の場合、consumer が案内されたパスを開けなくなる新規の壊れたリンクとして検出する。

    逆方向（Issue #4070）: 「本体の...配下（consumer 未配布）」と注記されているが実際には
    配布済みの docs/ パスも、consumer が誤った案内で参照先を見失う不具合として検出する。
    """
    from tidd_tools import docs_reference_check

    new_refs = docs_reference_check.find_new_undistributed_references(repo_root)
    if new_refs:
        refs_list = "\n".join(f"         - {r}" for r in new_refs)
        print(
            "ERROR: pre-flight: consumer に配布されていない docs/ パスが新規参照されています (#4039)\n"
            f"{refs_list}\n"
            "       templates/workflow/docs/ 配下へ配布するか、"
            "projects/py/tidd_tools/src/tidd_tools/data/docs-reference-baseline.yaml へ追加してください。",
            file=sys.stderr,
        )
        failures.append("distributed-docs-reference")

    misclassified_refs = docs_reference_check.find_misclassified_distributed_references(repo_root)
    if misclassified_refs:
        refs_list = "\n".join(f"         - {r}" for r in misclassified_refs)
        print(
            "ERROR: pre-flight: 配布済み docs/ パスが「consumer 未配布」と誤注記されています (#4070)\n"
            f"{refs_list}\n"
            "       「本体の...配下（consumer 未配布）」の定型句をやめ、"
            "相対パス（例: docs/setup/secrets-management.md）表記に戻してください。",
            file=sys.stderr,
        )
        failures.append("misclassified-distributed-docs-reference")


# ── 未追跡の決定ジャーナル検査（Issue #4077）────────────────────────────────


def _check_untracked_decision_journals(repo_root: Path) -> None:
    """docs/decisions/ 配下の git 未追跡 .md ファイルを検知し stdout に WARN として出力する（Issue #4077）.

    決定ジャーナル（`docs/decisions/YYYY-MM-DD-<slug>.md`）は対応する Issue/PR を
    持たないケースがあり（壁打ちの結論だけが成果物のケース）、その場合 commit 経路に
    乗らず git 未追跡（`git status` の `??`）のまま残る。git 管理外である以上、
    ワーキングツリーを掃除した瞬間や worktree の作り直しで失われてしまうため、
    PR 作成前の最後の関門である pre-flight で気づけるようにする。

    決定ジャーナルは別 PR でコミットしたい場合や書きかけの場合があり、無関係な PR の
    作成を止めるべきではないため、検出しても failures には追加しない（非ブロッキング）。
    """
    decisions_dir = repo_root / "docs" / "decisions"
    if not decisions_dir.is_dir():
        return  # docs/decisions/ が存在しないリポジトリ（consumer 等）はチェック対象外

    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain", "--", "docs/decisions"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"WARN: pre-flight: 未追跡の決定ジャーナル検査の実行に失敗しました: {exc}", file=sys.stderr)
        return

    if proc.returncode != 0:
        return  # git status 失敗（fail-open・非ブロッキングチェックのため無言でスキップ）

    untracked_paths = [
        rel_path
        for line in proc.stdout.splitlines()
        if line.startswith("?? ") and (rel_path := line[3:].strip()).endswith(".md")
    ]
    if not untracked_paths:
        return

    print("WARN: pre-flight: 未追跡の決定ジャーナルが見つかりました。コミットを検討してください（#4077）")
    for path in untracked_paths:
        print(path)


# ── [AI確認] 項目の事前提示（Issue #4074）───────────────────────────────────
#
# `[AI確認]` 項目の検証が `tidd ai-review` の APPROVE 後（`.claude/rules/test-plan-checklist.md`）
# に置かれているため、レビュー結果に依存しない項目（成果物の記述の有無・ファイル存在等）まで
# APPROVE 後まで検証が遅れ、不備発覚 → 修正 → push → 再レビューの往復が発生していた。
# pre-flight の時点で `[AI確認]` 未消化項目を一覧提示し、レビュー非依存項目は
# ai-review 実行前に検証できるようにする。ブロックはしない（failures に追加しない）。
# 分類基準・改訂後の運用は `.claude/rules/test-plan-checklist.md` 参照。

_AI_CONFIRM_SKIP_MESSAGE = "ai-confirm skip"


def _fetch_pr_body_for_ai_confirm(repo_root: Path) -> tuple[str | None, bool]:
    """現在のブランチに紐づく PR の body を取得する（Issue #4074）.

    Returns:
        ``(body, True)``: 取得成功（PR が存在し gh 呼び出しも成功）。
        ``(None, False)``: PR 未作成 or gh 呼び出し失敗（区別しない。呼び出し元は
        Issue 本文へフォールバックする）。
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", "--json", "body", "--jq", ".body"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, False
    if proc.returncode != 0:
        return None, False
    return proc.stdout, True


def _fetch_issue_body_for_ai_confirm(issue_number: str) -> tuple[str | None, bool]:
    """Issue 本文を取得する（Issue #4074）.

    Returns:
        ``(body, True)``: 取得成功。 ``(None, False)``: gh 呼び出し失敗。
    """
    try:
        proc = subprocess.run(
            ["gh", "issue", "view", issue_number, "--json", "body", "--jq", ".body"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, False
    if proc.returncode != 0:
        return None, False
    return proc.stdout, True


def _print_ai_confirm_items(items: list[str]) -> None:
    """`[AI確認]` 未消化項目を stdout へ一覧表示する（Issue #4074）.

    ``items`` が空の場合は ``ai-confirm skip`` を出力する（`tidd test-plan` 未生成時の
    「チェックリスト0件スキップ」と同じ流儀）。
    """
    if not items:
        print(_AI_CONFIRM_SKIP_MESSAGE)
        return
    print(f"==> pre-flight: 未消化の [AI確認] 項目が {len(items)} 件あります (#4074)")
    for item in items:
        print(item.strip())


def _check_ai_confirm_items(repo_root: Path, branch: str, issue_key: str | None) -> None:  # noqa: ARG001
    """PR 作成前に `[AI確認]` 項目を stdout へ提示する（Issue #4074）.

    PR が既に存在する場合は PR ボディの `## Test plan` に含まれる未消化 `[AI確認]` 行を、
    まだ存在しない場合は Issue 本文（`## やること`）の未消化 `[AI確認]` 行を表示する。

    どちらも取得できない場合（PR 未作成 かつ Issue 番号が特定できない・gh 呼び出し失敗）は
    stderr に警告を出すのみで、failures には追加しない（fail-open。#4074 の Gherkin
    「Issue も PR も取得できない場合は中断しない」）。

    ``issue_key`` は呼び出し元との一貫性のため受け取るが、Issue 番号解決は
    ``branch`` から ``_extract_issue_number()`` で直接行う（``issue_key`` は
    feat/fix 以外の type では None になりうるため）。
    """
    from tidd_tools.ai_review.verify_ai_confirm import _extract_ai_confirm_lines

    pr_body, pr_ok = _fetch_pr_body_for_ai_confirm(repo_root)
    if pr_ok and pr_body is not None:
        _print_ai_confirm_items([line for _idx, line in _extract_ai_confirm_lines(pr_body)])
        return

    issue_number = _extract_issue_number(branch) if branch else None
    if issue_number is None:
        print(
            "WARN: pre-flight: [AI確認] 事前提示 — Issue 番号を特定できずスキップしました (#4074)",
            file=sys.stderr,
        )
        return

    issue_body, issue_ok = _fetch_issue_body_for_ai_confirm(str(issue_number))
    if not issue_ok or issue_body is None:
        print(
            "WARN: pre-flight: [AI確認] 事前提示 — Issue/PR 本文の取得に失敗しました（gh コマンドエラー・#4074）",
            file=sys.stderr,
        )
        return

    _print_ai_confirm_items([line for _idx, line in _extract_ai_confirm_lines(issue_body)])
