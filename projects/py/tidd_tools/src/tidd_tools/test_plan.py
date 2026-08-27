"""test-plan サブコマンド（旧 scripts/test-plan.sh の Python 移植）.

旧 bash 実装の主要機能を 1:1 で再現する:

1. 再帰ガード（_TIDD_TOOLS_RECURSION_GUARD で代替）
2. PR ボディから checklist 抽出（コードブロック・人間セクション除外）
3. PR diff の sh/bats / projects/gas / projects/py 変更検出
4. 項目分類（[AI確認] / [手動] / bats / Jest / pytest / 未カバー）
5. Jest 実行（projects/gas/<project>）
6. pytest 実行（projects/py/<project>）
7. bats 並列実行（タグ選択・per-file/suite タイムアウト・タイムアウト時 Issue 自動作成）
8. PR diff の tests/regressions/*.bats 明示パス実行
9. スキップ regression の JSONL 記録 + PR コメント
10. 未カバー項目で exit 1 ブロック
11. PR ボディ更新（[x] マーク・人間確認セクション）

Phase 4 で bats 全廃したら 5/6/7/8/9 を pytest marker ベースに書き換える予定。
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterable, MutableMapping
from datetime import UTC, datetime
from pathlib import Path

# Phase 2-A (#1053) で `tidd_tools.shared.gh_client` に統合済み。
# 既存コード（`gh.pr_body` 等）と既存テスト（`mocker.patch("tidd_tools.test_plan.gh.xxx")`）の
# 互換のため、エイリアス名 `gh` で再エクスポートする。
from tidd_tools import (
    context_budget,
    preflight_markers,
    pytest_tmpdir,
    skill_checker,
    test_output_capture,
    tmp_capacity,
)
from tidd_tools.pytest_flock import PytestFlockContext
from tidd_tools.pytest_workers import calc_pytest_workers_from_system
from tidd_tools.shared import gh_client as gh
from tidd_tools.shared.checklist import classify_checkbox_text
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.issue_body import extract_closes_issues
from tidd_tools.shared.paths import cache_dir as _cache_dir
from tidd_tools.shared.recursion import is_recursive_call, mark_recursive_subprocess

logger = logging.getLogger(__name__)


# ── 振る舞いの定数 ────────────────────────────────────────────────────────────

JEST_KEYWORD_RE = re.compile(r"jest", re.IGNORECASE)
PYTEST_KEYWORD_RE = re.compile(r"pytest", re.IGNORECASE)
BATS_KEYWORD_RE = re.compile(r"bats|リグレッションテスト", re.IGNORECASE)

CHECKBOX_RE = re.compile(r"^[ \t]*- \[ \]")
CODEBLOCK_RE = re.compile(r"^[ \t]*(```|~~~)")
HUMAN_SECTION_HEADER = "## 人間の確認が必要な項目"
TEST_PLAN_SECTION_HEADER_RE = re.compile(r"^##[ \t]+Test plan\b", re.IGNORECASE)
H2_HEADING_RE = re.compile(r"^##[ \t]+")
# Issue #2468: チェックボックスなし箇条書き検知用
# `- ` で始まるが `- [ ]`・`- [x]`・`- [X]` にマッチしない行を「書式崩れ」とする
BARE_BULLET_RE = re.compile(r"^[ \t]*- (?!\[[ xX]\])")
CHECKED_CHECKBOX_RE = re.compile(r"^[ \t]*- \[[xX]\]")


@dataclasses.dataclass
class Classified:
    auto_items: list[str] = dataclasses.field(default_factory=list)
    jest_items: list[str] = dataclasses.field(default_factory=list)
    pytest_items: list[str] = dataclasses.field(default_factory=list)
    ai_confirm_items: list[str] = dataclasses.field(default_factory=list)
    # Issue #1402: `[AI確認-post-merge]` は auto-merge を妨げず、post-merge で
    # cron routine `verify-post-merge` が自律検証する項目。
    # `[AI確認]` (pre-merge) とは別枠で管理する。
    ai_confirm_post_merge_items: list[str] = dataclasses.field(default_factory=list)
    human_items: list[str] = dataclasses.field(default_factory=list)
    uncovered_items: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class RunContext:
    pr_num: str
    repo: str | None
    repo_root: Path
    state_dir: Path | None
    dry_run: bool
    json_output: bool


@dataclasses.dataclass(frozen=True)
class PytestOutcome:
    """`_run_pytest()` の結果（Issue #2778）.

    `passed` だけでは「実行して成功」と「未実行のまま成功扱い」を区別できず、呼び出し元
    （`pre_flight.py`）が未検証の tree にマーカーを書いてしまうため `ran` を併せて返す。

    `marker_path`/`marker_written_at` は `skip_reason == "マーカー hit"` の場合のみ設定される
    （Issue #2800）。マーカー hit 時に「どのファイルが・いつ書かれたか」を呼び出し元が
    自己記録できるようにする。
    """

    passed: bool
    ran: bool
    skip_reason: str | None = None
    marker_path: str | None = None
    marker_written_at: str | None = None


# ── entry point ──────────────────────────────────────────────────────────────


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "test-plan",
        help="PR の Test plan チェックリストを検証する（test-plan.sh の Python 版）",
        description=(
            "PR ボディの Test plan チェックリストを解析し、bats/Jest/pytest を自動実行して "
            "結果に応じて - [x] に更新する。未カバー項目があれば exit 1 でブロックする。"
        ),
    )
    parser.add_argument("pr_num", help="PR 番号")
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    ctx = RunContext(
        pr_num=str(args.pr_num),
        repo=os.environ.get("REPO") or gh.repo_name_with_owner(),
        repo_root=_resolve_repo_root(),
        state_dir=Path(os.environ["STATE_DIR"]) if os.environ.get("STATE_DIR") else None,
        dry_run=bool(args.dry_run),
        json_output=bool(args.json_output),
    )
    if ctx.state_dir is not None:
        ctx.state_dir.mkdir(parents=True, exist_ok=True)
    return run(ctx)


# ── メインフロー ─────────────────────────────────────────────────────────────


def _run_pre_execution_gates(repo_root: Path, changed_files: list[str]) -> int | None:
    """CLAUDE.md/rules 変更・skills 変更の静的ゲートを実行する（#3187 C901 対応).

    ゲート対象の変更があり違反していれば exit code（1）を返す。それ以外は None を返す。
    """
    # Issue #1883: CLAUDE.md / `.claude/rules/**` 変更 PR は静的コンテキスト予算の
    # HARD ゲートを実行する（超過で exit 1・ラチェット式）。
    if any(context_budget.is_gate_trigger(f, repo_root) for f in changed_files):
        print(
            "==> context-budget: CLAUDE.md / .claude/rules / import 先変更を検出 → HARD 閾値チェック",
            file=sys.stderr,
        )
        if context_budget.run_gate(repo_root) != 0:
            print("テスト計画の検証に失敗しました（context budget exceeded）", file=sys.stderr)
            return 1
        print("==> context-budget: HARD 閾値以内（PASS）", file=sys.stderr)

    # Issue #2112: .claude/skills/**/*.md 変更 PR は SKILL.md 品質ゲートを実行する。
    if any(skill_checker.is_gate_trigger(f) for f in changed_files):
        print(
            "==> skill-checker: .claude/skills 変更を検出 → SKILL.md 品質チェック",
            file=sys.stderr,
        )
        if skill_checker.run_gate(repo_root, list(changed_files)) != 0:
            print("テスト計画の検証に失敗しました（SKILL.md quality gate failed）", file=sys.stderr)
            return 1
        print("==> skill-checker: SKILL.md 品質チェック PASS", file=sys.stderr)

    return None


def run(ctx: RunContext) -> int:
    # Issue #3788: `gh.pr_body()` の既定（fail-soft）は認証失敗等の取得失敗も
    # 「ボディが空」も同じ空文字列で返すため、未カバー項目検出ゲートが機能して
    # いない事実に気付けないまま exit 0 で通過していた。`raise_on_error=True` で
    # 取得失敗を例外として区別し、失敗時は exit 0 で黙って通過させない。
    try:
        pr_body = gh.pr_body(ctx.pr_num, ctx.repo, raise_on_error=True)
    except GhCommandError as exc:
        print(
            f"ERROR: PR #{ctx.pr_num} のボディを取得できませんでした（{exc}）。"
            "テスト計画チェックを実行できないため失敗させます（Issue #3788）。",
            file=sys.stderr,
        )
        return 1
    if not pr_body:
        print(
            f"==> PR #{ctx.pr_num} のボディが空です。テスト計画チェックをスキップします。",
            file=sys.stderr,
        )
        return 0

    items = extract_checklist(pr_body)

    # Issue #2468: チェックボックス書式崩れの検知（0件スキップとは別パス）
    format_errors = detect_format_errors(pr_body)
    if format_errors:
        print(
            "ERROR: ## Test plan セクションにチェックボックス形式でない箇条書きが含まれています（Issue #2468）。",
            file=sys.stderr,
        )
        print(
            "       各項目は `- [ ] ...` または `- [x] ...` 形式で記述してください。",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        for err_line in format_errors:
            print(f"  書式崩れ: {err_line}", file=sys.stderr)
        print("", file=sys.stderr)
        return 1

    if not items:
        print("==> テスト計画にチェックリスト項目がありません。スキップします。", file=sys.stderr)
        return 0

    print(f"==> テスト計画チェックリスト: {len(items)} 件", file=sys.stderr)

    changed_files = gh.pr_diff_files(ctx.pr_num, ctx.repo)

    gate_exit_code = _run_pre_execution_gates(ctx.repo_root, changed_files)
    if gate_exit_code is not None:
        return gate_exit_code

    skip_bats_no_sh = _skip_bats_no_sh_change(changed_files)
    pr_regressions, skip_existing_regressions = _collect_regressions(ctx.repo_root, changed_files)

    # Issue #2972: bats のタグベース選択実行（_select_bats_files_by_tags）は撤去済み。
    # tests/*.bats は現在 0 件のため bats_filter_files=None（全件対象・実質 no-op）で通す。
    if os.environ.get("AI_REVIEW_BATS_FULL") == "1":
        print("==> bats: AI_REVIEW_BATS_FULL=1 のためフル実行します", file=sys.stderr)

    classified = classify_items(items)

    jest_passed = _run_jest(ctx, changed_files, classified.jest_items)
    pytest_passed = _run_pytest(ctx, changed_files, classified.pytest_items).passed
    auto_passed = _run_bats(
        ctx,
        skip_bats_no_sh=skip_bats_no_sh,
        skip_bats_no_tag=False,
        bats_filter_files=None,
    )

    if not _run_pr_regressions(ctx, pr_regressions):
        return 1

    # Issue #1464: feat/fix PR には対応する .feature ファイルの生成を必須化
    feature_error = _check_feature_file_required(ctx, changed_files)
    if feature_error:
        print(feature_error, file=sys.stderr)
        return 1

    # Issue #2000: PR 変更ファイル中の step_defs に xfail marker が残っていればブロック
    xfail_error = _check_step_defs_xfail(ctx, changed_files)
    if xfail_error:
        print(xfail_error, file=sys.stderr)
        return 1

    _emit_skip_regressions_log(ctx, skip_existing_regressions, skip_bats_no_sh)

    if classified.uncovered_items:
        _emit_uncovered_error(classified.uncovered_items)
        return 1

    # Issue #1402: `[AI確認-post-merge]` があれば post-merge 検証に委ねる旨を通知する。
    # auto-merge はブロックしない（Scenario 1）。
    if classified.ai_confirm_post_merge_items:
        print(
            "==> [AI確認-post-merge] は post-merge で検証されます "
            f"({len(classified.ai_confirm_post_merge_items)} 件・cron routine verify-post-merge が処理)",
            file=sys.stderr,
        )

    updated_body = _update_pr_body(
        pr_body,
        classified=classified,
        auto_passed=auto_passed,
        jest_passed=jest_passed,
        pytest_passed=pytest_passed,
    )

    if ctx.dry_run:
        print("==> dry-run: PR ボディ更新をスキップします", file=sys.stderr)
    else:
        # Issue #3776: gh.update_pr_body() は REST 化後も別要因（ネットワーク断・404 等）で
        # GhCommandError を送出しうる。未処理例外（トレースバック）で落ちるのではなく、
        # 制御されたエラーメッセージを stderr に出力して exit 1 で終了する。
        try:
            gh.update_pr_body(ctx.pr_num, ctx.repo, updated_body)
        except GhCommandError as exc:
            print(f"ERROR: PR ボディの更新に失敗しました: {exc}", file=sys.stderr)
            return 1

    _print_summary(classified)
    return 0


# ── PR ボディ解析 ────────────────────────────────────────────────────────────


def extract_checklist(body: str) -> list[str]:
    """PR ボディの `## Test plan` セクション内から `- [ ]` 項目を抽出する（Issue #1272）.

    抽出範囲は「`## Test plan` 見出しの次の行から、次の `## ` 見出しまで」に限定する。
    これにより PR ボディに Test plan 以外のチェックリスト（例: Issue の やること
    消化状況、Definition of Done 等）が含まれても、それらは Test plan 項目として
    認識されない。

    - `## Test plan` セクションが存在しない場合は空リストを返す
    - コードブロック内の `- [ ]` は除外する
    - `## 人間の確認が必要な項目` セクション内の項目は除外する
    - 各項目の先頭 `- [ ] ` は剥がした文字列を返す
    """
    result: list[str] = []
    in_test_plan = False
    in_code = False
    in_human = False
    for line in body.splitlines():
        # Test plan セクションの開始/終了判定は他のフラグより優先する。
        # コードブロック内の見出しは Markdown 上ありえないため考慮しない。
        if TEST_PLAN_SECTION_HEADER_RE.match(line):
            in_test_plan = True
            in_code = False
            in_human = False
            continue
        if in_test_plan and H2_HEADING_RE.match(line):
            # 次の H2 見出しに遭遇したら Test plan セクションを抜ける。
            # そのまま人間確認セクションの開始判定も行う（後段で処理される）。
            in_test_plan = False

        if not in_test_plan:
            # Test plan セクション外の判定は「人間確認セクション」の状態管理のみ行う。
            if line.startswith(HUMAN_SECTION_HEADER):
                in_human = True
            elif in_human and line.startswith("## "):
                in_human = False
            continue

        # ここから Test plan セクション内の処理
        if CODEBLOCK_RE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.startswith(HUMAN_SECTION_HEADER):
            in_human = True
            continue
        if in_human and line.startswith("## "):
            in_human = False
        if in_human:
            continue
        m = re.match(r"^[ \t]*- \[ \][ \t]*(.+?)\s*$", line)
        if m:
            result.append(m.group(1))
    return result


def detect_format_errors(body: str) -> list[str]:
    """PR ボディの `## Test plan` セクション内で書式崩れの箇条書き行を検知する（Issue #2468）.

    チェックボックス（`- [ ]` / `- [x]` / `- [X]`）形式でない `- ` 始まりの行を
    「書式崩れ」として返す。コードブロック内・セクション外・`## 人間の確認が必要な項目`
    セクション内の行は除外する。

    Returns:
        書式崩れ行（前後の空白を剥がした文字列）のリスト。正常なら空リスト。
    """
    malformed: list[str] = []
    in_test_plan = False
    in_code = False
    in_human = False
    for line in body.splitlines():
        if TEST_PLAN_SECTION_HEADER_RE.match(line):
            in_test_plan = True
            in_code = False
            in_human = False
            continue
        if in_test_plan and H2_HEADING_RE.match(line):
            in_test_plan = False

        if not in_test_plan:
            if line.startswith(HUMAN_SECTION_HEADER):
                in_human = True
            elif in_human and line.startswith("## "):
                in_human = False
            continue

        # ここから Test plan セクション内の処理
        if CODEBLOCK_RE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.startswith(HUMAN_SECTION_HEADER):
            in_human = True
            continue
        if in_human and line.startswith("## "):
            in_human = False
        if in_human:
            continue

        # `- [ ]` または `- [x]`/`- [X]` にマッチしない `- ` 行を書式崩れとして記録
        if BARE_BULLET_RE.match(line) and not CHECKBOX_RE.match(line) and not CHECKED_CHECKBOX_RE.match(line):
            malformed.append(line.strip())
    return malformed


def classify_items(items: Iterable[str]) -> Classified:
    # prefix 判定（post_merge / ai_confirm / manual）は shared.checklist.classify_checkbox_text()
    # （Issue #2942 で core.py / subcommands.py と統合）に委譲する。jest/pytest/bats/uncovered の
    # 判定は test_plan 固有のためこの関数に残す。
    out = Classified()
    for item in items:
        category = classify_checkbox_text(item)
        if category == "post_merge":
            out.ai_confirm_post_merge_items.append(item)
        elif category == "ai_confirm":
            out.ai_confirm_items.append(item)
        elif category == "manual":
            # [手動]/legacy キーワードは後方互換のため human_items に残すが、exit 4 ブロックはしない
            # （test_plan ではスキップ扱い・merge gate でのブロックは core.py が廃止）
            out.human_items.append(item)
        elif JEST_KEYWORD_RE.search(item):
            out.jest_items.append(item)
        elif PYTEST_KEYWORD_RE.search(item):
            out.pytest_items.append(item)
        elif BATS_KEYWORD_RE.search(item):
            out.auto_items.append(item)
        else:
            out.uncovered_items.append(item)
    return out


# ── diff 検出 ────────────────────────────────────────────────────────────────


def _skip_bats_no_sh_change(changed_files: list[str]) -> bool:
    if not changed_files:
        return False
    targets = [
        f
        for f in changed_files
        if f.endswith(".sh") or f.endswith(".bats") or f.startswith("scripts/") or f.startswith("tests/")
    ]
    if not targets:
        print(
            "==> bats をスキップ（scripts/・tests/ および .sh/.bats ファイルの変更なし: "
            f"{len(changed_files)} ファイル）",
            file=sys.stderr,
        )
        return True
    return False


def _collect_regressions(repo_root: Path, changed_files: list[str]) -> tuple[list[Path], list[str]]:
    pr_files: list[Path] = []
    for f in changed_files:
        if re.match(r"^tests/regressions/.*\.bats$", f):
            full = repo_root / f
            if full.is_file():
                pr_files.append(full)

    skip_existing: list[str] = []
    regressions_dir = repo_root / "tests" / "regressions"
    if regressions_dir.is_dir():
        for existing in sorted(regressions_dir.glob("*.bats")):
            if existing not in pr_files:
                skip_existing.append(existing.name)
    return pr_files, skip_existing


def _compute_target_tags(changed_files: list[str], repo_root: Path | None = None) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    # Issue #1501: `.claude/hooks/**` 変更は sandbox_copier_poc template drift テストを
    # 選択実行して source ↔ template drift を PR gate で早期検出する。
    # `_lib/` サブディレクトリを含む hooks 配下すべてが対象。
    hook_touched = any(f.startswith(".claude/hooks/") for f in changed_files)
    if hook_touched:
        drift_tag = "target:sandbox_copier_poc"
        seen.add(drift_tag)
        tags.append(drift_tag)
    # Issue #1883: CLAUDE.md / `.claude/rules/**` 変更は静的コンテキスト予算テストを
    # 選択実行対象に含める（HARD gate 本体は run() の context_budget.run_gate）。
    # Issue #3643: repo_root 指定時は CLAUDE.md の @import 先変更も同対象。
    if any(context_budget.is_gate_trigger(f, repo_root) for f in changed_files):
        budget_tag = "target:context_budget"
        seen.add(budget_tag)
        tags.append(budget_tag)
    for f in changed_files:
        if f.startswith("tests/regressions/"):
            continue
        base = os.path.basename(f)
        name = base.rsplit(".", 1)[0] if "." in base else base
        tag = f"target:{name}"
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


# ── Jest / pytest 実行 ──────────────────────────────────────────────────────


# Issue #2972: 選択実行不能（複数テストへ影響しうる）と判断する共有ファイルの basename。
_PYTEST_FULL_SUITE_BASENAMES = frozenset({"conftest.py", "pyproject.toml"})


def _requires_full_pytest_suite(changed_files: list[str]) -> bool:
    """target marker 選択実行が不能な共有モジュール変更を検知する（Issue #2972）.

    conftest.py・`shared/` 配下・pyproject.toml の変更は複数テストへ横断的に
    影響しうるため、target marker による絞り込みをせずフルスイートを実行する。
    """
    for f in changed_files:
        if os.path.basename(f) in _PYTEST_FULL_SUITE_BASENAMES:
            return True
        if "shared" in Path(f).parts:
            return True
    return False


def _tag_to_pytest_marker(tag: str) -> str:
    """`_compute_target_tags()` の `target:<name>` タグを pytest marker 名に変換する（Issue #2972）.

    marker 名は識別子として `-m` 式に埋め込むため、既存の marker 命名規約
    （`@pytest.mark.target_<basename>`・ハイフンはアンダースコアに変換）に合わせる。
    """
    name = tag.split(":", 1)[1] if ":" in tag else tag
    return "target_" + name.replace("-", "_")


def _compute_pytest_marker_expr(changed_files: list[str], repo_root: Path | None = None) -> str | None:
    """変更ファイルから pytest `-m` 選択実行式を組み立てる（Issue #2972）.

    共有モジュール変更（`_requires_full_pytest_suite`）を含む場合や tag が
    1件も計算できない場合は `None` を返す。呼び出し側はこれをフルスイート実行の
    合図として扱う。
    """
    if not changed_files:
        return None
    if _requires_full_pytest_suite(changed_files):
        return None
    tags = _compute_target_tags(changed_files, repo_root)
    if not tags:
        return None
    markers = list(dict.fromkeys(_tag_to_pytest_marker(tag) for tag in tags))
    if not markers:
        return None
    return "(" + " or ".join(markers) + ")"


def _run_captured(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    runner: str,
) -> subprocess.CompletedProcess[bytes]:
    """テストランナーを実行し既定では出力をログファイルへキャプチャする（Issue #3426）.

    成功時は 1 行サマリ、失敗時（タイムアウト含む）は末尾 ``TAIL_LINES`` 行 + ログパスを
    stderr に出す。``TIDD_TEST_OUTPUT_VERBOSE=1`` の場合は従来どおり stderr へ素通しする。
    """
    if test_output_capture.verbose_enabled():
        return subprocess.run(  # noqa: S603
            cmd, cwd=cwd, stdout=sys.stderr, stderr=sys.stderr, env=env, timeout=timeout
        )
    log_path = test_output_capture.reserve_log_path(runner)
    start = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as fh:
        try:
            proc = subprocess.run(  # noqa: S603
                cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, env=env, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            test_output_capture.emit_result(
                runner=runner, log_path=log_path, returncode=1, elapsed=time.monotonic() - start
            )
            raise
    test_output_capture.emit_result(
        runner=runner, log_path=log_path, returncode=proc.returncode, elapsed=time.monotonic() - start
    )
    return proc


def _build_jest_subprocess_env() -> dict[str, str]:
    """Jest サブプロセスの環境変数を組み立てる.

    Issue #4143 レビュー指摘: 容量不足フォールバック時に選択した TMPDIR が Jest
    サブプロセスへ渡らず、既定 `/tmp` を使い続けて `ENOSPC` になる問題を修正する
    （`_build_pytest_subprocess_env` と同様に診断結果を反映する）。

    レビュー指摘（PR #4146）: `mark_recursive_subprocess()` は `os.environ` をコピーする
    ため、親プロセスの `TMPDIR` にすでに既定の一時領域（inode 不足等で使えないディレクトリ）
    が設定されている場合、`apply_env()` の no-op だけでは既定 TMPDIR が env に残ったまま
    Jest サブプロセスへ渡ってしまう。`_build_pytest_subprocess_env` と同様に、診断が失敗
    した場合は Jest を起動せず fail-fast する。
    """
    env = mark_recursive_subprocess()
    diagnosis = tmp_capacity.diagnose(stderr=sys.stderr, quiet=True)
    if not diagnosis.ok:
        print(f"ERROR: 一時領域診断に失敗したため Jest を起動しません（{diagnosis.reason}）", file=sys.stderr)
        sys.exit(1)
    tmp_capacity.apply_env(diagnosis, env)
    return env


def _run_jest(ctx: RunContext, changed_files: list[str], jest_items: list[str]) -> bool:
    gas_projects = _detect_projects(changed_files, "projects/gas/")
    if not jest_items and not gas_projects:
        return False
    if not gas_projects:
        if jest_items:
            print(
                "==> WARN: Jest キーワードがありますが PR diff に projects/gas/ の変更が"
                "見つかりません。スキップします。",
                file=sys.stderr,
            )
        return True
    all_passed = True
    for project_dir in gas_projects:
        pkg = ctx.repo_root / project_dir / "package.json"
        if not pkg.is_file():
            print(f"==> スキップ: {ctx.repo_root / project_dir} に package.json がありません", file=sys.stderr)
            continue
        print(f"==> Jest テスト実行中: {project_dir}", file=sys.stderr)
        proc = _run_captured(
            ["npx", "jest"],
            cwd=ctx.repo_root / project_dir,
            env=_build_jest_subprocess_env(),
            timeout=600,
            runner="jest",
        )
        if proc.returncode == 0:
            print(f"==> Jest テスト成功: {project_dir}", file=sys.stderr)
        else:
            print(f"==> Jest テスト失敗: {project_dir}", file=sys.stderr)
            all_passed = False
    if not all_passed:
        print("テスト計画の検証に失敗しました（Jest テストが失敗）", file=sys.stderr)
        sys.exit(1)
    return True


DEFAULT_PYTEST_TIMEOUT_SECS = 1800


def _pytest_timeout_secs() -> int:
    """pytest 実行のタイムアウト秒数を返す（``TIDD_PYTEST_TIMEOUT_SECS`` で上書き可・Issue #2832）."""
    raw = os.environ.get("TIDD_PYTEST_TIMEOUT_SECS")
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value >= 1:
            return value
    return DEFAULT_PYTEST_TIMEOUT_SECS


def _kill_pytest_process_group(proc: subprocess.Popen[bytes]) -> None:
    """`uv run` 越しの孫プロセス（実体の pytest）ごとプロセスグループを停止する（Issue #2832）.

    `os.killpg`/`os.getpgid`/`signal.SIGKILL` は POSIX 専用（Windows には存在せず、
    typeshed のスタブも `sys.platform` ガード配下にのみ定義されているため
    `mypy --strict` が win32 で `[attr-defined]` エラーを出す・Issue #3877）。
    Windows では `start_new_session=True` がプロセスグループ生成を行わない
    （無視される）ため、直接 `proc.kill()` で十分（孫プロセスの停止は #3880 のスコープ）。
    """
    if sys.platform != "win32":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    else:
        proc.kill()
    proc.wait()


def _print_pytest_timeout_guidance(timeout_secs: int) -> None:
    print(
        f"==> pytest テスト打ち切り: {timeout_secs} 秒で終了しなかったため停止しました。"
        "TIDD_PYTEST_TIMEOUT_SECS でタイムアウト秒数を延長できます。",
        file=sys.stderr,
    )


def _build_pytest_subprocess_env() -> MutableMapping[str, str]:
    """pytest サブプロセス（と再帰呼び出しの孫プロセス）の環境変数を組み立てる.

    Issue #2834: basetemp を uv キャッシュと同一ファイルシステムへ置く。
    Issue #4143: 既定 TMPDIR の容量・inode を診断し、容量不足なら repo-local/cache
    配下へ切り替えた TMPDIR を反映する。

    レビュー指摘（PR #4146）: `mark_recursive_subprocess()` は `os.environ` をコピーする
    ため、親プロセスの `TMPDIR` にすでに既定の一時領域（inode 不足等で使えないディレクトリ）
    が設定されている場合、`apply_env()` の no-op だけでは既定 TMPDIR が env に残ったまま
    pytest サブプロセスへ渡ってしまう。診断が失敗した場合は pytest を起動せず fail-fast する
    （`pre_flight` の早期チェックとは独立に、`tidd test-plan` 直接実行等の経路でも同じ保証を
    持たせるため）。
    """
    env = pytest_tmpdir.apply_temproot(mark_recursive_subprocess())
    diagnosis = tmp_capacity.diagnose(stderr=sys.stderr, quiet=True)
    if not diagnosis.ok:
        print(f"ERROR: 一時領域診断に失敗したため pytest を起動しません（{diagnosis.reason}）", file=sys.stderr)
        sys.exit(1)
    tmp_capacity.apply_env(diagnosis, env)
    return env


def _run_pytest_process(cmd: list[str], cwd: Path, timeout_secs: int) -> int:
    """pytest を独立セッションで起動し、タイムアウト時はプロセスグループごと停止する.

    ``uv run`` 越しに起動するため、親だけを kill すると実体の pytest が孤児として
    走り続けて同じ端末に出力を混ぜる（Issue #2832）。既定では出力をログファイルへ
    キャプチャし、成功時はサマリ 1 行・失敗時（タイムアウト含む）は末尾のみを表示する
    （Issue #3426・``TIDD_TEST_OUTPUT_VERBOSE=1`` で従来どおりの素通しに戻せる）。
    """
    env = _build_pytest_subprocess_env()

    if test_output_capture.verbose_enabled():
        proc = subprocess.Popen(  # noqa: S603
            cmd, cwd=cwd, stdout=sys.stderr, stderr=sys.stderr, env=env, start_new_session=True
        )
        try:
            return proc.wait(timeout=timeout_secs)
        except subprocess.TimeoutExpired:
            _kill_pytest_process_group(proc)
            _print_pytest_timeout_guidance(timeout_secs)
            sys.exit(1)

    log_path = test_output_capture.reserve_log_path("pytest")
    start = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen(  # noqa: S603
            cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True
        )
        try:
            returncode = proc.wait(timeout=timeout_secs)
        except subprocess.TimeoutExpired:
            _kill_pytest_process_group(proc)
            returncode = None

    elapsed = time.monotonic() - start
    if returncode is None:
        test_output_capture.emit_result(runner="pytest", log_path=log_path, returncode=1, elapsed=elapsed)
        _print_pytest_timeout_guidance(timeout_secs)
        sys.exit(1)
    test_output_capture.emit_result(runner="pytest", log_path=log_path, returncode=returncode, elapsed=elapsed)
    return returncode


def _build_pytest_cmd(project_path: Path, workers: int, include_slow: bool, marker_expr: str | None) -> list[str]:
    """`uv run pytest` コマンドを組み立てる（Issue #2969 の `not slow` + #2972 の target marker 選択）."""
    cmd = ["uv", "run", "--project", str(project_path), "pytest", "-n", str(workers)]
    marker_parts = []
    if not include_slow:
        marker_parts.append("not slow")
    if marker_expr:
        marker_parts.append(marker_expr)
    if marker_parts:
        cmd.extend(["-m", " and ".join(marker_parts)])
    return cmd


# ── 配布物検証テスト（Issue #3604） ──────────────────────────────────────────
#
# `.claude/**`・`templates/**`・`.rulesync/**` のみを変更する PR は `_detect_projects()` が
# `projects/py/` プレフィックスの変更を検出できず pytest 自体がスキップされる。加えて
# Issue #2969 で `slow` marker 付きテスト（copier E2E 等）は通常の pre-flight から
# 除外されている。直近 nightly 失敗の大半がこの経路（配布物固有パス混入・実行権限・
# skill セット対称差等）だったため、対象パス変更時に限りこのリストのテストを
# `slow` を含めて明示実行する。

DIST_CHECK_PROJECT_DIR = "projects/py/tidd_tools"

# 直近 nightly 失敗ファミリーをカバーするテストのパスリスト（DIST_CHECK_PROJECT_DIR からの相対パス）。
# 保守漏れ検知の考え方は `docs/reference/test-plan-guide.md`「配布物検証テストの選択実行」参照。
DIST_CHECK_TEST_PATHS: tuple[str, ...] = (
    "tests/step_defs/test_issue_2227.py",
    "tests/step_defs/test_issue_2218.py",
    "tests/regressions/test_fix_2243.py",
    "tests/regressions/test_fix_2520.py",
    "tests/test_sandbox_copier_skills.py",
    "tests/step_defs/test_issue_2355.py",
    "tests/step_defs/test_issue_2357.py",
    "tests/step_defs/test_issue_1641.py",
    "tests/test_copier_org_email.py",
    "tests/step_defs/test_issue_3229.py",
    "tests/step_defs/test_issue_2486.py",
    "tests/regressions/test_fix_3763_yaru_verifier_codex_distribution.py",
    "tests/step_defs/test_issue_3937.py",
    "tests/regressions/test_fix_3956_settings_json_conflict_markers.py",
    "tests/step_defs/test_issue_3979.py",
    "tests/step_defs/test_issue_3983.py",
    "tests/test_fix_4084_coderabbit_untrusted_input.py",
    "tests/step_defs/test_issue_4167.py",
)

DIST_CHECK_TRIGGER_PREFIXES = (".claude/", "templates/", ".rulesync/")


def _dist_check_triggered(changed_files: list[str]) -> bool:
    """変更ファイルに配布物検証テスト対象パスが含まれるか判定する（Issue #3604）."""
    return any(f.startswith(DIST_CHECK_TRIGGER_PREFIXES) for f in changed_files)


def _build_dist_check_cmd(project_path: Path, test_files: list[Path]) -> list[str]:
    """配布物検証用 pytest コマンドを組み立てる（Issue #4120）.

    curated テストには copier/uv を起動する slow テストや pytest ロックを検証する
    テストが含まれる。これらを xdist worker 間で並行実行すると、worker の終了通知と
    子プロセス・ロックの解放順序が競合し、pytest が最終サマリを返さないことがある。
    配布物検証は worker を使わず、外側のプロセスグループとロック境界で完了を管理する。
    """
    return [
        "uv",
        "run",
        "--project",
        str(project_path),
        "pytest",
        "-n",
        "0",
        *(str(path) for path in test_files),
    ]


def _run_dist_check_tests(ctx: RunContext) -> PytestOutcome | None:
    """`DIST_CHECK_TEST_PATHS` を `slow` 込みで明示実行する（Issue #3604）.

    フルスイート用の tree hash マーカー（Issue #2449）とは別のマーカーで
    同一 tree hash の再実行をスキップする（`-m "not slow"` のフルスイート成功は
    配布物検証テストの成功を意味しないため）。

    `DIST_CHECK_PROJECT_DIR` に `pyproject.toml`/対象ファイルが存在しない環境
    （本家 tidd_tools プロジェクトを含まない tmp fixture 等）では ``None`` を返し、
    呼び出し元に通常の「py プロジェクト未検出」経路へフォールバックさせる
    （Issue #2778 の既存 skip_reason 契約を壊さないため）。
    """
    if os.environ.get("TIDD_TEST_PLAN_SKIP_DIST_CHECK") == "1":
        print(
            "test-plan: 配布物検証テストを TIDD_TEST_PLAN_SKIP_DIST_CHECK=1 のためスキップします",
            file=sys.stderr,
        )
        return PytestOutcome(passed=True, ran=False, skip_reason="TIDD_TEST_PLAN_SKIP_DIST_CHECK=1")

    tree_hash = ""
    if os.environ.get("AI_REVIEW_SKIP_TEST_PLAN_CACHE") != "1":
        tree_hash = _git_tree_hash(ctx.repo_root)
        if tree_hash and preflight_markers.has_fresh_dist_check_marker(ctx.repo_root, tree_hash):
            print(f"test-plan: skip 配布物検証テスト（マーカー hit・tree={tree_hash}）", file=sys.stderr)
            marker_path, marker_written_at = marker_origin(
                ctx.repo_root, preflight_markers._dist_check_marker_path(ctx.repo_root, tree_hash)
            )
            return PytestOutcome(
                passed=True,
                ran=False,
                skip_reason="マーカー hit",
                marker_path=marker_path,
                marker_written_at=marker_written_at,
            )

    project_path = ctx.repo_root / DIST_CHECK_PROJECT_DIR
    pyproject = project_path / "pyproject.toml"
    if not pyproject.is_file():
        print(f"==> スキップ: {project_path} に pyproject.toml がありません（配布物検証テスト）", file=sys.stderr)
        return None

    test_files = [project_path / p for p in DIST_CHECK_TEST_PATHS if (project_path / p).is_file()]
    if not test_files:
        print("==> 配布物検証テストをスキップ（対象ファイルが見つかりません）", file=sys.stderr)
        return None

    print(
        "==> 配布物検証テスト実行中（.claude/・templates/・.rulesync/ 変更を検出したため slow 込みで実行）",
        file=sys.stderr,
    )
    workers = calc_pytest_workers_from_system(stderr=sys.stderr)
    timeout_secs = _pytest_timeout_secs()
    # 通常の pytest ループは worker 数をメモリ量から算出するが、配布物検証は
    # worker 間の並行実行が終了通知・子プロセス・ロックの競合を起こすため直列化する。
    # 算出結果は診断ログに残し、通常ループとの worker 設計差を明示する。
    print(f"test-plan: 配布物検証の pytest worker 数={workers}（実行は 0 に固定）", file=sys.stderr)
    cmd = _build_dist_check_cmd(project_path, test_files)
    lock_ctx = contextlib.nullcontext() if is_recursive_call() else PytestFlockContext()
    with lock_ctx:
        returncode = _run_pytest_process(cmd, project_path, timeout_secs)
    if returncode != 0:
        print("==> 配布物検証テスト失敗", file=sys.stderr)
        return PytestOutcome(passed=False, ran=True)
    print("==> 配布物検証テスト成功", file=sys.stderr)
    if tree_hash:
        preflight_markers.write_dist_check_marker(ctx.repo_root, tree_hash)
    return PytestOutcome(passed=True, ran=True)


def _maybe_run_dist_check(ctx: RunContext, changed_files: list[str]) -> PytestOutcome | None:
    """必要なら配布物検証テストを実行する（Issue #3604・`_run_pytest` の C901 対応）.

    トリガー対象外なら ``None`` を返す。失敗時は `_run_pytest` を即 `sys.exit(1)` させる。

    `DIST_CHECK_PROJECT_DIR`（`projects/py/tidd_tools`）自体が同時に変更され通常の
    pytest ループでも対象になる場合でも実行する。通常ループは `-m "not slow"` で
    slow marker 付きテストを除外するため、`py_projects` にこのプロジェクトが含まれる
    ことは `DIST_CHECK_TEST_PATHS` 中の slow テストが実行される保証にならない
    （codex レビュー指摘・PR #3607）。
    """
    if not _dist_check_triggered(changed_files):
        return None
    outcome = _run_dist_check_tests(ctx)
    if outcome is not None and not outcome.passed:
        print("テスト計画の検証に失敗しました（配布物検証テストが失敗）", file=sys.stderr)
        sys.exit(1)
    return outcome


def _early_pytest_outcome(
    py_projects: list[str], pytest_items: list[str], dist_check_outcome: PytestOutcome | None
) -> PytestOutcome | None:
    """`py_projects` 未検出時の早期リターンをまとめる（#3187/#3604 の C901 対応）.

    通常の pytest ループを実行すべきときは ``None`` を返す。
    """
    if py_projects:
        return None
    if dist_check_outcome is not None:
        return dist_check_outcome
    if not pytest_items:
        return PytestOutcome(passed=False, ran=False, skip_reason="py プロジェクト未検出")
    print(
        "==> WARN: pytest キーワードがありますが PR diff に projects/py/ の変更が見つかりません。スキップします。",
        file=sys.stderr,
    )
    return PytestOutcome(passed=True, ran=False, skip_reason="py プロジェクト未検出")


def _run_pytest(ctx: RunContext, changed_files: list[str], pytest_items: list[str]) -> PytestOutcome:
    py_projects = _detect_projects(changed_files, "projects/py/")
    dist_check_outcome = _maybe_run_dist_check(ctx, changed_files)
    early_outcome = _early_pytest_outcome(py_projects, pytest_items, dist_check_outcome)
    if early_outcome is not None:
        return early_outcome
    if os.environ.get("AI_REVIEW_SKIP_TEST_PLAN_CACHE") != "1":
        tree_hash = _git_tree_hash(ctx.repo_root)
        if tree_hash and has_fresh_preflight_tree_marker(ctx.repo_root, tree_hash):
            print(f"test-plan: skip pytest（pre-flight マーカー hit・tree={tree_hash}）", file=sys.stderr)
            marker_path, marker_written_at = marker_origin(
                ctx.repo_root, _preflight_tree_marker_path(ctx.repo_root, tree_hash)
            )
            return PytestOutcome(
                passed=True,
                ran=False,
                skip_reason="マーカー hit",
                marker_path=marker_path,
                marker_written_at=marker_written_at,
            )
        sha = _git_head_sha(ctx.repo_root)
        # working tree が HEAD（sha）の内容と一致する場合のみ SHA marker を信頼する（PR #2457
        # codex レビュー指摘）。dirty tree（未コミットの変更あり）で HEAD に古い SHA marker が
        # 残っていると、その marker はいま実際にテストしようとしている内容を検証していないため、
        # 信頼して skip すると呼び出し元（pre_flight.py）が「未検証」を成功と誤認し、dirty tree の
        # tree hash に対して新規 tree marker を書いてしまう（実際にはテストされていない内容が
        # ai-review 側で誤 cache hit する経路を生む）。
        if (
            sha
            and tree_hash
            and tree_hash == _git_commit_tree_hash(ctx.repo_root, sha)
            and has_fresh_preflight_marker(ctx.repo_root, sha)
        ):
            print(f"test-plan: skip pytest（pre-flight マーカー hit・SHA={sha}）", file=sys.stderr)
            marker_path, marker_written_at = marker_origin(ctx.repo_root, _preflight_marker_path(ctx.repo_root, sha))
            return PytestOutcome(
                passed=True,
                ran=False,
                skip_reason="マーカー hit",
                marker_path=marker_path,
                marker_written_at=marker_written_at,
            )
    all_passed = True
    ran_any = False
    for project_dir in py_projects:
        pyproject = ctx.repo_root / project_dir / "pyproject.toml"
        if not pyproject.is_file():
            print(
                f"==> スキップ: {ctx.repo_root / project_dir} に pyproject.toml がありません",
                file=sys.stderr,
            )
            continue
        # vendor 配布された project（`tests/` 非同梱・Issue #3979）に対して pytest を起動すると、
        # `tests/conftest.py` の `pytest_addoption` で登録される ini オプション（`skip_threshold` 等）が
        # 未登録のまま `filterwarnings = ["error", ...]` により INTERNALERROR になる。
        # `tests/` が実在しない project は pytest 対象から除外する（Issue #3991）。
        if not (ctx.repo_root / project_dir / "tests").is_dir():
            print(
                f"==> スキップ: {ctx.repo_root / project_dir} に tests/ ディレクトリがありません"
                "（vendor 配布・Issue #3991）",
                file=sys.stderr,
            )
            continue
        print(f"==> pytest テスト実行中: {project_dir}", file=sys.stderr)
        ran_any = True
        # `uv run --project <project_dir> pytest` で各プロジェクトの dev 依存（pytest）を
        # 解決して実行する。ai-review.sh から呼ばれたとき呼び出し側 venv には pytest が
        # 入っていないため、bare `pytest` だとクリーン環境で FileNotFoundError になる。
        # Issue #2784: `-n auto` の代わりにメモリ量を考慮したワーカー数を使う。
        workers = calc_pytest_workers_from_system(stderr=sys.stderr)
        timeout_secs = _pytest_timeout_secs()
        print(f"pytest タイムアウト: {timeout_secs} 秒", file=sys.stderr)
        # Issue #2787: PytestFlockContext で同時実行を直列化してメモリ枯渇を防ぐ。
        # Issue #2835: 入れ子（親がロックを握ったまま起動した子 pytest）では取り直さない。
        # 祖先が保持済みなので直列化の目的は満たされており、取りにいくと必ず上限まで待つ。
        lock_ctx = contextlib.nullcontext() if is_recursive_call() else PytestFlockContext()
        # Issue #2969: slow marker 付きテスト（copier E2E・mypy フル実行・uv sync 等）を
        # pre-flight/ai-review では除外して実行時間を削減する。
        # TIDD_TEST_PLAN_INCLUDE_SLOW=1 で全件実行（escape hatch）。
        include_slow = os.environ.get("TIDD_TEST_PLAN_INCLUDE_SLOW") == "1"

        # Issue #2972: target marker ベースの選択実行。conftest.py・shared/ 配下・
        # pyproject.toml 等の共有モジュール変更は _compute_pytest_marker_expr が None を
        # 返しフルスイートへフォールバックする。TIDD_TEST_PLAN_FULL_SUITE=1 で常にフル実行
        # する（escape hatch）。
        if os.environ.get("TIDD_TEST_PLAN_FULL_SUITE") == "1":
            print("==> pytest: TIDD_TEST_PLAN_FULL_SUITE=1 のためフル実行します", file=sys.stderr)
            marker_expr = None
        else:
            marker_expr = _compute_pytest_marker_expr(changed_files, ctx.repo_root)
            if marker_expr:
                print(f'==> pytest: 選択実行（-m "{marker_expr}"）', file=sys.stderr)
            else:
                print("==> pytest: 選択実行対象外の変更のためフルスイートを実行します", file=sys.stderr)

        project_path = ctx.repo_root / project_dir
        with lock_ctx:
            returncode = _run_pytest_process(
                _build_pytest_cmd(project_path, workers, include_slow, marker_expr),
                project_path,
                timeout_secs,
            )
            # pytest の「no tests collected」は exit code 5。選択実行で対象テストが
            # 0件の場合はフルスイートへフォールバックし、テストの取りこぼしを防ぐ。
            if marker_expr and returncode == 5:
                print(
                    "==> pytest: 選択実行の対象テストが0件のためフルスイートへフォールバックします",
                    file=sys.stderr,
                )
                returncode = _run_pytest_process(
                    _build_pytest_cmd(project_path, workers, include_slow, None),
                    project_path,
                    timeout_secs,
                )
        if returncode == 0:
            print(f"==> pytest テスト成功: {project_dir}", file=sys.stderr)
        else:
            print(f"==> pytest テスト失敗: {project_dir}", file=sys.stderr)
            all_passed = False
    if not all_passed:
        print("テスト計画の検証に失敗しました（pytest テストが失敗）", file=sys.stderr)
        sys.exit(1)
    if not ran_any:
        return PytestOutcome(passed=True, ran=False, skip_reason="pyproject.toml 未検出")
    return PytestOutcome(passed=True, ran=True)


def _detect_projects(changed_files: list[str], prefix: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for f in changed_files:
        if not f.startswith(prefix):
            continue
        parts = f.split("/")
        if len(parts) >= 3:
            project = "/".join(parts[:3])
            if project not in seen:
                seen.add(project)
                out.append(project)
    return out


# ── bats 実行（並列・タイムアウト・Issue 自動作成） ───────────────────────


def _write_bats_status(ctx: RunContext, status: str) -> None:
    if ctx.state_dir is None:
        return
    (ctx.state_dir / "bats-status").write_text(status + "\n", encoding="utf-8")


def _bats_early_skip_reason(
    *,
    skip_bats_no_sh: bool,
    skip_bats_no_tag: bool,
    bats_cmd: str | None,
    candidate_files: list[Path],
) -> str | None:
    """`_run_bats` の早期スキップ判定をまとめる（#3187 C901 対応）.

    スキップすべき場合はスキップ理由メッセージを返す。実行すべき場合は None を返す。
    """
    if skip_bats_no_sh:
        return "==> bats をスキップ（sh/bats 変更なし：PR ボディのみ更新します）"
    if skip_bats_no_tag:
        return "==> bats をスキップ（タグ対応テストなし：全件は watch-main-tests.sh デイリーで実行）"
    if os.environ.get("SKIP_BATS_IN_TEST") == "1":
        return "==> bats をスキップ（SKIP_BATS_IN_TEST=1: テスト内での再帰実行を抑制）"
    if not bats_cmd:
        return "==> bats をスキップ（bats コマンドが PATH にありません）"
    if not candidate_files:
        return "==> bats をスキップ（tests/*.bats が存在しない）"
    return None


def _run_bats(
    ctx: RunContext,
    *,
    skip_bats_no_sh: bool,
    skip_bats_no_tag: bool,
    bats_filter_files: list[Path] | None,
) -> bool:
    bats_cmd = shutil.which("bats")
    tests_dir = ctx.repo_root / "tests"
    candidate_files: list[Path] = (
        bats_filter_files if bats_filter_files is not None else sorted(tests_dir.glob("*.bats"))
    )
    skip_reason = _bats_early_skip_reason(
        skip_bats_no_sh=skip_bats_no_sh,
        skip_bats_no_tag=skip_bats_no_tag,
        bats_cmd=bats_cmd,
        candidate_files=candidate_files,
    )
    if skip_reason is not None:
        print(skip_reason, file=sys.stderr)
        _write_bats_status(ctx, "skipped")
        return True
    assert bats_cmd is not None  # _bats_early_skip_reason で not bats_cmd を判定済み

    per_file_timeout = int(os.environ.get("BATS_FILE_TIMEOUT_SECS", "300"))
    suite_timeout = int(os.environ.get("BATS_SUITE_TIMEOUT_SECS", "600"))
    suite_start = time.monotonic()

    os.environ.setdefault("BATS_TEST_TIMEOUT", "30")
    env = mark_recursive_subprocess()
    # bats プロセス内から ai-review.sh などが呼ばれた場合、その先で tidd_tools を再帰実行しないように
    # _TIDD_TOOLS_RECURSION_GUARD=1 を環境に注入する（旧 _AI_REVIEW_RUNNING の後継）。
    # _AI_REVIEW_RUNNING は旧 sh スクリプト互換のためサブプロセスには伝播させない。
    env.pop("_AI_REVIEW_RUNNING", None)

    print(
        f"==> bats を直列実行します（{len(candidate_files)} ファイル, スイートタイムアウト {suite_timeout} 秒）...",
        file=sys.stderr,
    )

    timeout_files: list[Path] = []
    failed_files: list[Path] = []
    for f in candidate_files:
        elapsed = int(time.monotonic() - suite_start)
        remaining = suite_timeout - elapsed
        if remaining <= 0:
            _emit_suite_timeout(ctx, suite_timeout, f"PR ブランチ（serial pre-check, {elapsed}秒経過）")
            return False  # _emit_suite_timeout が sys.exit する
        if remaining <= per_file_timeout:
            file_budget = remaining
            cap_applied = True
        else:
            file_budget = per_file_timeout
            cap_applied = False
        try:
            proc = _run_captured(
                [bats_cmd, "--tap", str(f)],
                cwd=ctx.repo_root,
                env=env,
                timeout=file_budget,
                runner="bats",
            )
            if proc.returncode != 0:
                failed_files.append(f)
        except subprocess.TimeoutExpired:
            if cap_applied:
                elapsed = int(time.monotonic() - suite_start)
                _emit_suite_timeout(ctx, suite_timeout, f"PR ブランチ（serial budget cap, {elapsed}秒経過）")
                return False
            timeout_files.append(f)

    # タイムアウト Issue 起票
    for tf in timeout_files:
        _create_timeout_issue(ctx, tf, per_file_timeout)

    if failed_files:
        print("テスト計画の検証に失敗しました（bats tests/*.bats のうち以下が失敗）", file=sys.stderr)
        for ff in failed_files:
            print(f"  - {ff.name}", file=sys.stderr)
        _write_bats_status(ctx, "failed")
        sys.exit(1)

    if timeout_files:
        print(
            f"==> bats tests/*.bats 通過（タイムアウト {len(timeout_files)} 件はスキップして Issue 化）",
            file=sys.stderr,
        )
    else:
        print("==> bats tests/*.bats 通過", file=sys.stderr)
    _write_bats_status(ctx, "passed")
    return True


def _emit_suite_timeout(ctx: RunContext, suite_timeout: int, context: str) -> None:
    limit_mins = suite_timeout // 60
    print(
        f"bats タイムアウト：{limit_mins}分（{suite_timeout}秒）を超えたため中断しました"
        f"（context={context}, limit={suite_timeout}秒）",
        file=sys.stderr,
    )
    _write_bats_status(ctx, "failed")
    sys.exit(1)


def _create_timeout_issue(ctx: RunContext, bats_file: Path, file_timeout: int) -> None:
    basename = bats_file.name
    title = f"🤖 fix: {basename} がタイムアウトした"
    if gh.existing_open_issue(title, ctx.repo):
        print(f"==> {basename}: タイムアウト Issue は既に存在するためスキップします", file=sys.stderr)
        return
    body = f"""## 背景

`tidd_tools test-plan` の bats 実行で `{basename}` が {file_timeout} 秒のタイムアウトに達してハングした
（PR #{ctx.pr_num}）。
このファイルはタイムアウトしたため今回の PR では結果集計から除外されたが、原因を特定して修正する必要がある。

## やること

- [ ] `{basename}` の遅い・ハングする原因を特定する
- [ ] 必要に応じてテストを分割するか、外部依存をモックする
- [ ] `tests/regressions/` への移動を検討する

## 振る舞い

Feature: {basename} のタイムアウト解消

  Scenario: テストが {file_timeout} 秒以内に完了する
    Given tidd_tools test-plan が bats テストを実行する
    When {basename} が実行される
    Then {file_timeout} 秒以内に終了する（exit 0 または exit 1）
"""
    try:
        gh.create_issue(title, body, ["type: fix", "priority: medium"], ctx.repo)
    except gh.GhError as exc:
        print(f"WARN: {basename} のタイムアウト Issue 作成に失敗しました: {exc}", file=sys.stderr)


def _run_pr_regressions(ctx: RunContext, pr_regressions: list[Path]) -> bool:
    if not pr_regressions:
        return True
    if os.environ.get("SKIP_BATS_IN_TEST") == "1":
        return True
    bats_cmd = shutil.which("bats")
    if not bats_cmd:
        print("==> PR 追加 regression をスキップ（bats が PATH にない）", file=sys.stderr)
        return True
    suite_timeout = int(os.environ.get("BATS_SUITE_TIMEOUT_SECS", "600"))
    print(
        f"==> PR diff の regression テストを明示パスで実行します（{len(pr_regressions)} 件, "
        f"全体スイート予算 {suite_timeout} 秒）...",
        file=sys.stderr,
    )
    env = mark_recursive_subprocess()
    env.pop("_AI_REVIEW_RUNNING", None)
    try:
        proc = _run_captured(
            [bats_cmd, "--tap", *[str(p) for p in pr_regressions]],
            cwd=ctx.repo_root,
            env=env,
            timeout=suite_timeout,
            runner="bats",
        )
    except subprocess.TimeoutExpired:
        limit_mins = suite_timeout // 60
        print(
            f"bats タイムアウト：{limit_mins}分（{suite_timeout}秒）を超えたため中断しました"
            "（context=PR 追加 regression）",
            file=sys.stderr,
        )
        _write_bats_status(ctx, "failed")
        return False
    if proc.returncode != 0:
        print("テスト計画の検証に失敗しました（PR 追加の regression テストが失敗）", file=sys.stderr)
        _write_bats_status(ctx, "failed")
        return False
    print(f"==> PR 追加 regression テスト通過（{len(pr_regressions)} 件）", file=sys.stderr)
    return True


def _emit_skip_regressions_log(ctx: RunContext, skipped: list[str], skip_bats_no_sh: bool) -> None:
    if not skipped or skip_bats_no_sh:
        return
    skip_count = len(skipped)
    env_name = os.environ.get("HOSTNAME") or socket.gethostname() or "unknown"
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(
        f"==> {skip_count} 件の既存 regression をスキップしました（デイリー実行に委ねます）",
        file=sys.stderr,
    )
    jsonl_log_path = Path(
        os.environ.get("REGRESSION_SKIP_LOG") or str(_cache_dir() / "ai-reviewer" / "regression-skip-log.jsonl")
    )
    jsonl_log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": ts,
        "pr": ctx.pr_num,
        "env": env_name,
        "skipped_files": skipped,
    }
    with jsonl_log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"==> スキップ記録を {jsonl_log_path} に追記しました", file=sys.stderr)

    if ctx.dry_run:
        return
    file_list = "\n".join(f"- {sf}" for sf in skipped)
    comment = (
        f"{skip_count} 件の regression をスキップ（デイリーで実行）\n\n"
        "以下のファイルは PR 変更に含まれないため、今回の PR フローではスキップしました。\n"
        "main ブランチでのデイリー実行（`watch-main-tests.sh`）で定期的に検証されます。\n\n"
        f"{file_list}\n\n"
        "---\n"
        f"*`tidd_tools test-plan` によって記録されました（env: {env_name}）*"
    )
    try:
        gh.comment_pr(ctx.pr_num, ctx.repo, comment)
    except gh.GhError as exc:
        print(f"WARN: PR コメントの投稿に失敗しました: {exc}", file=sys.stderr)


# ── Issue #1464: .feature ファイル生成必須化 ────────────────────────────────


_FEATURE_REQUIRED_PATH_RE = re.compile(r"^projects/py/[^/]+/src/|^projects/gas/[^/]+/(?!tests/)|^\.claude/hooks/[^_]")
_FEATURE_EXEMPT_PATH_RE = re.compile(
    r"^(docs/|README\.md$|CLAUDE\.md$|.gitignore$|.circleci/|.github/|\.claude/rules/|\.claude/commands/|\.claude/skills/)"
)
# Issue #1855: hook 契約系 PR 判定用（REQUIRED ファイルが全て hooks なら .feature 不要）
_HOOK_PATH_RE = re.compile(r"^\.claude/hooks/")
# Issue #1962: tests/test_*.py 判定用（step_defs/ サブディレクトリは除く）
_TESTS_TEST_PY_RE = re.compile(r"^projects/py/[^/]+/tests/test_[^/]+\.py$")
# Issue #4078: `projects/py/<project>/` の project 名抽出用
_PROJECT_PATH_RE = re.compile(r"^projects/py/([^/]+)/")
# Issue #4078: 変更ファイルから対象 project を解決できない場合のフォールバック
# （旧来の tidd_tools 固定挙動を維持する。gas/hooks のみの変更等が該当）
_DEFAULT_FEATURE_PROJECT = "tidd_tools"


def _resolve_projects_for_changed_files(changed_files: list[str]) -> list[str]:
    """変更ファイルから `.feature` / step_defs の期待パス解決に使う project 名を求める（Issue #4078）.

    `projects/py/<project>/` にマッチするファイルから project 名を出現順・重複無しで
    抽出する。1 件も見つからない場合（`projects/gas/`・`.claude/hooks/` のみの変更等）は
    `[_DEFAULT_FEATURE_PROJECT]`（従来の `tidd_tools` 固定）を返す。

    複数 project が混在する場合は全件を返す。呼び出し側は返された project を順に試し、
    いずれか 1 つでも `.feature` + step_defs が揃えば通過させる（設計の選択肢 A・#4078）。
    """
    projects: list[str] = []
    for f in changed_files:
        match = _PROJECT_PATH_RE.match(f)
        if match and match.group(1) not in projects:
            projects.append(match.group(1))
    return projects or [_DEFAULT_FEATURE_PROJECT]


def _extract_issue_numbers_from_pr_body(pr_body: str) -> list[int]:
    """PR ボディから `closes #N` / `fixes #N` / `resolves #N` の Issue 番号を抽出する.

    Issue #2943: closes 抽出は shared/issue_body.py へ集約（旧実装は ``\\b`` なしだったが
    他 4 箇所の厳密側に統一）。
    """
    return extract_closes_issues(pr_body or "")


def _feature_required_for_files(changed_files: list[str]) -> bool:
    """変更ファイルに feat/fix で `.feature` 生成が必須なパスが含まれるか判定する.

    必須対象: `projects/py/*/src/`・`projects/gas/*/`（tests 除く）・`.claude/hooks/[^_]`
    対象外: `docs/`・`README.md`・`.claude/rules/`・`.circleci/`・`.github/`
    判定: 対象外ファイルを除外後、1 件でも必須対象があれば True
    """
    for f in changed_files:
        if _FEATURE_EXEMPT_PATH_RE.match(f):
            continue
        if _FEATURE_REQUIRED_PATH_RE.match(f):
            return True
    return False


def _check_feature_file_required(ctx: RunContext, changed_files: list[str]) -> str | None:
    """Issue #1464 + #1550 + #1962: feat/fix PR のテストカバレッジを検証する.

    Returns:
        `None` : PR は必須対象外 or テストカバレッジが十分
        エラー文字列: テストカバレッジが不足している feat/fix PR

    強制の条件（AND）:
    1. PR title の prefix が `feat` または `fix` （その他は対象外）
    2. 変更ファイルに `_FEATURE_REQUIRED_PATH_RE` にマッチするものが 1 つ以上
    3. 以下のいずれかでカバレッジが十分と判定:
       a. PR ボディから `closes #N` で抽出した Issue 番号のいずれかで
          `.feature` + `step_defs` skeleton の両方が揃う（BDD パス）
       b. PR 変更ファイルに `tests/test_*.py` が含まれる（通常 pytest パス）（Issue #1962）
    4. #1550: `.feature` が存在する Issue には対応する
       `projects/py/<project>/tests/step_defs/test_issue_<N>.py` も存在する

    Issue #4078: 期待パスの `<project>` は `changed_files` から
    `_resolve_projects_for_changed_files()` で解決する（旧: `tidd_tools` 固定）。
    複数 project が変更に混在する場合は、いずれか 1 つで `.feature` + step_defs が
    揃えば通過させる。
    """
    if not changed_files:
        return None

    # PR title の type: を取得
    try:
        pr_data = gh.pr_view(ctx.pr_num, repo=ctx.repo, fields=("title", "body"))
    except Exception:  # noqa: BLE001 - fail-open
        return None
    title = str(pr_data.get("title") or "")
    body = str(pr_data.get("body") or "")

    # feat/fix でなければ強制対象外
    if not re.match(r"^(feat|fix)(\([^)]*\))?:", title):
        return None

    # 変更ファイルが「対象外パスのみ」なら強制対象外
    if not _feature_required_for_files(changed_files):
        return None

    # Issue #1855: 全 REQUIRED ファイルが .claude/hooks/ のみなら hook 契約系 PR → .feature 不要
    # （hooks は test_*.py 契約テストで担保。src/ 等が混在する場合は通常の .feature 必須化を適用）
    required_files = [f for f in changed_files if _FEATURE_REQUIRED_PATH_RE.match(f)]
    if required_files and all(_HOOK_PATH_RE.match(f) for f in required_files):
        return None

    # closes #N から Issue 番号を抽出
    issue_numbers = _extract_issue_numbers_from_pr_body(body)
    if not issue_numbers:
        return None  # closes 参照がなければ強制しない（他 hook が別途 check）

    # Issue #4078: 変更ファイルから対象 project（複数可）を解決する
    candidate_projects = _resolve_projects_for_changed_files(changed_files)

    # PR の changed_files セット（disk 未存在でも PR 内追加を検知するため）
    changed_files_set = set(changed_files)

    # `.feature` が存在する Issue の番号と、それが見つかった project を集める
    # （skeleton 検査の対象母集団）
    # - disk 上に存在する場合（既存 PR 以降のファイル）
    # - または今回の PR で新規追加された場合（PR diff に含まれる）
    issues_with_feature: list[int] = []
    feature_project_by_issue: dict[int, str] = {}
    for project in candidate_projects:
        features_dir = ctx.repo_root / "projects" / "py" / project / "tests" / "features"
        for issue_num in issue_numbers:
            if issue_num in feature_project_by_issue:
                continue
            feature_rel = f"projects/py/{project}/tests/features/issue-{issue_num}.feature"
            if (features_dir / f"issue-{issue_num}.feature").is_file() or feature_rel in changed_files_set:
                issues_with_feature.append(issue_num)
                feature_project_by_issue[issue_num] = project

    if not issues_with_feature:
        # Issue #1962: PR 変更ファイルに tests/test_*.py があれば .feature 不要
        if any(_TESTS_TEST_PY_RE.match(f) for f in changed_files):
            return None
        # .feature も test_*.py もない: ブロック（Issue #1962 新エラーメッセージ）
        primary_project = candidate_projects[0]
        return (
            f"ERROR: feat/fix PR には .feature または test_*.py のどちらかが必要です (Issue #1962)\n"
            f"       closes #{', #'.join(str(n) for n in issue_numbers)} に対応する "
            f".feature ファイル生成が必須です。または PR 変更ファイルに tests/test_*.py を含めてください。\n"
            f"       期待パス: projects/py/{primary_project}/tests/features/issue-<N>.feature\n"
            f"       生成コマンド: `tidd extract-feature <N>`\n"
        )

    # #1550: `.feature` が存在する Issue のいずれかで対応 skeleton も存在するかを確認
    # - disk 上に存在する場合 または PR 内で追加された場合
    for issue_num in issues_with_feature:
        project = feature_project_by_issue[issue_num]
        step_defs_dir = ctx.repo_root / "projects" / "py" / project / "tests" / "step_defs"
        step_defs_rel = f"projects/py/{project}/tests/step_defs/test_issue_{issue_num}.py"
        if (step_defs_dir / f"test_issue_{issue_num}.py").is_file() or step_defs_rel in changed_files_set:
            return None  # 1 つでも両方揃えば OK

    # `.feature` はあるが対応 skeleton が全て欠落
    primary_issue = issues_with_feature[0]
    primary_project = feature_project_by_issue[primary_issue]
    return (
        f"ERROR: feat/fix PR には step_defs skeleton 生成が必須です (Issue #1550)\n"
        f"       closes #{', #'.join(str(n) for n in issues_with_feature)} の "
        f".feature に対応する step_defs skeleton が見つかりません。\n"
        f"       期待パス: projects/py/{primary_project}/tests/step_defs/test_issue_<N>.py\n"
        f"       生成コマンド: `tidd extract-feature {primary_issue}` "
        f"（--force で既存を上書き）\n"
    )


# ── Issue #2000: step_defs xfail 残存チェック ────────────────────────────────

# `tests/step_defs/` 直下の .py のみ対象（regressions/ 等の xfail 許容は維持）
_STEP_DEFS_PATH_RE = re.compile(r"(?:^|/)tests/step_defs/[^/]+\.py$")
# protect-tests.py（#1293）と同系のパターン。extract-feature 生成の
# `pytest.xfail(...)` 呼び出し・デコレータ・pytestmark 代入の 3 形式を検知する。
# 行頭（インデント許容）アンカーで、step title 等の文字列リテラル内の言及は誤検知しない。
_XFAIL_MARKER_RE = re.compile(
    r"^[ \t]*@pytest\.mark\.xfail" r"|^[ \t]*pytest\.xfail\s*\(" r"|^[ \t]*pytestmark\s*=\s*pytest\.mark\.xfail",
    re.MULTILINE,
)


def _check_step_defs_xfail(ctx: RunContext, changed_files: list[str]) -> str | None:
    """PR 変更ファイル中の `tests/step_defs/*.py` に xfail marker が残っていないか検証する.

    xfail 付き skeleton は pytest で xfailed（exit 0）となり commit status も
    GREEN になるため、振る舞いテストが実質無効のままマージされる品質ホールになる
    （Issue #2000）。残存を検出したらエラー文字列を返す（呼び出し側が exit 1）。

    Returns:
        `None` : xfail 残存なし（PR で削除されたファイルはスキップ）
        エラー文字列: xfail marker が残った step_defs ファイルがある
    """
    offending: list[str] = []
    for f in changed_files:
        if not _STEP_DEFS_PATH_RE.search(f):
            continue
        full = ctx.repo_root / f
        if not full.is_file():
            continue  # PR で削除・リネームされたファイル
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _XFAIL_MARKER_RE.search(content):
            offending.append(f)
    if not offending:
        return None
    lines = [
        "ERROR: step_defs に xfail marker が残っています (Issue #2000)",
        "       xfail のままでは振る舞いテストが実質無効のためマージできません。",
        "       xfail を外して pytest GREEN を確認してから push してください。",
        "",
    ]
    lines.extend(f"  - {f}" for f in offending)
    return "\n".join(lines)


# ── 未カバー項目 ─────────────────────────────────────────────────────────────


def _emit_uncovered_error(items: list[str]) -> None:
    """未カバー項目検出時の stderr 出力（Issue #3430: 短縮版）.

    定型の「対処方法」ガイド全文（書き換え例の列挙）は
    docs/reference/test-plan-guide.md へ移設済み。LLM がコンテキストへ
    毎回読み込む必要がないよう、ERROR サマリ 1 行 + 項目一覧 + 参照 1 行に絞る。
    """
    print("", file=sys.stderr)
    print(
        f"ERROR: 未カバーのテスト計画項目が {len(items)} 件あります"
        "（自動検証・[AI確認]・[AI確認-post-merge] のいずれにも未分類）。",
        file=sys.stderr,
    )
    for item in items:
        print(f"  - [ ] {item}", file=sys.stderr)
    print(
        "対処方法: docs/reference/test-plan-guide.md「未カバー項目の対処方法」を参照してください。",
        file=sys.stderr,
    )


# ── PR ボディ更新 ────────────────────────────────────────────────────────────


def _update_pr_body(
    body: str,
    *,
    classified: Classified,
    auto_passed: bool,
    jest_passed: bool,
    pytest_passed: bool,
) -> str:
    updated = body
    if auto_passed and classified.auto_items:
        updated = _mark_items_done(updated, classified.auto_items)
    if jest_passed and classified.jest_items:
        updated = _mark_items_done(updated, classified.jest_items)
    if pytest_passed and classified.pytest_items:
        updated = _mark_items_done(updated, classified.pytest_items)

    updated = _remove_human_section(updated)
    if classified.human_items:
        updated = _remove_inline_human_items(updated, classified.human_items)
        updated = _append_human_section(updated, classified.human_items)
    return updated


def _join_preserving_trailing(body: str, lines: list[str]) -> str:
    out = "\n".join(lines)
    if body.endswith("\n"):
        out += "\n"
    return out


def _mark_items_done(body: str, items: list[str]) -> str:
    target = set(items)
    out_lines = []
    for line in body.splitlines():
        m = re.match(r"^([ \t]*- )\[ \]([ \t]*)(.+?)\s*$", line)
        if m and m.group(3) in target:
            out_lines.append(f"{m.group(1)}[x]{m.group(2)}{m.group(3)}")
        else:
            out_lines.append(line)
    return _join_preserving_trailing(body, out_lines)


def _remove_human_section(body: str) -> str:
    out: list[str] = []
    skip = False
    for line in body.splitlines():
        if line.startswith(HUMAN_SECTION_HEADER):
            skip = True
            continue
        if skip and line.startswith("## "):
            skip = False
        if not skip:
            out.append(line)
    return _join_preserving_trailing(body, out)


def _remove_inline_human_items(body: str, human_items: list[str]) -> str:
    target = set(human_items)
    out: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^[ \t]*- \[ \][ \t]*(.+?)\s*$", line)
        if m and m.group(1) in target:
            continue
        out.append(line)
    return _join_preserving_trailing(body, out)


def _append_human_section(body: str, human_items: list[str]) -> str:
    lines = [f"- [ ] {item}" for item in human_items]
    section = "\n".join(lines)
    suffix = f"\n{HUMAN_SECTION_HEADER}\n\n{section}\n---\n*tidd_tools test-plan によって生成されました*"
    if not body.endswith("\n"):
        body = body + "\n"
    return body + suffix


# ── 出力 ─────────────────────────────────────────────────────────────────────


def _print_summary(classified: Classified) -> None:
    total_auto = len(classified.auto_items) + len(classified.jest_items) + len(classified.pytest_items)
    print(
        f"==> テスト計画チェック完了（bats: {len(classified.auto_items)} 件、"
        f"Jest: {len(classified.jest_items)} 件、pytest: {len(classified.pytest_items)} 件 = "
        f"計 {total_auto} 件チェック済み、AI確認 {len(classified.ai_confirm_items)} 件、"
        f"AI確認-post-merge {len(classified.ai_confirm_post_merge_items)} 件、"
        f"人間確認 {len(classified.human_items)} 件をPRボディに記載）",
        file=sys.stderr,
    )


# ── pre-flight pytest マーカーキャッシュ（Issue #2949 で preflight_markers.py へ移設） ──
#
# 実装は `preflight_markers.py` にあり、ここでは後方互換のため re-export する
# （既存呼び出し元・既存テストが `test_plan.<name>` 形式で参照し続けられるようにする）。
_preflight_marker_path = preflight_markers._preflight_marker_path
has_fresh_preflight_marker = preflight_markers.has_fresh_preflight_marker
write_preflight_pytest_marker = preflight_markers.write_preflight_pytest_marker
_git_head_sha = preflight_markers._git_head_sha
_preflight_tree_marker_path = preflight_markers._preflight_tree_marker_path
has_fresh_preflight_tree_marker = preflight_markers.has_fresh_preflight_tree_marker
write_preflight_pytest_tree_marker = preflight_markers.write_preflight_pytest_tree_marker
_git_tree_hash = preflight_markers._git_tree_hash
_git_commit_tree_hash = preflight_markers._git_commit_tree_hash
marker_origin = preflight_markers.marker_origin


# ── 補助 ────────────────────────────────────────────────────────────────────


def _resolve_repo_root() -> Path:
    """環境変数 TEST_REPO_ROOT があればそれを優先、なければ git rev-parse から取得する."""
    override = os.environ.get("TEST_REPO_ROOT")
    if override:
        return Path(override).resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=30,
        )
        return Path(out.stdout.strip()).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd().resolve()
