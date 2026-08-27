"""`tidd sandbox-copier-poc` サブコマンド.

リポジトリ root（`copier.yml` + `_subdirectory: templates/workflow`。#2231）を
空の git リポジトリに `copier copy` して、
配布されたファイル一覧・`.copier-answers.yml` の内容を検証する PoC ハーネス。

Phase 1 の受け入れ検証（#1206 の Scenario 1）を CI・ローカルの両方から
再現可能にするために提供する。

使い方:
    tidd sandbox-copier-poc                       # 一時ディレクトリで実行
    tidd sandbox-copier-poc --dest /tmp/consumer  # 明示的な出力先
    tidd sandbox-copier-poc --keep                # 実行後に一時ディレクトリを削除しない

終了コード:
- 0 → 全チェックがパスした
- 1 → チェック失敗（詳細は stderr）
- 2 → 前提不備（copier CLI 未インストール等）
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

import yaml

from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.venv_repair import REQUIRED_MAJOR, REQUIRED_MINOR

REQUIRED_FILES = (
    # Phase 1: rules
    # chezmoi.md / screenshot.md は #1919（#1888）で templates/workflow から削除されたため除外
    ".claude/rules/workflow.md",
    ".claude/rules/testing-framework.md",
    ".claude/rules/issue-creation.md",
    ".claude/rules/implementation-constraints.md",
    # rules: YAML 設定ファイル（#2205 で追記）
    ".claude/rules/context-budget.yaml",
    ".claude/rules/gherkin-forbidden-words.yaml",
    ".claude/rules/gherkin-required-markers.yaml",
    ".claude/rules/hardcoded-patterns.yaml",
    ".claude/rules/hook-groups.yaml",
    # #2561: 依存パッケージ許可リスト
    ".claude/rules/dependency-allowlist.yaml",
    # #3498: npm-cooldown ゲートの既知パッケージ許可リスト
    ".claude/rules/npm-cooldown-allowlist.yaml",
    ".claude/rules/manual-check-keywords.yaml",
    ".claude/rules/review-prompt.yaml",
    ".claude/rules/rule-bloat-keywords.yaml",
    # rules: Markdown ファイル（#2205 で追記）
    ".claude/rules/decision-journal.md",
    ".claude/rules/escalation-format.md",
    ".claude/rules/review-backends.md",
    ".claude/rules/test-plan-checklist.md",
    ".claude/rules/tool-calling.md",
    # Phase 2: hooks + _lib
    ".claude/hooks/require-issue.py",
    ".claude/hooks/validate-issue.py",
    ".claude/hooks/ban-shell-files.py",
    # #3559: md/toml への `issue-next-timing mark` 指示書き戻しをブロックする hook
    ".claude/hooks/ban-timing-mark-instruction.py",
    ".claude/hooks/ban-hardcoded-repo.py",
    ".claude/hooks/ban-claude-p.py",
    ".claude/hooks/block-dangerous-git.py",
    ".claude/hooks/block-subagent-review-merge.py",
    # #3992: subagent による allow-large-pr / allow-xxl マーカーの自己付与をブロックする hook
    ".claude/hooks/block-subagent-size-marker.py",
    # #3421: ai-review のバックグラウンド実行（run_in_background / nohup / 末尾 `&`）をブロックする hook
    ".claude/hooks/block-background-ai-review.py",
    # #3629: exit 3 の証跡なしでの ai-fallback-reviewer 起動をブロックする hook
    ".claude/hooks/block-unauthorized-fallback-review.py",
    # #3633: unattended モード中の AskUserQuestion（人間エスカレーション）をブロックする hook
    ".claude/hooks/block-unattended-escalation.py",
    # #2780: WSL2 空きメモリ枯渇事前検知 hook（/proc/meminfo 不存在環境では素通り）
    ".claude/hooks/memguard.py",
    # #2561: pyproject.toml への許可リスト外依存追加をブロックする hook
    ".claude/hooks/check-dependency-allowlist.py",
    ".claude/hooks/label-pr.py",
    ".claude/hooks/on-stop.py",
    # #2752: merge-summary marker への session_id 記録 hook
    ".claude/hooks/stamp-merge-summary-session.py",
    # #3779: issue-next state への session_id 記録 hook（require-issue-next-completion.py の
    # 別セッション誤ブロック対策）
    ".claude/hooks/stamp-issue-next-session.py",
    # #4050: ScheduleWakeup 呼び出し直後に待機予定を記録する hook（require-issue-next-completion.py の
    # 待機時間無視対策）
    ".claude/hooks/stamp-schedule-wakeup.py",
    ".claude/hooks/require-issue-next-completion.py",
    ".claude/hooks/protect-tests.py",
    ".claude/hooks/require-main-sync.py",
    ".claude/hooks/analyze-loop-on-stop.py",
    # #1224: Phase 7 の Actions 撤去に伴い SessionStart hook で staleness を通知する
    ".claude/hooks/notify-copier-staleness.py",
    # hooks: optional 有効化対象 hook（#2205 で追記）
    ".claude/hooks/auto-ruff-format.py",
    # #3085: 編集時にdiff累積行数の500/1000行閾値横断を警告するPostToolUse hook
    ".claude/hooks/warn-diff-size.py",
    ".claude/hooks/auto-tick-issue-items.py",
    ".claude/hooks/ban-anthropic-import.py",
    ".claude/hooks/ban-ng-words.py",
    ".claude/hooks/ban-parents-n.py",
    # #2912: Issue body 直接編集による やること tick ガード
    ".claude/hooks/block-direct-yaru-tick.py",
    ".claude/hooks/detect-ai-confirm-misuse.py",
    ".claude/hooks/detect-rule-bloat.py",
    ".claude/hooks/require-issue-id-in-pr-title.py",
    # #3420: PR 本文に closes #N が無い場合ブロックする hook
    ".claude/hooks/require-closes-in-pr-body.py",
    # #3425: PR 作成時に step_defs の xfail 残存をブロックする hook
    ".claude/hooks/require-no-xfail-stepdefs.py",
    ".claude/hooks/require-merge-ci-status.py",
    ".claude/hooks/require-mypy.py",
    ".claude/hooks/require-preflight-marker.py",
    # #4038: gh pr edit/merge/close 実行前に action log とのdriftを検知する hook
    ".claude/hooks/require-pr-state-drift-check.py",
    ".claude/hooks/require-red-first.py",
    ".claude/hooks/require-ruff-format.py",
    # #3423: 新規 pytest ファイルの @pytest.mark.target_ 欠落防止 hook
    ".claude/hooks/require-target-marker.py",
    # #3445: worktree 未使用の直接実装（main チェックアウト編集）をブロックする hook
    ".claude/hooks/require-worktree-for-edit.py",
    ".claude/hooks/require-subagent-prompt-contract.py",
    ".claude/hooks/require-quality-check.py",
    # #3993: size_over_1000_possible: true の Issue で分割検討根拠なしの issue-implementer 起動を防止
    ".claude/hooks/require-split-consideration.py",
    # #4218: ai-reviewer subagent 起動 prompt に worktree 絶対パス明示を機械強制する hook
    ".claude/hooks/require-ai-reviewer-worktree-path.py",
    # #3761: consumer worktree の Python venv を SessionStart で初期化する hook
    ".claude/hooks/session-start-venv-init.py",
    # #3160: 計測境界のタイムスタンプを機械記録する hook
    ".claude/hooks/record-timing-boundaries.py",
    ".claude/hooks/require-yaru-consistency.py",
    ".claude/hooks/session-start-cache.py",
    # #3659: MEMORY.md est tokens 超過を SessionStart で WARN する hook
    ".claude/hooks/session-start-memory-warn.py",
    # #3700: CLAUDE.local.md 等のサイズ超過を SessionStart で WARN/BLOCK する hook
    ".claude/hooks/session-start-claude-local-md-warn.py",
    # #1965: 未消化 post-merge 検証を SessionStart で catch-up する hook
    ".claude/hooks/session-start-verify-post-merge.py",
    # #3398: PR マージ後の不要ブランチを自動掃除する PostToolUse hook
    ".claude/hooks/sweep-merged-branches.py",
    ".claude/hooks/validate-skill.py",
    ".claude/hooks/_lib/__init__.py",
    ".claude/hooks/_lib/hardcoded_repo.py",
    ".claude/hooks/_lib/hook_io.py",
    # hooks/_lib: 追加ライブラリ（#2205 で追記）
    ".claude/hooks/_lib/bypass_audit.py",
    ".claude/hooks/_lib/gh_cache.py",
    ".claude/hooks/_lib/gh_cache_refresh.py",
    ".claude/hooks/_lib/override_markers.py",
    ".claude/hooks/_lib/session_detector.py",
    ".claude/hooks/_lib/skill_lint.py",
    ".claude/hooks/_lib/validate_payload.py",
    # hooks/_lib: heredoc パース共通ユーティリティ（#2951 で追記）
    ".claude/hooks/_lib/shell_parse.py",
    # hooks/_lib: やることセクション解析・issue body 取得共通ユーティリティ（#2953 で追記）
    ".claude/hooks/_lib/yaru_sections.py",
    # hooks/_lib: TDD RED-first 順序判定ロジックの単一の真実源（#2895 で追記）
    ".claude/hooks/_lib/tdd_order_check.py",
    # hooks/_lib: gh pr create 検出・PR body 抽出・closes/refs 抽出共通ユーティリティ（#2952 で追記）
    ".claude/hooks/_lib/gh_command.py",
    # hooks/_lib: git subprocess 実行・toplevel 解決共通ユーティリティ（#2958 で追記）
    ".claude/hooks/_lib/git_helpers.py",
    # hooks/_lib: venv 実行ファイル（python/ruff 等）の POSIX/Windows レイアウト解決共通ユーティリティ（#3896 で追記）
    ".claude/hooks/_lib/venv_python.py",
    ".claude/hooks/_lib/issue_next_all_completion.py",
    # hooks/_lib: 対象プロジェクトディレクトリ・timeout 解決共通ユーティリティ（#2958 で追記）
    ".claude/hooks/_lib/target_dir.py",
    # hooks/_lib: Gherkin Then/And継続行抽出・禁止語検査共通ユーティリティ（#2956 で追記）
    ".claude/hooks/_lib/gherkin_check.py",
    # hooks/_lib: on-stop.py 責務分割（#2967 で追記。stdin 読み取りは #2957 で hook_io.py へ統合済み）
    ".claude/hooks/_lib/slack_notify.py",
    ".claude/hooks/_lib/stop_merge_summary_check.py",
    ".claude/hooks/_lib/stop_orphan_state.py",
    ".claude/hooks/_lib/branch_cleanup.py",
    ".claude/hooks/_lib/brief_writer.py",
    # hooks/_lib: issue-<N> 抽出共通ユーティリティ（#3550 で追記）
    ".claude/hooks/_lib/issue_ref.py",
    # hooks/_lib/schemas（#2205 で追記）
    ".claude/hooks/_lib/schemas/PostToolUse.json",
    ".claude/hooks/_lib/schemas/PreToolUse.json",
    ".claude/hooks/_lib/schemas/SessionStart.json",
    ".claude/hooks/_lib/schemas/Stop.json",
    ".claude/hooks/_lib/schemas/UserPromptSubmit.json",
    # Phase 3: commands + skills（一般用途のみ・ai-dev-handbook 固有は除外）
    # #3200: brief は commands→skills 変換済み（.claude/commands/brief.md は削除）
    ".claude/skills/brief/SKILL.md",
    ".claude/skills/set-reviewer/SKILL.md",
    # skills: #2205 で追記
    ".claude/skills/ai-review/SKILL.md",
    ".claude/skills/create-issue/SKILL.md",
    ".claude/skills/detect-duplicates/SKILL.md",
    ".claude/skills/issue-review/SKILL.md",
    # #3752: 一般配布する調査ドキュメント作成 skill
    ".claude/skills/research-doc/SKILL.md",
    # #3766: session-start-verify-post-merge hook（consumer にも配布済み・default OFF opt-in）
    # が指示する skill。post-merge-verifier agent と一体で無条件配布する
    ".claude/skills/verify-post-merge/SKILL.md",
    # #4111: Codex/Claude 共通の長期メモリ監査入口
    ".claude/skills/memory-audit/SKILL.md",
    # agents（#2205 で追記）
    ".claude/agents/ai-confirm-verifier.md",
    ".claude/agents/ai-fallback-reviewer.md",
    ".claude/agents/ai-reviewer.md",
    ".claude/agents/coderabbit-screening-reviewer.md",
    ".claude/agents/duplicate-detector.md",
    ".claude/agents/issue-reviewer.md",
    ".claude/agents/issue-writer.md",
    ".claude/agents/scope-diff-checker.md",
    ".claude/agents/verdict-extractor.md",
    # #2451: issue-next 委譲用 full-tool subagent
    ".claude/agents/issue-implementer.md",
    ".claude/agents/issue-fixer.md",
    # #3763: yaru-auto-tick は consumer 側の config.json opt-in 機能（copier 質問の
    # use_* フラグに紐付かない）のため、scope-diff-checker.md と同様に無条件配布する
    ".claude/agents/yaru-verifier.md",
    # #3766: verify-post-merge skill の依存 agent
    ".claude/agents/post-merge-verifier.md",
    # Phase 6: settings.json（modify 方式で MANAGED/PRESERVED マージ後の結果）
    ".claude/settings.json",
    # copier metadata
    ".copier-answers.yml",
    # #1641: .mise.toml.example（notification_email 展開版）
    ".mise.toml.example",
    # #3869: worktree 用 mise 環境変数ブリッジ（自身の配置場所からリポジトリルートを
    # 動的解決する repo-agnostic スクリプト。worktree-mise-stub-path から参照される）
    "mise-worktree-bridge.bash",
    # #2217: .envrc / .mise.toml 等の機密情報が誤って git 管理対象になるのを防ぐ
    ".gitignore",
    # #2256: pre-commit + detect-secrets（コミット前のローカル secret scanning）
    # .gitleaks.toml は配布中止（#2361）。detect-secrets で継続
    ".pre-commit-config.yaml",
    # #3937: gherkin-lint local hook が参照する lint 設定
    ".gherkin-lintrc",
    # #2255: Dependabot version updates（primary_language に応じて uv / npm を分岐）
    ".github/dependabot.yml",
    # #2357: probot/settings 前提の宣言的リポジトリ設定（labels・squash-only・branch 保護。#2368 見直し）
    ".github/settings.yml",
    # docs: consumer 向けドキュメント（#2205 で追記）
    "docs/research/technology-radar-guide.md",
    "docs/setup/bare-metal.md",
    # #2259: CLAUDE.md 共通部分（セキュリティ原則）が参照するため配布対象に追加
    "docs/setup/secrets-management.md",
    # #3741: スクリーンショット参照ルール（AGENTS.md / overview.md が参照するため配布対象に追加）
    "docs/reference/screenshot-rules.md",
    # #3714: personal ブリッジの運用 README。
    # ブリッジ本体（docs/personal/<user>/CLAUDE.md）はディレクトリ名が copier 質問
    # personal_dir_name で決まる動的パスのため REQUIRED_FILES には含めない
    # （projects/py/{{ python_package_name }} と同じ扱い・専用テストで配布を検証する）。
    "docs/personal/README.md",
    # #3210: Codex 対応（.codex/config.toml は config.toml.jinja から生成。
    # post-gen タスクで .agents/skills symlink を生成）
    # #3711: AGENTS.md は rulesync が .rulesync/rules/*.md から生成する生成物
    "AGENTS.md",
    ".codex/config.toml",
    ".codex/hooks.json",
    ".codex/rules/permissions.rules",
    # #3333: rulesync 正本（consumer は rulesync generate で生成物を再生成できる）
    # #3711: overview.md も配布し、consumer の AGENTS.md を rulesync 生成物にする
    "rulesync.jsonc",
    ".rulesync/hooks.jsonc",
    ".rulesync/rules/overview.md",
    ".rulesync/rules/decision-journal.md",
    ".rulesync/rules/escalation-format.md",
    ".rulesync/rules/implementation-constraints.md",
    ".rulesync/rules/issue-creation.md",
    ".rulesync/rules/review-backends.md",
    ".rulesync/rules/test-plan-checklist.md",
    ".rulesync/rules/testing-framework.md",
    ".rulesync/rules/tool-calling.md",
    ".rulesync/rules/workflow.md",
    # #3332: Codex custom agents（.claude/agents/*.md の Codex 版。
    # 他の .toml はいずれかの use_* フラグ配下（.claude/agents/*.md 側と同じ基準）にあるため
    # ここでは無条件配布の scope_diff_checker.toml・yaru_verifier.toml（#3763）・
    # post_merge_verifier.toml（#3766: verify-post-merge skill の依存）のみ残す
    ".codex/agents/scope_diff_checker.toml",
    ".codex/agents/yaru_verifier.toml",
    ".codex/agents/post_merge_verifier.toml",
    # #3711: consumer 側で rulesync を導入するための Node 配布物
    "package.json",
    ".npmrc",
)

# use_* opt-in フラグ（copier.yml の質問デフォルトは全て "false"）が true のときのみ
# 配布・検証対象に加わるファイル群（#2365・#3243）。
# sandbox-copier-poc のデフォルト実行は copier 実運用デフォルト（全 false）に一致させ、
# handbook 固有パス（projects/py/tidd_tools）が consumer 配布物に混入しないことを検証する。
REQUIRED_BY_FLAG: dict[str, tuple[str, ...]] = {
    "use_issue_next": (
        ".claude/skills/issue-next/SKILL.md",
        ".claude/skills/issue-next/ai-confirm-verification.md",
        ".claude/skills/issue-next/duplicate-suspect-triage.md",
        ".claude/skills/issue-next/existing-test-failure.md",
        ".claude/skills/issue-next/fallback-review.md",
        ".claude/skills/issue-next/in-progress-label-check.md",
        ".claude/skills/issue-next/merge-summary-output.md",
        ".claude/skills/issue-next/opus-escalation.md",
        ".claude/skills/issue-next/parser-critical-pr.md",
        ".claude/skills/issue-next/resume-interrupted-review.md",
        ".claude/skills/issue-next/liveness-check.md",
        ".claude/skills/issue-next/main-sync-recovery.md",
        ".claude/skills/issue-next/resume-needs-human-merge.md",
        ".claude/skills/issue-next/smart-guardrails.md",
        ".claude/skills/issue-next/split-consideration.md",
        ".claude/skills/issue-next/step6-merge.md",
        ".claude/skills/issue-next/subagent-delegation.md",
        ".claude/skills/issue-next/unattended-park-and-continue.md",
        # #4038: subagent-delegation.md が参照する action log / drift チェックのリファレンス
        "docs/reference/action-log-and-drift-check.md",
        ".claude/skills/issue-next-all/SKILL.md",
        ".claude/agents/ai-confirm-verifier.md",
        # #2451: issue-next 委譲用 full-tool subagent
        ".claude/agents/issue-implementer.md",
        ".claude/agents/issue-fixer.md",
        # #3332: 上記 .md の Codex 版（同じフラグで除外・配布）
        ".codex/agents/ai_confirm_verifier.toml",
        ".codex/agents/issue_implementer.toml",
        ".codex/agents/issue_fixer.toml",
        "docs/reference/issue-next-agent-selection.md",
        # #4197: ローカル Codex runner と実行手順も issue-next 利用リポジトリへ配布する
        "scripts/issue-next-all-loop.py",
        "docs/reference/issue-next-all-loop.md",
    ),
    # #4084: CodeRabbit スクリーニング関連は use_issue_next かつ use_coderabbit の
    # 両方が true のときのみ配布される（run_cli の AND 条件判定で必須化）。
    "use_coderabbit": (
        ".claude/skills/issue-next/coderabbit-postmerge-screening.md",
        ".claude/agents/coderabbit-screening-reviewer.md",
        ".codex/agents/coderabbit_screening_reviewer.toml",
    ),
    "use_ai_review": (
        ".claude/skills/ai-review/SKILL.md",
        ".claude/agents/ai-reviewer.md",
        ".claude/agents/ai-fallback-reviewer.md",
        ".claude/agents/verdict-extractor.md",
        # #3332: 上記 .md の Codex 版（同じフラグで除外・配布）
        ".codex/agents/ai_reviewer.toml",
        ".codex/agents/ai_fallback_reviewer.toml",
        ".codex/agents/verdict_extractor.toml",
    ),
    "use_issue_quality_check": (
        ".claude/skills/create-issue/SKILL.md",
        ".claude/skills/issue-review/SKILL.md",
        ".claude/skills/detect-duplicates/SKILL.md",
        ".claude/skills/set-reviewer/SKILL.md",
        ".claude/agents/issue-writer.md",
        ".claude/agents/issue-reviewer.md",
        ".claude/agents/duplicate-detector.md",
        # #3332: 上記 .md の Codex 版（同じフラグで除外・配布）
        ".codex/agents/issue_writer.toml",
        ".codex/agents/issue_reviewer.toml",
        ".codex/agents/duplicate_detector.toml",
    ),
}

EXCLUDED_FILES = (
    "copier.yml",
    "README.md",
    "_copier",
    ".claude/hooks/__pycache__",
    ".claude/hooks/_lib/__pycache__",
    # ai-dev-handbook 固有 skill・commands（consumer に配布しない）
    ".claude/skills/slack-url-evaluator/SKILL.md",
    ".claude/skills/publish/SKILL.md",
    # #2239: maintainer 専用 hook（templates/ ディレクトリの存在が前提のため consumer には無意味）
    ".claude/hooks/notify-template-sync.py",
    # Phase 6: 中間ファイル settings.managed.json は _tasks 実行後に merge_settings.py が削除する
    ".claude/settings.managed.json",
    # #2355: root CLAUDE.md は _exclude 対象（consumer にはローカル管理させる）
    "CLAUDE.md",
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "sandbox-copier-poc",
        help="templates/workflow/ の Copier PoC を sandbox で実行して検証する",
        description=__doc__,
    )
    add_common_flags(parser)
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="テンプレートディレクトリ（default: root copier.yml を持つリポジトリルート）",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="展開先ディレクトリ（default: 一時ディレクトリ）",
    )
    parser.add_argument(
        "--project-name",
        default="Sandbox",
        help="copier に渡す project_name（default: Sandbox）",
    )
    parser.add_argument(
        "--primary-language",
        default="Python",
        choices=("Python", "TypeScript", "GAS", "Other"),
        help="copier に渡す primary_language（default: Python）",
    )
    parser.add_argument(
        "--github-org",
        default="sandbox-test-org",
        help="copier に渡す github_org（default: sandbox-test-org）",
    )
    parser.add_argument(
        "--notification-email",
        default="team@example.com",
        help="copier に渡す notification_email（default: team@example.com）",
    )
    parser.add_argument(
        "--ci-provider",
        default="none",
        choices=("github-actions", "none"),
        help="copier に渡す ci_provider（default: none）",
    )
    parser.add_argument(
        "--use-coderabbit",
        action="store_true",
        help="copier に渡す use_coderabbit（default: false・#2274）",
    )
    parser.add_argument(
        "--no-security-policy",
        dest="use_security_policy",
        action="store_false",
        help="copier に渡す use_security_policy を false にする（default: true・#2276）",
    )
    parser.add_argument(
        "--no-dependabot-ignore-major",
        dest="dependabot_ignore_major",
        action="store_false",
        help="copier に渡す dependabot_ignore_major を false にする（default: true・#2280）",
    )
    parser.add_argument(
        "--use-scorecard",
        action="store_true",
        help="copier に渡す use_scorecard（default: false・#2277）",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="実行後に sandbox ディレクトリを削除しない",
    )
    parser.add_argument(
        "--verify-tidd-install",
        action="store_true",
        help=(
            "sandbox 内で git+file:// URL を使って `tidd_tools` をインストールし、"
            "`tidd --help` が成功することまで検証する（Phase 5 検証用）"
        ),
    )
    parser.add_argument(
        "--full-e2e",
        action="store_true",
        help=(
            "copier copy → CLAUDE.md の repo-specific マーカー内を編集 → copier update "
            "→ 独自追記が保持され共通部分が更新されているか検証する（Phase 7 検証用）"
        ),
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    if shutil.which("copier") is None:
        print(
            "[sandbox-copier-poc] copier CLI が見つかりません。`uv tool install copier` を実行してください。",
            file=sys.stderr,
        )
        return 2

    template_dir = _resolve_template_dir(args.template)
    if template_dir is None or not template_dir.is_dir():
        print(
            f"[sandbox-copier-poc] テンプレートディレクトリが見つかりません: {template_dir}",
            file=sys.stderr,
        )
        return 2

    dest_ctx = _dest_context(args.dest, keep=args.keep)
    with dest_ctx as dest:
        _init_git_repo(dest)
        copier_result = _run_copier_copy(
            template=template_dir,
            dest=dest,
            project_name=args.project_name,
            primary_language=args.primary_language,
            github_org=getattr(args, "github_org", "sandbox-test-org"),
            notification_email=getattr(args, "notification_email", "team@example.com"),
            ci_provider=getattr(args, "ci_provider", "none"),
            use_coderabbit=getattr(args, "use_coderabbit", False),
            use_security_policy=getattr(args, "use_security_policy", True),
            dependabot_ignore_major=getattr(args, "dependabot_ignore_major", True),
            use_scorecard=getattr(args, "use_scorecard", False),
            use_issue_next=getattr(args, "use_issue_next", False),
            use_ai_review=getattr(args, "use_ai_review", False),
            use_issue_quality_check=getattr(args, "use_issue_quality_check", False),
        )
        if copier_result != 0:
            return copier_result
        # REQUIRED_FILES は全機能配布セットを維持（#1997 gate・#2451 登録検証のため）。
        # 検証時は opt-in フラグ（copier.yml 質問デフォルト: 全 false・#2365）が true の
        # ファイルのみ必須に加え、false のフラグ配下ファイルは必須対象から除外する（#3243）。
        flag_controlled = {f for files in REQUIRED_BY_FLAG.values() for f in files}
        required = tuple(rel for rel in REQUIRED_FILES if rel not in flag_controlled)
        for flag_name, flag_files in REQUIRED_BY_FLAG.items():
            if flag_name == "use_coderabbit":
                # #4084: CodeRabbit スクリーニング関連ファイルは use_issue_next と
                # use_coderabbit の両方が true のときのみ配布される（AND 条件）。
                if getattr(args, "use_issue_next", False) and getattr(args, "use_coderabbit", False):
                    required += flag_files
            elif getattr(args, flag_name, False):
                required += flag_files
        sandbox_result = _verify_sandbox(dest, required_files=required)
        if sandbox_result != 0:
            return sandbox_result
        links_result = _verify_relative_links(dest)
        if links_result != 0:
            return links_result
        # `_FakeArgs`（旧テスト）は verify_tidd_install 属性を持たないため getattr で
        # 後方互換を保つ。argparse 経由の呼び出しでは常に bool が入っている。
        if getattr(args, "verify_tidd_install", False):
            tidd_result = _verify_tidd_install(template_dir=template_dir)
            if tidd_result != 0:
                return tidd_result
        if getattr(args, "full_e2e", False):
            return _verify_full_e2e(template_dir=template_dir, dest=dest)
        return 0


def _resolve_template_dir(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit.resolve()
    # このモジュールはリポジトリの projects/py/tidd_tools/src/tidd_tools/ 配下にある。
    # #2231 以降 copier 設定は root copier.yml（_subdirectory: templates/workflow）に
    # 移動したため、親を遡り「root copier.yml + templates/workflow」を持つ位置を返す。
    here = Path(__file__).resolve()
    parents = here.parents
    for depth in range(3, min(8, len(parents))):
        candidate = parents[depth]
        if (candidate / "copier.yml").is_file() and (candidate / "templates" / "workflow").is_dir():
            return candidate
    return None


class _DestContext:
    """出力先ディレクトリのライフサイクル管理.

    `dest` 明示指定なら resolve するだけで削除しない。未指定なら `tempfile.mkdtemp()`
    で作成して `__exit__` で `shutil.rmtree()` する（`TemporaryDirectory` の非公開
    `_finalizer` に依存せず、`--keep` の実装がシンプルになる）。
    """

    def __init__(self, dest: Path | None, keep: bool) -> None:
        self._explicit = dest
        self._keep = keep
        self._tempdir_path: Path | None = None

    def __enter__(self) -> Path:
        if self._explicit is not None:
            resolved = self._explicit.resolve()
            resolved.mkdir(parents=True, exist_ok=True)
            return resolved
        self._tempdir_path = Path(tempfile.mkdtemp(prefix="tidd-copier-sandbox-")).resolve()
        return self._tempdir_path

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._tempdir_path is None:
            return
        if self._keep:
            print(
                f"[sandbox-copier-poc] --keep 指定のため sandbox を残します: {self._tempdir_path}",
                file=sys.stderr,
            )
            return
        shutil.rmtree(self._tempdir_path, ignore_errors=True)


def _dest_context(dest: Path | None, keep: bool) -> _DestContext:
    return _DestContext(dest, keep)


def _init_git_repo(dest: Path) -> None:
    """copier update が動く前提として git 履歴を作る.

    ユーザーの global git config に user.name / user.email が未設定でも動くように、
    リポジトリローカルの config で fixture 用の値を設定する（sandbox 用途のみ）。
    """
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    subprocess.run(
        ["git", "-C", str(dest), "config", "user.email", "sandbox@tidd.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(dest), "config", "user.name", "TiDD Sandbox"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(dest), "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
    )


def _run_copier_copy(
    *,
    template: Path,
    dest: Path,
    project_name: str,
    primary_language: str,
    github_org: str = "sandbox-test-org",
    notification_email: str = "team@example.com",
    ci_provider: str = "none",
    use_coderabbit: bool = False,
    use_security_policy: bool = True,
    dependabot_ignore_major: bool = True,
    use_scorecard: bool = False,
    use_issue_next: bool = False,
    use_ai_review: bool = False,
    use_issue_quality_check: bool = False,
) -> int:
    # `--vcs-ref HEAD` は必須（#2243）。省略すると copier は latest git tag をチェックアウト
    # するため、tag 以降に加えた template 修正が sandbox テストに反映されない。
    cmd = [
        "copier",
        "copy",
        "--vcs-ref",
        "HEAD",
        "--defaults",
        "--data",
        f"project_name={project_name}",
        "--data",
        f"primary_language={primary_language}",
        "--data",
        f"github_org={github_org}",
        "--data",
        f"notification_email={notification_email}",
        "--data",
        f"ci_provider={ci_provider}",
        "--data",
        f"use_coderabbit={'true' if use_coderabbit else 'false'}",
        "--data",
        f"use_security_policy={'true' if use_security_policy else 'false'}",
        "--data",
        f"dependabot_ignore_major={'true' if dependabot_ignore_major else 'false'}",
        "--data",
        f"use_scorecard={'true' if use_scorecard else 'false'}",
        "--data",
        f"use_issue_next={'true' if use_issue_next else 'false'}",
        "--data",
        f"use_ai_review={'true' if use_ai_review else 'false'}",
        "--data",
        f"use_issue_quality_check={'true' if use_issue_quality_check else 'false'}",
        "--trust",
        str(template),
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(
            f"[sandbox-copier-poc] copier copy が失敗しました (exit {result.returncode}):",
            file=sys.stderr,
        )
        print(result.stderr, file=sys.stderr)
        return 1
    return 0


def _verify_sandbox(
    dest: Path,
    required_files: Sequence[str] | None = None,
    excluded_files: Sequence[str] | None = None,
) -> int:
    required = tuple(required_files) if required_files is not None else REQUIRED_FILES
    excluded = tuple(excluded_files) if excluded_files is not None else EXCLUDED_FILES
    missing = [rel for rel in required if not (dest / rel).is_file()]
    if missing:
        print("[sandbox-copier-poc] 必須ファイルが不足:", file=sys.stderr)
        for rel in missing:
            print(f"  - {rel}", file=sys.stderr)
        return 1

    unexpected = [rel for rel in excluded if (dest / rel).exists()]
    if unexpected:
        print(
            "[sandbox-copier-poc] _exclude 対象が consumer に配布されています:",
            file=sys.stderr,
        )
        for rel in unexpected:
            print(f"  - {rel}", file=sys.stderr)
        return 1

    answers = (dest / ".copier-answers.yml").read_text(encoding="utf-8")
    # Copier は git 参照からのコピーだと `_commit`、ローカルディレクトリからだと
    # `_src_path` を answers に記録する。sandbox PoC はローカルディレクトリを
    # 参照するため、どちらか一方が含まれていれば追跡情報として十分。
    if "_commit" not in answers and "_src_path" not in answers:
        print(
            "[sandbox-copier-poc] .copier-answers.yml に _commit / _src_path のどちらも含まれていません",
            file=sys.stderr,
        )
        return 1

    # #2255: dependabot.yml.jinja の primary_language / ci_provider 分岐が壊れて
    # 不正な YAML を生成していないか検証する。
    dependabot_yml = dest / ".github" / "dependabot.yml"
    if dependabot_yml.is_file():
        try:
            yaml.safe_load(dependabot_yml.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            print(
                f"[sandbox-copier-poc] .github/dependabot.yml の YAML パースエラー: {exc}",
                file=sys.stderr,
            )
            return 1

    # #2274: coderabbit.yaml.jinja が壊れた YAML を生成していないか検証する
    # （use_coderabbit=false のときは配布されないため存在チェックのみ通す）。
    coderabbit_yaml = dest / ".coderabbit.yaml"
    if coderabbit_yaml.is_file():
        try:
            yaml.safe_load(coderabbit_yaml.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            print(
                f"[sandbox-copier-poc] .coderabbit.yaml の YAML パースエラー: {exc}",
                file=sys.stderr,
            )
            return 1

    print(f"[sandbox-copier-poc] OK: 全チェックパス (dest={dest})")
    return 0


_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# スキーム付き絶対参照（http(s)://・ftp:// 等）。`://` を必須にすることで
# SSH 形式（`git@host:path`）やコロンを含む相対パスの誤判定を避ける（#2259 レビュー指摘）。
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_NON_SLASH_SCHEMES = ("mailto:", "tel:")


def _find_broken_relative_links(dest: Path) -> list[tuple[Path, str]]:
    """`dest` 配下の Markdown ファイルが参照する相対リンクのうち解決できないものを返す.

    絶対 URL（`http(s)://`・`mailto:`等）・同一ファイル内アンカー（`#...`）は対象外。
    タイトル付きリンク（`[text](url "title")`）は URL 部分のみを見る。
    `/` で始まるリンクは `dest` 直下からの絶対パスとして解決する（システムルート誤参照を回避）。
    相対パスは `#fragment` を除去したうえでリンク元ファイルからの相対パスとして解決する。
    戻り値は `(リンク元ファイル, リンク文字列)` のタプル一覧（#2259）。
    """
    broken: list[tuple[Path, str]] = []
    for md_file in sorted(dest.rglob("*.md")):
        # #3711: ルール本体は `.claude/rules/` 相対で `./workflow.md` 等を参照する。
        # これを flat 展開した AGENTS.md に載せるとルール間リンクが解決できないが、
        # 実体（`.claude/rules/*.md`）側のリンクは正しく、AGENTS.md は単一ファイルの
        # 集約ビューであるため検査対象から除外する（root AGENTS.md と同じ扱い・#3711）。
        if md_file.name == "AGENTS.md" and md_file.parent == dest:
            continue
        text = md_file.read_text(encoding="utf-8", errors="ignore")
        for raw_link in _MD_LINK_RE.findall(text):
            link = raw_link.strip()
            if not link or link.startswith("#"):
                continue
            # タイトル付きリンク: `url "title"` / `url 'title'` の先頭空白以降を除去
            link = link.split(None, 1)[0]
            if link.startswith(_NON_SLASH_SCHEMES) or _SCHEME_RE.match(link):
                continue
            target_str = link.split("#", 1)[0].strip()
            if not target_str:
                continue
            if target_str.startswith("/"):
                target = (dest / target_str.lstrip("/")).resolve()
            else:
                target = (md_file.parent / target_str).resolve()
            if not target.exists():
                broken.append((md_file.relative_to(dest), raw_link.strip()))
    return broken


def _verify_relative_links(dest: Path) -> int:
    broken = _find_broken_relative_links(dest)
    if broken:
        print(
            "[sandbox-copier-poc] 解決できない相対リンクが見つかりました:",
            file=sys.stderr,
        )
        for rel_file, link in broken:
            print(f"  - {rel_file}: {link}", file=sys.stderr)
        return 1

    print(f"[sandbox-copier-poc] OK: リンク検証パス（検出済みリンク切れ 0 件・dest={dest})")
    return 0


_PUBLISH_DEV_EXTRA_ENTRY = (
    "    # Issue #2782: tests/step_defs/test_issue_2768.py が `publish.core` を import する。\n"
    "    # workspace メンバーとして明示依存させることで、`uv sync --project\n"
    "    # projects/py/tidd_tools --extra dev`（worktree 初期化・venv 修復の既定コマンド）\n"
    "    # の exact sync で publish が prune されなくなる。\n"
    '    "publish",\n'
)

_PUBLISH_UV_SOURCES_TABLE = (
    '# Issue #2782: dev extra の "publish" を workspace メンバー解決にする。\n'
    '# これがないと uv が PyPI で "publish" という名前のパッケージを探しに行き解決に失敗する。\n'
    "[tool.uv.sources]\n"
    "publish = { workspace = true }\n"
)


def strip_workspace_only_pyproject(text: str) -> str:
    """workspace root の無い環境向けに、workspace メンバー依存の宣言を取り除いた pyproject.toml を返す.

    Issue #2782 で `tidd_tools` の dev extra に workspace メンバー `publish`
    （`[tool.uv.sources] publish = { workspace = true }`）を明示依存として追加したが、
    uv はこの `[tool.uv.sources]` エントリを、対象 extra が要求されているかに関わらず
    パッケージメタデータのビルド時に常に検証する。そのため workspace root を持たない
    独立ディレクトリ（isolated コピー・workspace root を含まない worktree 模擬環境）では
    `publish` を解決できずビルド自体が失敗する（PR #2801 レビュー指摘）。

    dev 依存や `[tool.ruff]` 等の他設定は isolated 環境でも意味を持つ（ruff 実行検証等）ため
    残し、workspace 解決が必須になる 2 箇所（dev extra の `"publish"` エントリ・
    `[tool.uv.sources]` テーブル）だけをテキストとして取り除く。

    Raises:
        ValueError: 想定した記述（Issue #2782 で追加した箇所）が見つからない場合。
            pyproject.toml の記述変更を静かに見逃さないための fail-loud ガード。
    """
    if _PUBLISH_DEV_EXTRA_ENTRY not in text:
        raise ValueError("dev extra の publish エントリが見つかりません（pyproject.toml が変更された可能性）")
    if _PUBLISH_UV_SOURCES_TABLE not in text:
        raise ValueError("[tool.uv.sources] publish テーブルが見つかりません（pyproject.toml が変更された可能性）")
    text = text.replace(_PUBLISH_DEV_EXTRA_ENTRY, "")
    text = text.replace(_PUBLISH_UV_SOURCES_TABLE, "")
    return text


def _copy_tidd_project_isolated(tidd_project: Path, dest_dir: Path) -> Path:
    """tidd_project の pyproject.toml と src/ を workspace context のない dest_dir にコピーする.

    uv tool install に渡すパスが workspace root 配下にある場合、uv がワークスペースを
    自動検出して uv.lock を参照する。CircleCI 環境ではこの workspace-aware 解決が
    コンテナ環境固有の問題（uv バージョン差異・lock 不整合など）で失敗することがある。

    コピー先（dest_dir）を workspace root の外に置くことで、uv は workspace を検出せず
    独立したパッケージとして解決するため、環境依存の失敗を回避できる（Issue #2651）。

    pyproject.toml はそのままコピーせず `strip_workspace_only_pyproject()` で
    workspace メンバー依存（Issue #2782 の `publish`）の宣言を取り除いてから書き出す
    （PR #2801 レビュー指摘: isolated コピーには workspace root が無く解決不能）。

    Note:
        コピーするのは pyproject.toml と src/ のみ。tests/ や uv.lock は不要で
        コピーしない（インストール時に tests の依存は使わないため）。

    Returns:
        コピー先ディレクトリのパス（dest_dir / "tidd_tools"）
    """
    copy_dest = dest_dir / "tidd_tools"
    copy_dest.mkdir(parents=True, exist_ok=True)
    original_text = (tidd_project / "pyproject.toml").read_text(encoding="utf-8")
    (copy_dest / "pyproject.toml").write_text(
        strip_workspace_only_pyproject(original_text),
        encoding="utf-8",
    )
    shutil.copytree(
        tidd_project / "src",
        copy_dest / "src",
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return copy_dest


def _verify_tidd_install(template_dir: Path) -> int:
    """Phase 5: workspace isolation した一時コピーから tidd_tools を install → tidd --help で検証.

    template_dir から遡って ai-dev-handbook のリポジトリルートを見つけ、
    `projects/py/tidd_tools` の pyproject.toml と src/ を workspace context のない
    一時ディレクトリにコピーしてから uv tool install する。
    一時的な UV_TOOL_DIR を使って consumer 環境を汚さない。

    旧実装では `git+file://<repo>#subdirectory=...` という spec を使っていたが、
    CircleCI の shallow clone（`--depth=1`）環境では `refs/remotes/origin/HEAD` が
    設定されずに uv が失敗する（Issue #2635）。ローカルパス直指定に変更したが、
    tidd_project が workspace root（`[tool.uv.workspace]` を持つ親 pyproject.toml）
    の配下にある場合、uv が workspace を自動検出して uv.lock を参照するため、
    CircleCI 環境固有の lock 解決失敗が起きる（Issue #2651）。

    tidd_project を workspace context のない一時ディレクトリにコピーすることで、
    uv が workspace を検出せず独立インストールが実現し、どの環境でも安定して動作する。

    Issue #2673: `_copy_tidd_project_isolated()` は `.python-version`（3.11 pin）を
    コピーしないため、CircleCI のようにまだ Python 3.11 をキャッシュしていない
    フレッシュな環境では `uv tool install` が制約 `>=3.11` を満たす別バージョン
    （既にキャッシュ済みの 3.14 等）を選んでしまうことがある。
    そのツール venv の Python が `tidd_tools.shared.venv_repair` の要求する 3.11 と
    一致しないと、`tidd --help` 実行時に venv 自動修復（Issue #2178）が発動し、
    誤って実リポジトリの `.venv` を作り直した上で `os.execv` してしまい
    `ModuleNotFoundError: No module named 'tidd_tools'` で失敗する。
    `uv tool install --python 3.11` で明示的にバージョンを固定することで、
    どの環境でも venv_repair が要求する Python バージョンと一致させる。
    """
    if shutil.which("uv") is None:
        print(
            "[sandbox-copier-poc] uv が見つかりません。`--verify-tidd-install` を使うには uv が必要です。",
            file=sys.stderr,
        )
        return 2

    repo_root = _find_repo_root_from_template(template_dir)
    if repo_root is None:
        print(
            "[sandbox-copier-poc] template から repo root を特定できませんでした。",
            file=sys.stderr,
        )
        return 2

    tidd_project = repo_root / "projects" / "py" / "tidd_tools"
    if not (tidd_project / "pyproject.toml").is_file():
        print(
            f"[sandbox-copier-poc] tidd_tools プロジェクトが見つかりません: {tidd_project}",
            file=sys.stderr,
        )
        return 2

    # 一時 UV_TOOL_DIR を作って consumer 環境の tidd tool を上書きしないようにする。
    # また、isolated_dir に tidd_project をコピーして workspace context を排除する。
    # Issue #2651: tidd_project が workspace root 配下にある場合、uv が uv.lock を
    # 参照して CircleCI 環境固有の失敗が起きるため、workspace 外の tmp dir を使う。
    with tempfile.TemporaryDirectory(prefix="tidd-copier-tool-") as tool_dir:
        # tidd_project を workspace context のない tmp dir にコピーして
        # uv が workspace root を検出しないようにする（Issue #2651）。
        isolated_dir = Path(tool_dir) / "isolated-src"
        isolated_project = _copy_tidd_project_isolated(tidd_project, isolated_dir)

        # 現在の環境変数を引き継ぐ（pyenv/asdf/プロキシ設定などを維持）。
        # UV_TOOL_DIR と UV_TOOL_BIN_DIR だけ一時ディレクトリに向ける。
        env = {
            **os.environ,
            "UV_TOOL_DIR": tool_dir,
            "UV_TOOL_BIN_DIR": str(Path(tool_dir) / "bin"),
        }
        # Issue #2635: git+file:// は CircleCI の shallow clone 環境で
        # `refs/remotes/origin/HEAD` が解決できずに失敗する。
        # Issue #2651: workspace root 配下のパスを直接使うと uv が workspace を
        # 検出して uv.lock に依存する解決が起き、CircleCI 環境で失敗する。
        # isolated_project は workspace root の外にある tmp dir なので安全。
        spec = f"tidd-tools @ {isolated_project}"
        # Issue #2673: `--python` を明示しないと、Python 3.11 がまだキャッシュされて
        # いないフレッシュな環境（CircleCI 等）で `uv` が `>=3.11` を満たす別バージョン
        # （例: 既にキャッシュ済みの 3.14）を選び、venv_repair（Issue #2178）が要求する
        # 3.11 と食い違って `tidd --help` が ModuleNotFoundError で失敗する。
        install_cmd = [
            "uv",
            "tool",
            "install",
            "--python",
            f"{REQUIRED_MAJOR}.{REQUIRED_MINOR}",
            "--force",
            spec,
        ]
        install_result = subprocess.run(
            install_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if install_result.returncode != 0:
            print(
                "[sandbox-copier-poc] uv tool install が失敗しました:",
                file=sys.stderr,
            )
            print(install_result.stderr, file=sys.stderr)
            return 1

        tidd_bin = Path(tool_dir) / "bin" / "tidd"
        if not tidd_bin.is_file():
            print(
                f"[sandbox-copier-poc] tidd 実行ファイルが生成されていません: {tidd_bin}",
                file=sys.stderr,
            )
            return 1

        help_result = subprocess.run(
            [str(tidd_bin), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if help_result.returncode != 0:
            print(
                f"[sandbox-copier-poc] tidd --help が失敗しました (exit {help_result.returncode}):",
                file=sys.stderr,
            )
            print(help_result.stderr, file=sys.stderr)
            return 1
        if "TiDD" not in help_result.stdout:
            print(
                f"[sandbox-copier-poc] tidd --help 出力が期待の説明を含みません:\n{help_result.stdout}",
                file=sys.stderr,
            )
            return 1

    print("[sandbox-copier-poc] OK: tidd_tools のインストールと --help 検証成功")
    return 0


# Issue #3809: --full-e2e の上流ルール反映検証で使うマーカー。
# #3770 で detail rule 9 本（`.rulesync/rules/workflow.md` 等）は `targets: ['claudecode']`
# になり AGENTS.md（root: true・targets: codexcli の overview.md のみが内容源）への
# インライン展開から除外された。detail rule 由来マーカーと root ファイル由来マーカーを
# 分けることで、この設計を _verify_upstream_rule_propagation で正しく検証する。
_E2E_DETAIL_TOUCH_MARKER = "<!-- e2e-touch-detail -->"
_E2E_ROOT_TOUCH_MARKER = "<!-- e2e-touch-root -->"


def _verify_upstream_rule_propagation(dest: Path) -> int:
    """#3809: 上流の rules 更新が正しい生成物にのみ反映されていることを検証する.

    detail rule（`.rulesync/rules/workflow.md` 等・targets: claudecode 専用）の更新は
    rules 正本 `.rulesync/rules/workflow.md` と rulesync 生成物 `.claude/rules/workflow.md`
    には反映されるが、AGENTS.md（root: true・targets: codexcli の overview.md のみが
    内容源）には反映されない（#3770 のインライン展開除外設計）。root ファイル
    （overview.md）の更新のみが AGENTS.md に反映される。

    `dest`（consumer リポジトリ）に `_E2E_DETAIL_TOUCH_MARKER`（detail rule 経由）と
    `_E2E_ROOT_TOUCH_MARKER`（root ファイル経由）が事前に書き込まれている前提。
    """
    rules_source = dest / ".rulesync" / "rules" / "workflow.md"
    if _E2E_DETAIL_TOUCH_MARKER not in rules_source.read_text(encoding="utf-8"):
        print(
            "[sandbox-copier-poc] --full-e2e: 上流の rules 更新が .rulesync/rules に反映されていません",
            file=sys.stderr,
        )
        return 1
    claude_rules_md = dest / ".claude" / "rules" / "workflow.md"
    if _E2E_DETAIL_TOUCH_MARKER not in claude_rules_md.read_text(encoding="utf-8"):
        print(
            "[sandbox-copier-poc] --full-e2e: 上流の rules 更新が .claude/rules/workflow.md に反映されていません",
            file=sys.stderr,
        )
        return 1
    agents_md = dest / "AGENTS.md"
    agents_text = agents_md.read_text(encoding="utf-8")
    if _E2E_ROOT_TOUCH_MARKER not in agents_text:
        print(
            "[sandbox-copier-poc] --full-e2e: 上流の rules 更新（root ファイル）が"
            " AGENTS.md（rulesync 生成物）に反映されていません",
            file=sys.stderr,
        )
        return 1
    if _E2E_DETAIL_TOUCH_MARKER in agents_text:
        print(
            "[sandbox-copier-poc] --full-e2e: detail rule 専用マーカーが AGENTS.md に"
            " 混入しています（#3770 のインライン展開除外設計に違反）",
            file=sys.stderr,
        )
        return 1
    return 0


def _verify_full_e2e(template_dir: Path, dest: Path) -> int:
    """Phase 7: consumer が repo-specific ブロック内を編集後、`copier update` で
    独自追記が保持され共通部分は最新化されているかを検証する.

    #3711: コア部の正本は `.rulesync/rules/overview.md`（copier が
    `overview.md.jinja` から生成）。repo-specific マーカー内を consumer が編集した状態で
    `copier update` を実行し、独自追記が保持され・ルール更新が `.rulesync/rules/*.md` と
    rulesync 生成物 `AGENTS.md` に反映され・`rulesync generate --check` が exit 0 になる
    ことを検証する。

    `copier update` はテンプレート側が git repo で `_commit` が answers に
    記録されていることを要求する。本ハーネスでは `template_dir` を
    scratch git repo（bare 相当）に一時複製してから copier copy をやり直し、
    `--vcs-ref` でその git repo の HEAD を指す形で update する。
    """
    overview_md = dest / ".rulesync" / "rules" / "overview.md"
    if not overview_md.is_file():
        # #3711: overview.md が無い環境（旧テンプレート由来の consumer）では skip。
        print(
            "[sandbox-copier-poc] --full-e2e: .rulesync/rules/overview.md が配布されていないため skip",
            file=sys.stderr,
        )
        return 0

    begin_marker = "<!-- BEGIN: repo-specific -->"
    marker_line = "<!-- FULL-E2E CUSTOM RULE -->"
    if begin_marker not in overview_md.read_text(encoding="utf-8"):
        print(
            f"[sandbox-copier-poc] --full-e2e 前提: {overview_md} に {begin_marker} が無い",
            file=sys.stderr,
        )
        return 1

    # copier update が動く形にするため、scratch git repo として template を複製する。
    # template を repo root に直接配置することで `git+file://` URL が使える
    # （copier は URL 由来だと `_commit` を answers に記録する）。
    # #2236: template_dir はリポジトリルートなので丸ごとコピーせず、copier に必要な
    # root copier.yml + templates/workflow ツリーのみを複製する（.git/.venv 等を含めない）。
    with tempfile.TemporaryDirectory(prefix="tidd-copier-tmpl-") as tmpl_scratch:
        scratch = Path(tmpl_scratch).resolve()
        shutil.copy2(template_dir / "copier.yml", scratch / "copier.yml")
        shutil.copytree(
            template_dir / "templates" / "workflow",
            scratch / "templates" / "workflow",
            symlinks=False,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        subprocess.run(["git", "-C", str(scratch), "init", "-q", "-b", "main"], check=True)
        subprocess.run(
            ["git", "-C", str(scratch), "config", "user.email", "sandbox@tidd.local"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(scratch), "config", "user.name", "TiDD Sandbox"],
            check=True,
        )
        subprocess.run(["git", "-C", str(scratch), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(scratch), "commit", "-q", "-m", "initial template snapshot"],
            check=True,
        )

        template_url = f"git+file://{scratch}"

        # copier copy をやり直す（`_commit` を answers に記録するため）
        shutil.rmtree(dest / ".claude", ignore_errors=True)
        shutil.rmtree(dest / ".github", ignore_errors=True)
        for f in (dest / ".copier-answers.yml", dest / ".mise.toml.example"):
            f.unlink(missing_ok=True)
        # Issue #1747: 既存 dest に残っている生成物（PR #1742 で追加された docs/research 等）と
        # 衝突しないよう --overwrite で非対話的に強制上書きする。--defaults と併用可。
        copy_cmd = [
            "copier",
            "copy",
            "--defaults",
            "--overwrite",
            "--data",
            "project_name=E2E",
            "--data",
            "primary_language=Python",
            "--data",
            "github_org=sandbox-e2e-org",
            "--data",
            "notification_email=sandbox@e2e.example.com",
            "--trust",
            template_url,
            str(dest),
        ]
        copy_result = subprocess.run(copy_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if copy_result.returncode != 0:
            print(
                f"[sandbox-copier-poc] --full-e2e: 再 copier copy 失敗"
                f" (exit {copy_result.returncode}):\n{copy_result.stderr}",
                file=sys.stderr,
            )
            return 1

        # consumer 側で全ファイルを commit（clean working tree にする）
        try:
            subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", str(dest), "commit", "-q", "-m", "initial copier copy"],
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            print(f"[sandbox-copier-poc] 初回 git commit 失敗: {exc}", file=sys.stderr)
            return 1

        # BEGIN マーカー直後に consumer 独自追記を挿入する
        text = overview_md.read_text(encoding="utf-8")
        text = text.replace(begin_marker, f"{begin_marker}\n{marker_line}", 1)
        overview_md.write_text(text, encoding="utf-8")
        try:
            subprocess.run(["git", "-C", str(dest), "add", ".rulesync/rules/overview.md"], check=True)
            subprocess.run(
                ["git", "-C", str(dest), "commit", "-q", "-m", "consumer edit in repo-specific block"],
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            print(f"[sandbox-copier-poc] git commit 失敗: {exc}", file=sys.stderr)
            return 1

        # scratch template を少し変更してもう一度 commit（新しい版として扱う）。
        # maintainer は正本 `.rulesync/rules/*.md` を更新したら rulesync generate で
        # `.claude/rules/*.md` と AGENTS.md を再生成してから配布する（#3333・#3711）。
        # 本ハーネスではこの「再生成後」状態を再現するため、detail rule 正本と生成物
        # `.claude/rules/workflow.md` に detail 用マーカーを、root ファイル
        # `overview.md.jinja`（AGENTS.md の唯一の内容源・#3770）に root 用マーカーを
        # それぞれ追記する。
        for rel in (
            "templates/workflow/.rulesync/rules/workflow.md",
            "templates/workflow/.claude/rules/workflow.md",
        ):
            touched_rule = scratch / rel
            touched_rule.write_text(
                touched_rule.read_text(encoding="utf-8") + f"\n\n{_E2E_DETAIL_TOUCH_MARKER}\n",
                encoding="utf-8",
            )
        overview_tmpl = scratch / "templates/workflow/.rulesync/rules/overview.md.jinja"
        overview_tmpl.write_text(
            overview_tmpl.read_text(encoding="utf-8") + f"\n\n{_E2E_ROOT_TOUCH_MARKER}\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(scratch), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(scratch), "commit", "-q", "-m", "add e2e-touch markers"],
            check=True,
        )

        # copier update を実行する
        update_cmd = ["copier", "update", "--defaults", "--trust", str(dest)]
        update_result = subprocess.run(update_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if update_result.returncode != 0:
            print(
                f"[sandbox-copier-poc] --full-e2e: copier update 失敗"
                f" (exit {update_result.returncode}):\n{update_result.stderr}",
                file=sys.stderr,
            )
            return 1

    # 検証: マーカー独自追記が保持され、共通部分の見出しが overview.md に残っている
    after = overview_md.read_text(encoding="utf-8")
    if marker_line not in after:
        print(
            "[sandbox-copier-poc] --full-e2e: consumer 独自追記が消えました",
            file=sys.stderr,
        )
        return 1
    for expected in ("## 行動原則", "## セキュリティ原則", "## TiDD ワークフロー"):
        if expected not in after:
            print(
                f"[sandbox-copier-poc] --full-e2e: 共通部分の見出しが消えました: {expected}",
                file=sys.stderr,
            )
            return 1

    # 上流の変更（e2e-touch マーカー）が正しい生成物にのみ反映されている（#3770・#3809）
    propagation_result = _verify_upstream_rule_propagation(dest)
    if propagation_result != 0:
        return propagation_result

    print("[sandbox-copier-poc] OK: --full-e2e フロー成功（consumer 追記保持 + 上流更新反映）")
    return 0


def _find_repo_root_from_template(template_dir: Path) -> Path | None:
    """template_dir から親を遡り、`projects/py/tidd_tools/pyproject.toml` がある位置を返す."""
    for parent in [template_dir, *template_dir.parents]:
        if (parent / "projects" / "py" / "tidd_tools" / "pyproject.toml").is_file():
            return parent
    return None


def _min_subprocess_env() -> dict[str, str]:
    """subprocess に渡す最小限の環境変数（PATH と HOME を含む）.

    現在はテストのヘルパー関数として保持。プロダクションコードは
    `os.environ.copy()` ベースで環境を引き継ぐ。
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
