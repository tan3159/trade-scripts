"""`tidd health-check` サブコマンド（Issue #2205 / #2212）.

リポジトリ内の workflow 動作依存（hooks/rules/skills/agents の drift、
REQUIRED_FILES の整合性、本体→template の配布追加漏れ、
SKILL.md サブコマンド実在）を自動検証する。

終了コード:
- 0 → 全チェックがパスした（stdout に "health-check: all checks passed"）
- 1 → 失敗あり（stderr に prefix 付き行を出力）
"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tidd_tools import timing_log
from tidd_tools.ai_review.backends import is_codex_stub
from tidd_tools.deprecated_config_registry import DEPRECATED_ENV_VARS
from tidd_tools.sandbox_copier_poc import (
    EXCLUDED_FILES,
    REQUIRED_BY_FLAG,
    REQUIRED_FILES,
)
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.subprocess_runner import run as run_subprocess
from tidd_tools.shared.subprocess_timeouts import STANDARD_TIMEOUT_SEC

# 記録断絶検知の対象 step（Issue #3385）。record-timing-boundaries.py が自動記録する
# 3 マークのうち、merge_summary.py がフェーズ行の区間端として使う step。
_TIMING_BOUNDARY_STEPS: tuple[str, ...] = (
    "step2-branch-created",
    "step4-pr-created",
    "step5-airview-end",
)
_TIMING_BOUNDARY_WINDOW_DAYS = 7

# template 側にのみ存在し、ソース（.claude/hooks/）には置かれない hook。
# consumer は copier copy で受け取る配布物だが、ai-dev-handbook 本体には不在。
TEMPLATE_ONLY_HOOKS: frozenset[str] = frozenset({"notify-copier-staleness.py"})

# 意図的に本体と template で内容を divergent にする hook（存在は両側必須・内容比較のみ skip）。
# require-merge-ci-status.py: mermaid-lint/docs 必須化（#2794）は本体限定の運用。
# mermaid lint 未導入の consumer に配布すると `.md` 変更だけで永久にマージブロックされるため、
# 本体にのみ mermaid-lint/docs 判定を追加し template 側は配布しない
# （Issue #2794「設計の選択肢」参照）。tests/test_sandbox_copier_drift.py の
# HOOK_ALLOWED_TO_DIVERGE と同期させること。
HOOK_ALLOWED_TO_DIVERGE: frozenset[str] = frozenset({"require-merge-ci-status.py"})

# `settings.managed.json.jinja` の hooks 登録で意図的に本家 `.claude/settings.json` と
# 乖離させる hook（`_check_managed_settings_drift` が照合から除外する・#3566）。
# 本家にのみ登録される（consumer 配布不要の maintainer 専用）:
# - notify-template-sync.py: maintainer 専用（consumer の template drift 通知は notify-copier-staleness が担当・#2239）
# template にのみ登録される（consumer 専用・#1224）:
# - notify-copier-staleness.py: copier 更新通知（本家には SessionStart で使わない）
MANAGED_HOOKS_ALLOWED_DIVERGENT: frozenset[str] = frozenset(
    {
        "notify-template-sync.py",
        "notify-copier-staleness.py",
    }
)

# 本体 `.claude/` にのみ存在し template（consumer）へ配布しない skill/agent。
# ai-dev-handbook 固有（agy/tidd_tools 固有依存）のため配布対象外（#2212）。
# tests/test_sandbox_copier_skills.py の EXCLUDED_SKILL_DIRS / EXCLUDED_AGENT_FILES と
# 同期させること（片方のみ更新すると drift 検知テストが FAIL する。実例: #3339 で
# publish を平ファイル→ディレクトリ化した際に test 側の追記が漏れ #3367 で nightly が FAIL した）。
UNDISTRIBUTED_SKILL_DIRS: frozenset[str] = frozenset(
    {
        "weekly-audit",
        "slack-url-evaluator",
        "proofread",  # 部内 tips 執筆向け・docs/share_tips/ 固有パス依存のため配布しない (#2775)
        "publish",  # Google Drive + 特定 GAS webapp 依存のため配布しない（#3339）
        # verify-post-merge は #3766 で配布対象化済み（session-start-verify-post-merge hook
        # が consumer に配布済みのため、対になる skill も必要）。
    }
)
UNDISTRIBUTED_AGENT_FILES: frozenset[str] = frozenset(
    {
        # yaru-verifier.md は #3763 で配布対象化済み（yaru-auto-tick は consumer 側の
        # config.json opt-in 機能のため consumer にも subagent persona が必要）。
        # post-merge-verifier.md は #3766 で配布対象化済み（verify-post-merge skill の依存）。
    }
)

# 本体 `.codex/agents/` にのみ存在し template（consumer）へ配布しない agent（Issue #3332）。
# UNDISTRIBUTED_AGENT_FILES（.claude/agents 側）と同じ基準を Codex 側にも適用する。
CODEX_UNDISTRIBUTED_AGENT_FILES: frozenset[str] = frozenset(
    {
        # yaru_verifier.toml は #3763 で配布対象化済み（UNDISTRIBUTED_AGENT_FILES 参照）。
        # post_merge_verifier.toml も #3766 で配布対象化済み。
    }
)

# 意図的に template と src で byte 不一致にする rules yaml（#2239）。
# hardcoded-patterns.yaml は org 固有パターンを src（本体運用）にのみ残し、
# 配布版（template）は skeleton にする。tests/test_sandbox_copier_drift.py の
# RULE_YAML_ALLOWED_TO_DIVERGE と同期させること。
RULE_YAML_ALLOWED_TO_DIVERGE: frozenset[str] = frozenset({"hardcoded-patterns.yaml"})

# 静的パスだが choice 質問（primary_language 以外）の回答値により配布可否が
# 分岐するファイル（#2254: ci_provider=github-actions のときのみ配布される ci.yml。
# #2274: use_coderabbit=true のときのみ配布される .coderabbit.yaml。
# #2276: use_security_policy=true のときのみ配布される SECURITY.md）。
# `python_package_name` のようにディレクトリ名自体が Jinja でないため
# `_check_required_files` の "{{" 判定では拾えない。配布可否・配布内容の検証は
# 専用の BDD テスト（tests/step_defs/test_issue_2254.py・test_issue_2274.py・
# test_issue_2276.py）に委ねるため対象外にする。
CONDITIONALLY_DISTRIBUTED_FILES: frozenset[str] = frozenset(
    {".github/workflows/ci.yml", ".coderabbit.yaml", "SECURITY.md", ".github/workflows/scorecard.yml"}
)

# サブコマンド名マッチ用正規表現（英小文字 + 数字 + ハイフン）
_CMD_RE = re.compile(r"python -m tidd_tools ([a-z][a-z0-9-]+)|tidd ([a-z][a-z0-9-]+)")

# "tidd_tools" 等の誤検知トークンを除外
_CMD_EXCLUDE: frozenset[str] = frozenset({"tidd_tools"})


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """CLI に health-check サブコマンドを登録する."""
    parser = subparsers.add_parser(
        "health-check",
        help="リポジトリ内の workflow 動作依存（hooks drift・REQUIRED_FILES 整合・コマンド実在）を検証する",
        description=__doc__,
    )
    add_common_flags(parser)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="リポジトリルートのパス（省略時は git rev-parse --show-toplevel）",
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    """health-check のエントリポイント。0 = 全合格、1 = 失敗あり。"""
    repo_root = _resolve_repo_root(getattr(args, "repo_root", None))
    failures: list[str] = []

    template_dir = repo_root / "templates" / "workflow"
    if template_dir.is_dir():
        failures.extend(_check_hooks(repo_root, template_dir))
        failures.extend(_check_rules(repo_root, template_dir))
        failures.extend(_check_skills(repo_root, template_dir))
        failures.extend(_check_flat_skill_files(repo_root))
        failures.extend(_check_agents(repo_root, template_dir))
        failures.extend(_check_codex_agents(repo_root, template_dir))
        failures.extend(_check_required_files(template_dir))
        failures.extend(_check_reverse_distribution(repo_root, template_dir))
        failures.extend(_check_global_tidd_editable_install(repo_root))
        failures.extend(_check_agents_skills_symlink(repo_root))
        failures.extend(_check_rulesync_drift(repo_root, rulesync_bin=_resolve_rulesync_bin(repo_root)))
        # MANAGED 部分（permissions.deny・hooks・env・statusLine）の本家乖離検出（#3566）
        failures.extend(_check_managed_settings_drift(repo_root))

    skill_md = repo_root / ".claude" / "skills" / "issue-next" / "SKILL.md"
    if skill_md.is_file():
        failures.extend(_check_missing_commands(skill_md))

    # maintainer/consumer どちらの環境でも実行する（PATH 環境依存でリポジトリ構成に依らないため）
    failures.extend(_check_tidd_path_shadowing())

    # .claude/ ↔ .codex/ の構造パリティ（templates/workflow の有無に依らず実行する・#3338）
    failures.extend(_check_codex_parity(repo_root))

    # AGENTS.md の Codex project_doc_max_bytes 上限ガード（maintainer/consumer どちらでも実行・#3406）
    failures.extend(_check_agents_md_size(repo_root))

    # root CLAUDE.md ブリッジの欠落検知（maintainer/consumer どちらでも実行・CI では skip・#3407）
    failures.extend(_check_root_claude_md(repo_root))

    # settings.json が参照する hook ファイルの実在検証（maintainer/consumer どちらでも実行・#3965）
    failures.extend(_check_settings_hook_files_exist(repo_root))

    # 計測境界マークの記録断絶検知（maintainer/consumer どちらでも実行・Issue #3385）。
    # メッセージは stdout にも明示出力する（stderr のみだと「計測不可」と正常系の区別が
    # つかず断絶が数週間見過ごされた再発防止・Issue #3385 背景）。
    timing_gap_messages = _check_timing_boundary_freshness()
    for line in timing_gap_messages:
        print(line)
    failures.extend(timing_gap_messages)

    warnings = _check_codex_stub()
    for line in warnings:
        print(line, file=sys.stderr)

    deprecated_warnings = _check_deprecated_env_vars()
    for line in deprecated_warnings:
        print(line, file=sys.stderr)

    if failures:
        for line in failures:
            print(line, file=sys.stderr)
        return 1

    print("health-check: all checks passed")
    return 0


# ── チェック 1: hooks バイト一致 ────────────────────────────────────────────


def _check_hooks(repo_root: Path, template_dir: Path) -> list[str]:
    """templates/workflow/.claude/hooks/ ↔ .claude/hooks/ を双方向で照合する.

    直下の .py は byte 一致まで確認する。_lib/ 配下はファイル集合のみ確認する
    （既存の test_sandbox_copier_drift.py と同じ基準）。
    """
    failures: list[str] = []
    tmpl_hooks = template_dir / ".claude" / "hooks"
    src_hooks = repo_root / ".claude" / "hooks"

    if not tmpl_hooks.is_dir() or not src_hooks.is_dir():
        return failures

    def _direct_py(root: Path) -> dict[str, Path]:
        return {p.name: p for p in root.glob("*.py") if p.is_file()}

    def _lib_py_names(root: Path) -> set[str]:
        lib = root / "_lib"
        if not lib.is_dir():
            return set()
        return {p.name for p in lib.glob("*.py") if p.is_file()}

    tmpl_direct = _direct_py(tmpl_hooks)
    src_direct = _direct_py(src_hooks)

    # template 専用 hook を除外
    tmpl_names = {k for k in tmpl_direct if k not in TEMPLATE_ONLY_HOOKS}
    src_names = set(src_direct.keys())

    # template → src: 欠落または内容差分（直下 .py のみ byte 比較。
    # HOOK_ALLOWED_TO_DIVERGE は存在確認のみ行い内容比較は skip する）
    for rel in sorted(tmpl_names):
        if rel not in src_names:
            failures.append(f"DRIFT: .claude/hooks/{rel} (src にない)")
            continue
        if rel in HOOK_ALLOWED_TO_DIVERGE:
            continue
        if tmpl_direct[rel].read_bytes() != src_direct[rel].read_bytes():
            failures.append(f"DRIFT: .claude/hooks/{rel}")

    # src → template: src にあるが template にない
    for rel in sorted(src_names - tmpl_names - TEMPLATE_ONLY_HOOKS):
        failures.append(f"DRIFT: .claude/hooks/{rel} (template にない)")

    # _lib/: ファイル集合の対称差分のみ確認
    tmpl_lib = _lib_py_names(tmpl_hooks)
    src_lib = _lib_py_names(src_hooks)
    for rel in sorted(tmpl_lib - src_lib):
        failures.append(f"DRIFT: .claude/hooks/_lib/{rel} (src にない)")
    for rel in sorted(src_lib - tmpl_lib):
        failures.append(f"DRIFT: .claude/hooks/_lib/{rel} (template にない)")

    return failures


# ── チェック 1.5: settings.managed.json.jinja の MANAGED 部分乖離 ────────────


_HOOK_NAME_RE = re.compile(r"/hooks/([a-zA-Z0-9_-]+\.py)")


def _managed_hook_groups(settings: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
    """`hooks.<event>[].hooks[].command` から (event, matcher) → hook 名集合 を抽出する."""
    groups: dict[tuple[str, str], set[str]] = {}
    for event, entries in settings.get("hooks", {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = str(entry.get("matcher", ""))
            names: set[str] = set()
            for h in entry.get("hooks", []):
                if not isinstance(h, dict):
                    continue
                m = _HOOK_NAME_RE.search(str(h.get("command", "")))
                if m:
                    names.add(m.group(1))
            groups.setdefault((event, matcher), set()).update(names)
    return groups


def _check_managed_settings_drift(repo_root: Path) -> list[str]:
    """`.claude/settings.json`（本家）と `settings.managed.json.jinja` の MANAGED 部分乖離を検出する.

    Issue #3566: MANAGED 部分（`permissions.deny`・`hooks`・`env`・`statusLine`）は
    consumer の `.claude/settings.json` にそのまま反映されるため、本家と乖離すると
    consumer で計測境界の欠落（record-timing-boundaries 未登録）や `CODEX_PARITY` 常時警告が
    発生する。`tidd sync-template` は `.claude/**` のファイル本体を対象とし
    `settings.managed.json.jinja` は対象外のため、登録反映漏れを機械検出する。

    意図的な乖離（maintainer 専用・consumer 専用 hook）は
    `MANAGED_HOOKS_ALLOWED_DIVERGENT` で除外する。
    """
    import json

    settings_path = repo_root / ".claude" / "settings.json"
    managed_path = repo_root / "templates" / "workflow" / ".claude" / "settings.managed.json.jinja"
    if not settings_path.is_file() or not managed_path.is_file():
        return []

    try:
        src = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"CODEX_PARITY: .claude/settings.json の読み込みに失敗しました: {exc}"]
    try:
        managed = json.loads(managed_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"CODEX_PARITY: settings.managed.json.jinja の読み込みに失敗しました: {exc}"]

    failures: list[str] = []
    failures.extend(_check_managed_hook_registrations(src, managed))
    failures.extend(_check_managed_permissions_deny(src, managed))
    failures.extend(_check_managed_top_level_keys(src, managed))
    return failures


def _check_managed_hook_registrations(src: dict[str, Any], managed: dict[str, Any]) -> list[str]:
    """hooks の (event, matcher) ごとに本家と template の hook 登録を照合する.

    `test_settings_managed_hook_registration.py` の集合ベース照合（#2229）は
    hook 名の全体集合しか比較しないため、`record-timing-boundaries` のように
    別 matcher に登録済みだと matcher 単位の欠落を検出できない（#3566 背景）。
    本チェックは (event, matcher) 単位で比較する。
    """
    failures: list[str] = []
    src_groups = _managed_hook_groups(src)
    managed_groups = _managed_hook_groups(managed)
    for key in sorted(set(src_groups) | set(managed_groups)):
        event, matcher = key
        src_names = src_groups.get(key, set())
        managed_names = managed_groups.get(key, set())
        for name in sorted(src_names - managed_names - MANAGED_HOOKS_ALLOWED_DIVERGENT):
            failures.append(
                f"CODEX_PARITY: settings.managed.json.jinja の hooks/{event}/{matcher} に {name} が"
                " 登録されていません（本家 .claude/settings.json には登録済み）"
            )
        for name in sorted(managed_names - src_names - MANAGED_HOOKS_ALLOWED_DIVERGENT):
            failures.append(
                f"CODEX_PARITY: settings.managed.json.jinja の hooks/{event}/{matcher} にのみ {name} が"
                " 登録されています（本家 .claude/settings.json にはありません）"
            )
    return failures


def _check_managed_permissions_deny(src: dict[str, Any], managed: dict[str, Any]) -> list[str]:
    """`permissions.deny`（MANAGED）を本家と template で照合する.

    `permissions.allow`・`defaultMode` は PRESERVED（consumer 独自）のため対象外
    （merge_settings.py の `extract_preserved` と同じ基準）。
    """
    failures: list[str] = []
    src_deny = set(src.get("permissions", {}).get("deny", []))
    managed_deny = set(managed.get("permissions", {}).get("deny", []))
    for entry in sorted(src_deny - managed_deny):
        failures.append(
            f"CODEX_PARITY: settings.managed.json.jinja の permissions.deny に {entry} が"
            " ありません（本家 .claude/settings.json には登録済み）"
        )
    for entry in sorted(managed_deny - src_deny):
        failures.append(
            f"CODEX_PARITY: settings.managed.json.jinja の permissions.deny に本家にない {entry} が含まれています"
        )
    return failures


def _check_managed_top_level_keys(src: dict[str, Any], managed: dict[str, Any]) -> list[str]:
    """`env`・`statusLine` 等のスカラーレベル MANAGED キーを照合する（#3566）.

    現状どちらのファイルにも存在しないため通常は no-op だが、いずれか片方にだけ
    追加された場合に乖離として検出する（`MANAGED_TOP_KEYS` と同じ対象・merge_settings.py）。
    """
    failures: list[str] = []
    for key in ("env", "statusLine"):
        src_val = src.get(key)
        managed_val = managed.get(key)
        if src_val == managed_val:
            continue
        if src_val is not None:
            failures.append(
                f"CODEX_PARITY: settings.managed.json.jinja の {key} が本家 .claude/settings.json と一致しません"
                "（本家側にのみ設定があります）"
            )
        elif managed_val is not None:
            failures.append(
                f"CODEX_PARITY: settings.managed.json.jinja にのみ {key} が設定されています"
                "（本家 .claude/settings.json にはありません）"
            )
    return failures


# ── チェック 2: rules 照合 ───────────────────────────────────────────────────


def _check_rules(repo_root: Path, template_dir: Path) -> list[str]:
    """templates/workflow/.claude/rules/ を正として .claude/rules/ を照合する.

    .yaml は byte 一致まで確認する。.md はファイル存在のみ確認する
    （.md は URL 変換等で内容が異なりうるため。既存テストと同一基準）。
    """
    failures: list[str] = []
    tmpl_rules = template_dir / ".claude" / "rules"
    src_rules = repo_root / ".claude" / "rules"

    if not tmpl_rules.is_dir():
        return failures

    for tmpl_file in sorted(tmpl_rules.iterdir()):
        if not tmpl_file.is_file():
            continue
        rel = tmpl_file.name
        src_file = src_rules / rel
        if not src_file.is_file():
            failures.append(f"DRIFT: .claude/rules/{rel} (src に存在しない)")
            continue
        # yaml のみ byte 比較（md は URL 変換で異なりうるため存在確認のみ）
        if (
            rel.endswith(".yaml")
            and rel not in RULE_YAML_ALLOWED_TO_DIVERGE
            and tmpl_file.read_bytes() != src_file.read_bytes()
        ):
            failures.append(f"DRIFT: .claude/rules/{rel}")

    return failures


# ── チェック 3: skills 照合 ──────────────────────────────────────────────────


def _check_skills(repo_root: Path, template_dir: Path) -> list[str]:
    """templates/workflow/.claude/skills/ を正として .claude/skills/ を照合する."""
    return _check_dotclaude_subdir(
        repo_root,
        template_dir,
        subdir="skills",
    )


def _check_flat_skill_files(repo_root: Path) -> list[str]:
    """`.claude/skills/` 直下のフラットな `.md` ファイル（規約違反）を検出する（#3339）.

    スキルは `<skill-name>/SKILL.md` のディレクトリ形式が規約で、
    `validate-skill.py` も `.claude/skills/**/SKILL.md` のみ検証対象とする。
    直下のフラットな `.md` は検証対象から外れ、スキルとして認識されない
    可能性があるため failure として報告する。
    """
    skills_dir = repo_root / ".claude" / "skills"
    if not skills_dir.is_dir():
        return []
    failures: list[str] = []
    for p in sorted(skills_dir.iterdir()):
        if p.is_file() and p.suffix == ".md":
            failures.append(
                f"FLAT_SKILL: .claude/skills/{p.name} は規約違反です"
                "（スキルは <skill-name>/SKILL.md 形式に配置してください・#3339）"
            )
    return failures


# ── チェック 4: agents 照合 ──────────────────────────────────────────────────


def _check_agents(repo_root: Path, template_dir: Path) -> list[str]:
    """templates/workflow/.claude/agents/ を正として .claude/agents/ を照合する."""
    return _check_dotclaude_subdir(
        repo_root,
        template_dir,
        subdir="agents",
    )


def _check_codex_agents(repo_root: Path, template_dir: Path) -> list[str]:
    """templates/workflow/.codex/agents/ を正として .codex/agents/ を照合する（Issue #3332）."""
    failures: list[str] = []
    tmpl_dir = template_dir / ".codex" / "agents"
    src_dir = repo_root / ".codex" / "agents"

    if not tmpl_dir.is_dir():
        return failures

    for tmpl_file in sorted(tmpl_dir.iterdir()):
        if not tmpl_file.is_file():
            continue
        rel = tmpl_file.name
        src_file = src_dir / rel
        if not src_file.is_file():
            failures.append(f"DRIFT: .codex/agents/{rel} (src に存在しない)")
            continue
        if tmpl_file.read_bytes() != src_file.read_bytes():
            failures.append(f"DRIFT: .codex/agents/{rel}")

    return failures


def _check_dotclaude_subdir(repo_root: Path, template_dir: Path, *, subdir: str) -> list[str]:
    """.claude/<subdir>/ を template 側を正として照合する共通ロジック."""
    failures: list[str] = []
    tmpl_dir = template_dir / ".claude" / subdir
    src_dir = repo_root / ".claude" / subdir

    if not tmpl_dir.is_dir():
        return failures

    for tmpl_file in sorted(tmpl_dir.rglob("*")):
        if not tmpl_file.is_file():
            continue
        rel = tmpl_file.relative_to(tmpl_dir)
        rel_str = str(rel)

        if rel_str.endswith(".jinja"):
            # .jinja テンプレートは .jinja を除いた名前が本体側に存在するか確認のみ
            body_name = rel_str[: -len(".jinja")]
            src_file = src_dir / body_name
            if not src_file.exists():
                failures.append(f"DRIFT: .claude/{subdir}/{body_name} (src に存在しない)")
        else:
            src_file = src_dir / rel
            if not src_file.is_file():
                failures.append(f"DRIFT: .claude/{subdir}/{rel_str} (src に存在しない)")
                continue
            if tmpl_file.read_bytes() != src_file.read_bytes():
                failures.append(f"DRIFT: .claude/{subdir}/{rel_str}")

    return failures


# ── チェック 5: REQUIRED_FILES 動的照合 ─────────────────────────────────────


def _check_required_files(template_dir: Path) -> list[str]:
    """template 走査と REQUIRED_FILES の整合性を確認する.

    `.copier-answers.yml` は copier が生成するため走査対象外。
    `.claude/settings.json` は copier _tasks（merge_settings.py）が生成するため、
    template 側には `settings.managed.json.jinja` として存在する（特別扱い）。
    """
    failures: list[str] = []

    # copier が生成するファイル（template 走査では見つからない）を REQUIRED_FILES から除外
    copier_generated: frozenset[str] = frozenset({".copier-answers.yml"})
    # merge_settings.py 等の _tasks で生成されるファイル → template に別名 source がある
    # #3711: AGENTS.md は copier `_tasks` の generate_rulesync_agents.py が rulesync で
    # `.rulesync/rules/*.md`（overview.md.jinja + 9 本）から生成する。
    tasks_generated: dict[str, str] = {
        ".claude/settings.json": ".claude/settings.managed.json.jinja",
        "AGENTS.md": ".rulesync/rules/overview.md.jinja",
    }

    # template を再帰走査してファイル集合を導出
    template_files: set[str] = set()
    for p in template_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(template_dir)
        # #3888: Windows ネイティブでは str(rel) がバックスラッシュ区切りになり、
        # スラッシュ区切りで定義された REQUIRED_FILES と 1 件も一致しなくなるため
        # as_posix() で正規化する。
        rel_str = rel.as_posix()

        # EXCLUDED_FILES に該当するか確認（.jinja suffix の有無にかかわらず）
        if _is_excluded(rel_str):
            continue

        # .jinja suffix を剥がして正規化
        if rel_str.endswith(".jinja"):
            rel_str = rel_str[: -len(".jinja")]
            # 正規化後も EXCLUDED に該当するか再確認（例: settings.managed.json.jinja）
            if _is_excluded(rel_str):
                continue

        # copier が生成するファイルは除外（.copier-answers.yml.jinja → .copier-answers.yml 等）
        if rel_str in copier_generated:
            continue

        # #2257: ディレクトリ名自体が Jinja テンプレート（例: `projects/py/{{ python_package_name }}/`）
        # の場合、consumer 側の実パスは回答値によって変わるため REQUIRED_FILES の
        # 静的な文字列一致では照合できない。動的パスは配布可否・配布内容の検証を
        # 専用の BDD テスト（tests/step_defs/test_issue_2257.py 等）に委ねるため、
        # このチェックの対象外とする。
        if "{{" in rel_str:
            continue

        # #2254: ci_provider=github-actions のときのみ配布される静的パスファイル
        if rel_str in CONDITIONALLY_DISTRIBUTED_FILES:
            continue

        template_files.add(rel_str)

    # _tasks 生成ファイルを REQUIRED_FILES から除去し、代わりに template source の存在で検証
    # opt-in フラグ配下のファイル（issue-next・ai-review 等）も配布登録対象として扱う（#3243）
    required_set = (set(REQUIRED_FILES) | {f for files in REQUIRED_BY_FLAG.values() for f in files}) - copier_generated
    for generated, source in tasks_generated.items():
        if generated not in required_set:
            continue
        required_set.discard(generated)
        source_path = template_dir / source
        if source_path.is_file():
            # source が存在する → _tasks 実行後に生成されるため合格扱い
            # required_set から除去済みなので照合不要
            pass
        else:
            failures.append(f"MISSING_TEMPLATE: {generated} (source: {source})")

    # template にあるが REQUIRED_FILES 未登録
    for rel_str in sorted(template_files - required_set):
        failures.append(f"UNREGISTERED: {rel_str}")

    # REQUIRED_FILES にあるが template にない
    for rel_str in sorted(required_set - template_files):
        direct = template_dir / rel_str
        jinja = template_dir / f"{rel_str}.jinja"
        if not direct.is_file() and not jinja.is_file():
            failures.append(f"MISSING_TEMPLATE: {rel_str}")

    return failures


def _is_excluded(rel_str: str) -> bool:
    """EXCLUDED_FILES に該当するパスかどうか判定する.

    EXCLUDED_FILES のエントリはプレフィックス一致（ディレクトリ名）と
    完全一致の両方をカバーする。
    """
    for exc in EXCLUDED_FILES:
        if rel_str == exc:
            return True
        # ディレクトリプレフィックス（末尾 / なし）
        if rel_str.startswith(exc + "/") or rel_str.startswith(exc + "\\"):
            return True
        # 末尾がファイル名（basename）一致（copier.yml 等の単純ファイル名指定）
        # #3888: rel_str がバックスラッシュ区切り（Windows）のまま渡される可能性も
        # 考慮し、パス区切りに依存せず basename を抽出する。
        if "/" not in exc and rel_str.replace("\\", "/").split("/")[-1] == exc:
            return True
    return False


# ── チェック 6: 本体→template 逆方向照合（配布追加漏れ検出） ─────────────────


def _check_reverse_distribution(repo_root: Path, template_dir: Path) -> list[str]:
    """本体 `.claude/` にのみ存在し template に配布されていないファイルを検出する.

    `_check_rules` / `_check_skills` / `_check_agents` は template を正として
    本体側の欠落・drift のみ検出する片方向照合のため、本体に新規追加した
    rule/skill/agent の配布追加漏れ（逆方向）を検出できなかった（Issue #2212）。
    """
    failures: list[str] = []
    failures.extend(_check_reverse_skills(repo_root, template_dir))
    failures.extend(_check_reverse_agents(repo_root, template_dir))
    failures.extend(_check_reverse_codex_agents(repo_root, template_dir))
    failures.extend(_check_reverse_rules(repo_root, template_dir))
    failures.extend(_check_consumer_agents_md_size(repo_root, template_dir))
    return failures


def _check_reverse_skills(repo_root: Path, template_dir: Path) -> list[str]:
    """本体 `.claude/skills/` にのみ存在するディレクトリ（配布対象）を検出する."""
    src_skills = repo_root / ".claude" / "skills"
    tmpl_skills = template_dir / ".claude" / "skills"
    if not src_skills.is_dir():
        return []

    failures: list[str] = []
    for entry in sorted(src_skills.iterdir()):
        if not entry.is_dir() or entry.name in UNDISTRIBUTED_SKILL_DIRS:
            continue
        if not (tmpl_skills / entry.name).is_dir():
            failures.append(f"UNDISTRIBUTED: .claude/skills/{entry.name}")
    return failures


def _check_reverse_agents(repo_root: Path, template_dir: Path) -> list[str]:
    """本体 `.claude/agents/` にのみ存在するファイル（配布対象）を検出する.

    template 側が `.jinja` テンプレート化されている場合（例: `agent.md.jinja`）も
    配布済みとみなす（`_check_dotclaude_subdir` の .jinja 剥がしと同じ基準）。
    """
    src_agents = repo_root / ".claude" / "agents"
    tmpl_agents = template_dir / ".claude" / "agents"
    if not src_agents.is_dir():
        return []

    failures: list[str] = []
    for entry in sorted(src_agents.iterdir()):
        if not entry.is_file() or entry.name in UNDISTRIBUTED_AGENT_FILES:
            continue
        if (tmpl_agents / entry.name).is_file() or (tmpl_agents / f"{entry.name}.jinja").is_file():
            continue
        failures.append(f"UNDISTRIBUTED: .claude/agents/{entry.name}")
    return failures


def _check_reverse_codex_agents(repo_root: Path, template_dir: Path) -> list[str]:
    """本体 `.codex/agents/` にのみ存在するファイル（配布対象）を検出する（Issue #3332）."""
    src_agents = repo_root / ".codex" / "agents"
    tmpl_agents = template_dir / ".codex" / "agents"
    if not src_agents.is_dir():
        return []

    failures: list[str] = []
    for entry in sorted(src_agents.iterdir()):
        if not entry.is_file() or entry.name in CODEX_UNDISTRIBUTED_AGENT_FILES:
            continue
        if (tmpl_agents / entry.name).is_file():
            continue
        failures.append(f"UNDISTRIBUTED: .codex/agents/{entry.name}")
    return failures


def _check_reverse_rules(repo_root: Path, template_dir: Path) -> list[str]:
    """本体 `.claude/rules/` にのみ存在するファイル（配布対象）を検出する."""
    src_rules = repo_root / ".claude" / "rules"
    tmpl_rules = template_dir / ".claude" / "rules"
    if not src_rules.is_dir():
        return []

    failures: list[str] = []
    for entry in sorted(src_rules.iterdir()):
        if not entry.is_file():
            continue
        if not (tmpl_rules / entry.name).is_file():
            failures.append(f"UNDISTRIBUTED: .claude/rules/{entry.name}")
    return failures


def _render_overview_jinja(template_dir: Path) -> str:
    """`templates/workflow/.rulesync/rules/overview.md.jinja` を consumer 相当の値でレンダリングする.

    tidd_tools は jinja2 に依存しない（#3565）ため、テンプレート内の変数を
    文字列置換で代表値に置き換える。変数は repo-specific ブロックの
    `{{ project_name }}`（h1）と `{{ github_org }}` のみ。consumer の実名が
    どれほど長くても 32KiB 判定に大きな影響を与えない代表値を使う。
    """
    jinja = template_dir / ".rulesync" / "rules" / "overview.md.jinja"
    return (
        jinja.read_text(encoding="utf-8")
        .replace("{{ project_name }}", "your-project")
        .replace("{{ github_org }}", "your-org")
    )


def _frontmatter_targets(text: str) -> set[str]:
    """frontmatter の `targets:` リスト値の集合を返す（クォート除去・#3770）."""
    frontmatter = text.split("---", 2)[1] if text.startswith("---") else ""
    m = re.search(r"targets:\s*\n((?:\s*-\s*.+\n?)+)", frontmatter)
    if not m:
        return set()
    return {v.strip().strip("'\"") for v in re.findall(r"-\s*(.+)", m.group(1))}


def _is_codexcli_target(text: str) -> bool:
    """rule ファイルの frontmatter が `codexcli`/`*` ターゲットを含むか判定する（#3770）.

    #3770 で detail rule 本文（`.claude/rules/*.md` 9 本）は `targets: ['claudecode']`
    にして consumer 生成 `AGENTS.md`（codexcli 生成物）のインライン展開から除外した。
    本関数は実 rulesync の `--targets codexcli` 絞り込みをこの検証でも再現する。
    """
    targets = _frontmatter_targets(text)
    return "codexcli" in targets or "*" in targets


def _render_consumer_agents_md(template_dir: Path) -> str:
    """consumer 配布 AGENTS.md の生成後本文（概算）を返す（#3711・#3770）.

    consumer の `AGENTS.md` は rulesync が `.rulesync/rules/*.md` のうち
    `targets` に `codexcli`/`*` を含むファイル（既定では overview.md のみ・#3770）を
    生成する。本関数は frontmatter を除いた本文を連結した概算サイズを返す。
    厳密な rulesync 出力とは数バイトの差があるが、32KiB 判定のガードとしては
    十分な精度を持つ。
    """
    overview = _render_overview_jinja(template_dir)
    body = overview.split("---", 2)[2] if overview.startswith("---") else overview
    parts = [body]
    for rule in sorted((template_dir / ".rulesync" / "rules").glob("*.md")):
        text = rule.read_text(encoding="utf-8")
        if not _is_codexcli_target(text):
            continue
        if text.startswith("---"):
            text = text.split("---", 2)[2]
        parts.append(text)
    return "".join(parts)


def _check_consumer_agents_md_size(repo_root: Path, template_dir: Path) -> list[str]:
    """consumer 配布 AGENTS.md（rulesync 生成物）のサイズが Codex 32KiB 上限を超えないか検証する.

    Issue #3565 の後継（#3711）: 従来は `AGENTS.md.jinja` を検証していたが、consumer の
    `AGENTS.md` は rulesync が `.rulesync/rules/*.md`（overview.md + 9 本）を flat 展開して
    生成するようになった。本体 `AGENTS.md` の上限ガード（`_check_agents_md_size`・#3406）は
    配布物には適用されず、配布物だけが 32,768 bytes を超えると配布先 Codex で無音切り詰めが
    起き、末尾に展開されるルールが Codex にだけ適用されない片対応状態になる。
    """
    overview_jinja = template_dir / ".rulesync" / "rules" / "overview.md.jinja"
    if not overview_jinja.is_file():
        return ["MISSING_TEMPLATE: .rulesync/rules/overview.md.jinja"]
    if not (template_dir / ".rulesync" / "rules").is_dir():
        return []
    size = len(_render_consumer_agents_md(template_dir).encode("utf-8"))
    if size > _AGENTS_MD_LIMIT_BYTES:
        return [
            f"consumer 配布 AGENTS.md の生成後サイズが Codex の project_doc_max_bytes 上限"
            f" ({_AGENTS_MD_LIMIT_BYTES}) を超過（現在 {size} bytes・超過 {size - _AGENTS_MD_LIMIT_BYTES} bytes）。"
            " 対象: templates/workflow/.rulesync/rules/（overview.md.jinja + 9 本）。"
            " 詳細記述の外部 URL 参照化・重複節の削除で上限未満に収めてください（#3711）"
        ]
    return []


# ── チェック 7: issue-next サブコマンド実在確認 ─────────────────────────────


def _check_missing_commands(skill_md: Path) -> list[str]:
    """SKILL.md のサブコマンド参照と entry_points の照合."""
    failures: list[str] = []
    content = skill_md.read_text(encoding="utf-8")

    # entry_points から実装済みコマンド名集合を取得
    eps = importlib.metadata.entry_points(group="tidd_tools.commands")
    implemented: set[str] = {ep.name for ep in eps}

    # SKILL.md からコマンド参照を抽出
    mentioned: set[str] = set()
    for m in _CMD_RE.finditer(content):
        cmd = m.group(1) or m.group(2)
        if cmd and cmd not in _CMD_EXCLUDE:
            mentioned.add(cmd)

    for cmd in sorted(mentioned - implemented):
        failures.append(f"MISSING_CMD: {cmd}")

    return failures


# ── チェック codex stub 検知 ────────────────────────────────────────────────


def _check_codex_stub() -> list[str]:
    """PATH 上の codex が shell script stub かどうかを検知し警告を返す.

    Issue #2527: fake APPROVE を防ぐため、セッション開始時に stub を警告する。
    失敗（failures）ではなく警告（warnings）として扱う（exit code には影響しない）。
    """
    codex_path = shutil.which("codex")
    if codex_path is None:
        return []
    if is_codex_stub(codex_path):
        return [
            f"WARN: codex backend が stub（shell script）を検知しました（{codex_path}）。"
            " `tidd ai-review` が fake APPROVE を返す可能性があります。"
            " 実 codex CLI をインストールするか"
            " `tidd config disable ai-review-codex --repo または --machine` を実行してください。",
        ]
    return []


# ── チェック: AGENTS.md の Codex 32KiB 上限ガード（Issue #3406） ────────────

# Codex の project_doc_max_bytes デフォルト上限（bytes）。超えると無音切り詰めが起き、
# 末尾のルールが Codex にだけ適用されない片対応状態になる（docs/reference/codex-interop.md §4-4）。
_AGENTS_MD_LIMIT_BYTES = 32768
# 上限の 90%（警告閾値・bytes）。エラーにはせず stderr へ警告のみ出力する。
_AGENTS_MD_WARN_THRESHOLD_BYTES = 30720


def _check_agents_md_size(repo_root: Path) -> list[str]:
    """`AGENTS.md` が Codex の `project_doc_max_bytes` 上限を超えていないか検証する.

    Issue #3406: 上限超過は Codex 側で無音切り詰めが起き、末尾のルールが Codex にだけ
    適用されない片対応状態になるがサイズを検証するガードが無かった。

    - 32,768 bytes 超 → failure を返す
    - 30,720 bytes（30 KiB）超 32,768 bytes 以下 → stderr へ警告のみ出力（failure なし）
    - AGENTS.md 不在 → skip（他 check がエラーを出すため二重報告しない）
    """
    agents_md = repo_root / "AGENTS.md"
    if not agents_md.is_file():
        return []

    size = agents_md.stat().st_size
    if size > _AGENTS_MD_LIMIT_BYTES:
        return [
            f"AGENTS.md が Codex の project_doc_max_bytes 上限 ({_AGENTS_MD_LIMIT_BYTES})"
            f" を超過（現在 {size} bytes）。ルール分割または上限引き上げが必要"
        ]
    if size > _AGENTS_MD_WARN_THRESHOLD_BYTES:
        print(
            f"WARN: AGENTS.md が {size} bytes で Codex の project_doc_max_bytes 上限"
            f" ({_AGENTS_MD_LIMIT_BYTES}) の 90% に到達しています。ルール追記の余地が少ないため"
            " 分割を検討してください。",
            file=sys.stderr,
        )
    return []


# ── チェック: root CLAUDE.md ブリッジの欠落検知（Issue #3407） ──────────────

# root CLAUDE.md 欠落時の修復手順（エラーメッセージへ共通で含める）。
_ROOT_CLAUDE_MD_REMEDIATION = "（作成手順: echo '@docs/personal/<your-name>/CLAUDE.md' > CLAUDE.md）"

# `tidd consumer-init` 直後の repo は「git init 直後の空 commit（`init`）」+
# 「初期コミット（`chore: initial commit (tidd consumer-init)`）」の最大 2 commit
# しか持たない。この commit 数以下なら「フレッシュな bootstrap 直後」とみなす（Issue #3482）。
_FRESH_BOOTSTRAP_MAX_COMMIT_COUNT = 2


def _is_freshly_bootstrapped(repo_root: Path) -> bool:
    """commit 履歴が `tidd consumer-init` 直後相当以下の浅さかどうかを返す（Issue #3482）.

    `_check_root_claude_md` の `CI` 環境変数依存 skip は、テストヘルパーが子プロセスへ
    ホワイトリスト env（`CI` を含まない）しか渡さない場合に伝播せず誤検知する構造的な
    欠陥があった（背景: Issue #3482）。`CI` の有無に関係なく、repo 自身の commit 履歴の
    浅さから「フレッシュな bootstrap 直後で root CLAUDE.md が未作成なのが正常」と判定する。
    git が使えない・git 管理下にない場合は「フレッシュではない」として従来どおり検査する
    （安全側フォールバック）。

    `git` 実行ファイルが `PATH` 上に無い環境（Issue #3481）でも `subprocess.run` の
    `FileNotFoundError` を未捕捉のまま伝播させず、`result.returncode != 0` と同様に
    「フレッシュではない」フォールバックとして扱う。
    """
    try:
        result = run_subprocess(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo_root,
            timeout=STANDARD_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    try:
        commit_count = int(result.stdout.strip())
    except ValueError:
        return False
    return commit_count <= _FRESH_BOOTSTRAP_MAX_COMMIT_COUNT


def _resolve_main_worktree_root(repo_root: Path) -> Path:
    """git worktree ならメイン worktree のルートを解決する（Issue #3407）.

    root `CLAUDE.md` は gitignore 済みでメイン worktree にのみ手動作成される運用のため、
    TiDD ワークフロー標準の issue 用 linked worktree（`git worktree add`）をそのまま検査すると
    メイン worktree 側にブリッジがあっても常に「未作成」と誤検知する。共有 `.git` ディレクトリを
    `git rev-parse --git-common-dir` で解決し、linked worktree の場合はメイン worktree 側を
    検査対象にする（git が使えない・repo_root が git 管理下にない場合は repo_root をそのまま返す）。

    `git` 実行ファイルが `PATH` 上に無い環境（Issue #3481）では `subprocess.run` が
    `FileNotFoundError` を送出するため、`result.returncode != 0` と同様に repo_root への
    フォールバックとして扱う（他の呼び出し元が未捕捉のまま crash しないようにする）。
    """
    try:
        result = run_subprocess(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=repo_root,
            timeout=STANDARD_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        return repo_root
    if result.returncode != 0:
        return repo_root

    raw = result.stdout.strip()
    if not raw:
        return repo_root

    common_dir = Path(raw)
    if not common_dir.is_absolute():
        common_dir = (repo_root / common_dir).resolve()
    return common_dir.parent


# `@` import 候補パスを本文から抽出する正規表現（consumer 側 mn-scripts #689 実装を流用）。
# 本文中の任意の位置の `@...` トークンにマッチするため、`foo@example.com` のような
# メールアドレスの `@` 以降も候補として拾ってしまう。実際の import かどうかは
# `_is_likely_import_path()` の「`/` を含むか `.md` で終わるか」で絞り込む（Issue #3966）。
_AT_IMPORT_CANDIDATE_RE = re.compile(r"@([^\s,;:\"'()\[\]{}<>]+)")


def _is_likely_import_path(candidate: str) -> bool:
    """`@` に続くトークンが import パスらしいかどうかを判定する（メールアドレス等を除外・Issue #3966）."""
    return "/" in candidate or candidate.endswith(".md")


def _extract_import_targets(content: str) -> list[str]:
    """本文から `@` import 先パス候補の一覧を抽出する（メールアドレス等を除外・Issue #3966）."""
    return [
        candidate
        for candidate in (m.group(1) for m in _AT_IMPORT_CANDIDATE_RE.finditer(content))
        if _is_likely_import_path(candidate)
    ]


def _find_missing_import_targets(source_md: Path, _seen: set[Path] | None = None) -> list[str]:
    """`source_md` の `@` import 先を再帰的に検証し、存在しないパスの一覧を返す（Issue #3966）.

    import パスは `source_md` 自身のディレクトリからの相対パスとして解決する
    （`docs/personal/<user>/CLAUDE.md` 内の `@../../../.rulesync/rules/overview.md` のような
    「自ファイルからの相対」表記に合わせるため。root CLAUDE.md の場合は自ファイルのディレクトリが
    repo root と一致するため従来どおり repo root 相対と同義になる）。

    import 先が `.md` ファイルとして実在する場合、その中の `@` import もさらに検証対象へ含める
    （`docs/personal/<user>/CLAUDE.md` 自体が別ファイルへブリッジしているケースを追跡するため）。
    循環参照を無限再帰させないよう解決済みファイルを `_seen` で追跡する。
    """
    seen = _seen if _seen is not None else set()
    resolved_source = source_md.resolve()
    if resolved_source in seen:
        return []
    seen.add(resolved_source)

    if not source_md.is_file():
        return []

    content = source_md.read_text(encoding="utf-8")
    missing: list[str] = []
    for target in _extract_import_targets(content):
        resolved_target = (source_md.parent / target).resolve()
        if not resolved_target.is_file():
            missing.append(target)
            continue
        if resolved_target.suffix == ".md":
            missing.extend(_find_missing_import_targets(resolved_target, seen))
    return missing


def _check_root_claude_md(repo_root: Path) -> list[str]:
    """root `CLAUDE.md` が `docs/personal/<user>/CLAUDE.md` へのブリッジを持つか検証する（Issue #3407・#3966）.

    root `CLAUDE.md` は gitignore 済み・ローカル管理（#2355/#2356）で、各マシンが
    `docs/personal/<user>/CLAUDE.md` への `@` import ブリッジを自分で作成する運用になっている。
    ブリッジ欠落を検知する仕組みがないと、未作成マシンでは AGENTS.md（overview 含む）が
    Claude Code セッションに一切読み込まれないまま運用が続いてしまう（背景: Issue #3407）。

    `@` import 行が存在すること自体は #3407 で検証済みだが、その import 先ファイルが実在するかは
    検証していなかった。import パスは手書きのため typo・パス変更・personal ディレクトリの
    リネームで容易に壊れ、壊れても Claude Code はエラーを出さず黙って何も読み込まない
    （#3407 と同一の結果）。本チェックは import 先の実在を再帰的に検証する（Issue #3966）。

    CI では CLAUDE.md が gitignore 済みで原理的に存在しないため、環境変数 `CI` が設定されて
    いる場合はこのチェックを skip する。また `tidd consumer-init` 直後のようなフレッシュな
    bootstrap 直後の repo（root CLAUDE.md 未作成が正常な状態）は、commit 履歴の浅さから
    判定して skip する（`CI` env var の伝播に依存しない・Issue #3482）。

    `git` 実行ファイルが `PATH` 上に丸ごと無い環境（Issue #3481）では、worktree 解決や
    フレッシュ判定など本チェックの前提となる git 情報を何一つ取得できないため、
    「repo_root 自体を検査対象として CLAUDE.md を要求する」という安全側フォールバックが
    成立しない（`git` は使えるがカレントが git 管理下にないだけのケースとは区別する）。
    その場合は本チェック自体を skip する。
    """
    import os
    import shutil

    if os.environ.get("CI"):
        return []

    if shutil.which("git") is None:
        return []

    check_root = _resolve_main_worktree_root(repo_root)

    if _is_freshly_bootstrapped(check_root):
        return []

    claude_md = check_root / "CLAUDE.md"
    if not claude_md.is_file():
        return [f"MISSING_CLAUDE_MD: root CLAUDE.md が存在しません {_ROOT_CLAUDE_MD_REMEDIATION}"]

    content = claude_md.read_text(encoding="utf-8")
    has_import_line = any(line.startswith("@") for line in content.splitlines())
    if not has_import_line:
        return [f"MISSING_CLAUDE_MD: root CLAUDE.md に `@` import 行がありません {_ROOT_CLAUDE_MD_REMEDIATION}"]

    missing_targets = _find_missing_import_targets(claude_md)
    if missing_targets:
        joined = ", ".join(missing_targets)
        return [
            f"MISSING_CLAUDE_MD: root CLAUDE.md の `@` import 先ファイルが存在しません: {joined}"
            f" {_ROOT_CLAUDE_MD_REMEDIATION}"
        ]
    return []


# ── チェック: settings.json 参照 hook ファイルの実在（Issue #3965） ─────────

# `.command` 文字列から `.claude/hooks/<name>.py` を抽出する正規表現。
# `_HOOK_NAME_RE`（`/hooks/<name>.py`）とほぼ同じだが、こちらは `.claude/hooks/` prefix
# まで要求せず `hooks/<name>.py` にも一致する既存パターンを踏襲しつつ、consumer 側
# 実装（mn-scripts #828）に合わせて `.claude/hooks/` 明示 prefix で絞り込む。
_SETTINGS_HOOK_NAME_RE = re.compile(r"\.claude/hooks/([A-Za-z0-9_.-]+\.py)")


def _extract_hook_names_from_settings(settings: dict[str, Any]) -> set[str]:
    """`settings.json` の hooks セクションから登録済み hook ファイル名の集合を抽出する（Issue #3965）.

    グループ形式（`entry["hooks"][i]["command"]`）とフラット形式（`entry["command"]`）の
    両方に対応する。`hooks` セクション自体が dict でない・イベントが list でない・
    entry が dict でない等、想定外の形は無視して skip する（fail-open）。
    """
    names: set[str] = set()
    hooks_section = settings.get("hooks")
    if not isinstance(hooks_section, dict):
        return names

    for entries in hooks_section.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            # フラット形式: entry 自体が command を持つ
            m = _SETTINGS_HOOK_NAME_RE.search(str(entry.get("command", "")))
            if m:
                names.add(m.group(1))

            # グループ形式: entry["hooks"][i]["command"]
            nested = entry.get("hooks")
            if not isinstance(nested, list):
                continue
            for h in nested:
                if not isinstance(h, dict):
                    continue
                m = _SETTINGS_HOOK_NAME_RE.search(str(h.get("command", "")))
                if m:
                    names.add(m.group(1))

    return names


def _check_settings_hook_files_exist(repo_root: Path) -> list[str]:
    """`.claude/settings.json` が参照する hook ファイルの実在を検証する（Issue #3965）.

    `copier update` で上流が hook ファイルを削除しても consumer の `settings.json` の
    登録がその削除に追随しない経路があり、登録されたまま実体ファイルが消えると
    対応する tool を呼ぶたびに
    `python3: can't open file '.../.claude/hooks/<name>.py': [Errno 2] No such file or directory`
    が出続ける（背景: Issue #3965。consumer 側 mn-scripts #828 で実例が発生した）。

    `.claude/settings.json` 不在・JSON として読めない場合は判定不能として fail-open で skip する
    （手編集や copier マージ中の一時的な壊れ方まで異常終了させない）。
    """
    settings_path = repo_root / ".claude" / "settings.json"
    if not settings_path.is_file():
        return []

    import json

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(settings, dict):
        return []

    hooks_dir = repo_root / ".claude" / "hooks"
    failures: list[str] = []
    for name in sorted(_extract_hook_names_from_settings(settings)):
        if not (hooks_dir / name).is_file():
            failures.append(
                f"MISSING_HOOK_FILE: .claude/settings.json が .claude/hooks/{name} を参照していますが"
                " ファイルが存在しません（copier update で削除された可能性があります・#3965）"
            )
    return failures


# ── チェック: .claude/ ↔ .codex/ 構造パリティ（Issue #3338） ────────────────


def _check_codex_parity(repo_root: Path) -> list[str]:
    """`.claude/` を正として `.codex/` 側の構造的な整合性を検証する（Issue #3338）.

    rulesync の `generate --check`（`_check_rulesync_drift`）は「ソースから生成物を
    再生成して差分がないか」しか見ないため、`.rulesync/hooks.jsonc` の override 宣言
    自体が意図的に非対称な場合（例: codexcli.hooks.postToolUse を空配列で上書き）は
    再生成しても差分が出ず検知できない。本チェックは `.claude/` 側の実体と `.codex/`
    側の実体を直接突き合わせ、以下を検証する:

    - hook イベント種別ごとの本数パリティ（override 宣言があれば許容）
    - `.claude/agents/*.md` ↔ `.codex/agents/*.toml` のファイル名パリティ
    - `.codex/agents/*.toml` の `sandbox_mode` 指定（default_permissions 無効化）検出
    """
    failures: list[str] = []
    failures.extend(_check_codex_hook_count_parity(repo_root))
    failures.extend(_check_codex_agent_name_parity(repo_root))
    failures.extend(_check_codex_agent_sandbox_mode(repo_root))
    return failures


def _strip_jsonc(text: str) -> str:
    """JSONC（コメント付き JSON）からコメントを除去して JSON 文字列を返す.

    `.rulesync/hooks.jsonc` は拡張子どおり将来コメントを含みうるため、
    `json.loads` に通す前にコメントを取り除く（tests/test_template_rulesync_drift.py
    等、既存テストで確立済みの前処理パターンを踏襲）。
    """
    out: list[str] = []
    i = 0
    in_string = False
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return re.sub(r",\s*([}\]])", r"\1", "".join(out))


def _count_hook_entries(groups: object, *, exclude: frozenset[str] = frozenset()) -> int:
    """`.claude/settings.json` / `.codex/hooks.json` の 1 イベント分の hook 本数を数える.

    形式は共通で `[{"matcher": ..., "hooks": [...]}, ...]`。

    `exclude` に hook のファイル名（例: `notify-copier-staleness.py`）を渡すと、
    その hook を本数から除外する（Issue #3968: consumer 専用 hook を意図的な
    非対称として扱うため）。
    """
    if not isinstance(groups, list):
        return 0
    total = 0
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            continue
        for hook in group["hooks"]:
            if exclude and isinstance(hook, dict):
                m = _HOOK_NAME_RE.search(str(hook.get("command", "")))
                if m and m.group(1) in exclude:
                    continue
            total += 1
    return total


def _codex_hooks_overrides(repo_root: Path) -> dict[str, object]:
    """`.rulesync/hooks.jsonc` の `codexcli.hooks` override 宣言を返す（なければ空 dict）."""
    import json

    rulesync_path = repo_root / ".rulesync" / "hooks.jsonc"
    if not rulesync_path.is_file():
        return {}
    try:
        data = json.loads(_strip_jsonc(rulesync_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return {}
    codexcli = data.get("codexcli")
    if not isinstance(codexcli, dict):
        return {}
    hooks = codexcli.get("hooks")
    if not isinstance(hooks, dict):
        return {}
    return hooks


def _check_codex_hook_count_parity(repo_root: Path) -> list[str]:
    """`.claude/settings.json` と `.codex/hooks.json` のイベント種別ごとの hook 本数を照合する.

    意図的な差分は次のいずれかで説明できる場合のみ許容する:
    - `.rulesync/hooks.jsonc` の `codexcli.hooks.<event>` override 宣言（例: `postToolUse: []`）
    - `MANAGED_HOOKS_ALLOWED_DIVERGENT`（`notify-copier-staleness.py` 等の consumer 専用 hook。
      `_check_managed_settings_drift` と同じ集合を再利用する。Issue #3968: override 宣言が
      無い consumer 構成でも常時 CODEX_PARITY を誤検知していたため、本数計算から除外する）
    """
    import json

    settings_path = repo_root / ".claude" / "settings.json"
    codex_hooks_path = repo_root / ".codex" / "hooks.json"
    if not settings_path.is_file() or not codex_hooks_path.is_file():
        return []

    try:
        claude_hooks = json.loads(settings_path.read_text(encoding="utf-8")).get("hooks", {})
    except (json.JSONDecodeError, OSError) as exc:
        return [f"CODEX_PARITY: .claude/settings.json の読み込みに失敗しました: {exc}"]
    try:
        codex_hooks = json.loads(codex_hooks_path.read_text(encoding="utf-8")).get("hooks", {})
    except (json.JSONDecodeError, OSError) as exc:
        return [f"CODEX_PARITY: .codex/hooks.json の読み込みに失敗しました: {exc}"]

    if not isinstance(claude_hooks, dict):
        claude_hooks = {}
    if not isinstance(codex_hooks, dict):
        codex_hooks = {}

    overrides = _codex_hooks_overrides(repo_root)

    failures: list[str] = []
    for pascal_name in sorted(set(claude_hooks) | set(codex_hooks)):
        claude_count = _count_hook_entries(claude_hooks.get(pascal_name), exclude=MANAGED_HOOKS_ALLOWED_DIVERGENT)
        codex_count = _count_hook_entries(codex_hooks.get(pascal_name), exclude=MANAGED_HOOKS_ALLOWED_DIVERGENT)
        if claude_count == codex_count:
            continue
        camel_name = pascal_name[0].lower() + pascal_name[1:] if pascal_name else pascal_name
        override = overrides.get(camel_name)
        if isinstance(override, list) and len(override) == codex_count:
            continue
        failures.append(
            f"CODEX_PARITY: .codex/hooks.json の {pascal_name} 本数（{codex_count}）が"
            f" .claude/settings.json（{claude_count}）と異なり、.rulesync/hooks.jsonc の"
            f" codexcli.hooks.{camel_name} override でも説明できません"
            "（意図的な差分なら override を追加、そうでなければ hooks.jsonc の再生成漏れを疑ってください）"
        )
    return failures


def _check_codex_agent_name_parity(repo_root: Path) -> list[str]:
    """`.claude/agents/*.md` と `.codex/agents/*.toml` のファイル名パリティを検証する.

    ファイル名は kebab-case（`.md`）↔ snake_case（`.toml`）で 1:1 対応する
    （例: `issue-implementer.md` ↔ `issue_implementer.toml`）。
    """
    claude_dir = repo_root / ".claude" / "agents"
    codex_dir = repo_root / ".codex" / "agents"
    if not claude_dir.is_dir() or not codex_dir.is_dir():
        return []

    claude_names = {p.stem for p in claude_dir.glob("*.md")}
    codex_names = {p.stem for p in codex_dir.glob("*.toml")}

    failures: list[str] = []
    for name in sorted(claude_names):
        expected = name.replace("-", "_")
        if expected not in codex_names:
            failures.append(
                f"CODEX_PARITY: .codex/agents/{expected}.toml が見つかりません"
                f"（.claude/agents/{name}.md に対応する Codex agent 定義が欠けています）"
            )
    for name in sorted(codex_names):
        expected = name.replace("_", "-")
        if expected not in claude_names:
            failures.append(
                f"CODEX_PARITY: .codex/agents/{name}.toml に対応する .claude/agents/{expected}.md が見つかりません"
            )
    return failures


def _check_codex_agent_sandbox_mode(repo_root: Path) -> list[str]:
    """`.codex/agents/*.toml` の `sandbox_mode` 指定を検出する（Issue #3327 の再発防止）.

    Codex は `sandbox_mode` と `default_permissions`（permission profile）を併用できず、
    `sandbox_mode` を指定すると `default_permissions` のシークレット deny（.env 等の
    Read/Write 拒否）が無効化される（Codex 公式仕様）。#3327 で readonly-safe permission
    profile への置き換えが完了済みだが、再発を機械検知する。
    """
    import tomllib

    codex_dir = repo_root / ".codex" / "agents"
    if not codex_dir.is_dir():
        return []

    failures: list[str] = []
    for toml_file in sorted(codex_dir.glob("*.toml")):
        try:
            data = tomllib.loads(toml_file.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            failures.append(f"CODEX_PARITY: .codex/agents/{toml_file.name} の読み込みに失敗しました: {exc}")
            continue
        if "sandbox_mode" in data:
            failures.append(
                f"CODEX_PARITY: .codex/agents/{toml_file.name} に sandbox_mode が指定されています。"
                " sandbox_mode は default_permissions（シークレット deny）を無効化します。"
                " permission profile（例: readonly-safe）に置き換えてください（#3327）。"
            )
    return failures


# ── チェック: 廃止済み env var 残存検知（Issue #2531） ──────────────────────


def _get_config_value_for_key(key: str) -> bool | None:
    """config.json から指定キーの値を読み取る（なければ None）.

    ファイルなし・キーなし → None（default ではなく「未設定」を区別する）
    """
    import json
    import os

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or ""
        if appdata:
            config_path = Path(appdata) / "tidd_tools" / "config.json"
        else:
            home = os.environ.get("HOME") or str(Path.home())
            config_path = Path(home) / ".config" / "tidd_tools" / "config.json"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or ""
        if xdg:
            config_path = Path(xdg) / "tidd_tools" / "config.json"
        else:
            home = os.environ.get("HOME") or str(Path.home())
            config_path = Path(home) / ".config" / "tidd_tools" / "config.json"

    if not config_path.is_file():
        return None

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(config, dict) or key not in config:
        return None

    value = config[key]
    if isinstance(value, bool):
        return value
    return None


def _check_deprecated_env_vars() -> list[str]:
    """廃止済み env var が環境に残っていれば警告を返す（Issue #2531）.

    移行ガイド（ai-review-backend-migration.md）が手順を書いているが、
    機械強制がなく削除されなければ利用者は「移行済み」と誤認したままになる。
    失敗（failures）ではなく警告（warnings）として扱う（exit code には影響しない）。

    出力に含まれる情報:
    - 廃止された env var 名
    - 廃止 Issue 番号（#2495 等）
    - 移行先 config.json キー
    - 現在の実効値（config.json の値、またはキーなし時は default ON）
    """
    import os

    warnings: list[str] = []

    for env_var_name, entry in DEPRECATED_ENV_VARS.items():
        if env_var_name not in os.environ:
            continue

        # 移行先の実効値を取得（キーなし = default ON = True）
        config_value = _get_config_value_for_key(entry.replacement_key)
        effective_value = config_value if config_value is not None else True
        effective_label = "有効（ON）" if effective_value else "無効（OFF）"

        warnings.append(
            f"WARN: 廃止済み env var {env_var_name} が環境に残っています。"
            f" この設定は {entry.deprecated_issue} で廃止され、現在は完全に無視されています。"
            f" 移行先: {entry.replacement_key}（config.json）。"
            f" 現在の実効値: {effective_label}。"
            f" 移行手順: {entry.migration_guide_url}"
        )

    return warnings


# ── チェック: グローバル tidd tool の non-editable install 検知（Issue #2518） ──


def _global_tool_env_root() -> Path:
    """グローバル tidd tool の dist-info が置かれるディレクトリ.

    モジュールトップレベルで `Path.home()` を評価すると import 時点の値が固定され、
    テストが `monkeypatch.setenv("HOME", ...)` で隔離してもモジュール再 import まで
    反映されない（Windows でのテスト隔離漏れの原因の一つ・#3894）。呼び出し時に
    評価する関数へ変更し、遅延評価にする。
    """
    return Path.home() / ".local" / "share" / "uv" / "tools" / "tidd-tools"


_GLOBAL_DIST_INFO_GLOB = "tidd_tools-*.dist-info"


def _check_agents_skills_symlink(repo_root: Path) -> list[str]:
    """`.agents/skills` → `.claude/skills` symlink の生存確認（Issue #3235）.

    Codex interop の生成物（コミット対象外）が欠落・不正になっていないかを
    テンプレート本尊リポジトリで検証する。検証が実効するのは `.agents/skills`
    が存在するときのみ。`.agents/` 自体が存在しない（セットアップ未実施・
    fresh checkout / CI）場合は「未初期化」として WARN を出力して skip する
    （黙って skip せず、実行条件を明示する・Issue #3336）。
    `.agents/skills` が symlink でない実体ディレクトリの場合（Issue #3567）は
    Codex から配布 skill が見えないまま無言で無効化されるため、移行手順付きの
    警告を返す（`ensure_check` は実体ディレクトリを generic な不正として扱うため
    実体ケースを先に分岐する）。
    """
    from tidd_tools.ensure_agents_skills import ensure_check

    if not (repo_root / ".agents").is_dir():
        print(
            "WARN: .agents/ が存在しないため .agents/skills symlink 検証を skip します"
            "（未初期化・`uv run --project projects/py/tidd_tools python -m tidd_tools"
            " ensure-agents-skills` で生成してください）",
            file=sys.stderr,
        )
        return []
    target = repo_root / ".agents" / "skills"
    if not target.is_symlink() and target.is_dir():
        return [
            "AGENTS_SKILLS_NOT_SYMLINK: .agents/skills が実体ディレクトリです"
            "（symlink ではないため Codex から配布 skill が見えません）。"
            "既存 skill を `.claude/skills/` へ移動してから"
            " `tidd ensure-agents-skills` を実行してください"
        ]
    if ensure_check(repo_root) != 0:
        return [
            "DRIFT: .agents/skills symlink が存在しないか不正です"
            "（`uv run --project projects/py/tidd_tools python -m tidd_tools"
            " ensure-agents-skills` を実行して生成してください）"
        ]
    return []


def _which_rulesync() -> str | None:
    """PATH 上の rulesync を解決する（Windows の拡張子を考慮・Issue #3900）.

    npm は Windows でも拡張子なしの POSIX shim（`rulesync`）を生成するが、
    Windows の `CreateProcess` は `PATHEXT` に載る拡張子でしか実行できないため、
    Windows では `.cmd` を先に試してから拡張子なしにフォールバックする。
    """
    if sys.platform == "win32":
        found = shutil.which("rulesync.cmd")
        if found:
            return found
    return shutil.which("rulesync")


def _resolve_rulesync_bin(repo_root: Path) -> str | None:
    """rulesync 実行コマンドを解決する（Issue #3411・Windows 対応 #3900）.

    rulesync は package.json に `--save-exact` で固定した devDependency のため、
    `npm ci` 後にローカル install される `node_modules/.bin/rulesync` を優先する。
    npm は Windows でも拡張子なしの POSIX shim（`rulesync`）を同時生成するが、
    Windows の `CreateProcess` は `PATHEXT` に載る拡張子でしか実行できず、
    POSIX shim をそのまま起動すると `WinError 193` で失敗するため、Windows では
    `node_modules/.bin/rulesync.cmd` を優先して選ぶ（無ければ拡張子なし shim に
    フォールバック）。見つからない場合のみ PATH 上のグローバル install に
    フォールバックする（移行中の環境・consumer リポジトリ等）。
    """
    bin_dir = repo_root / "node_modules" / ".bin"
    candidates = ("rulesync.cmd", "rulesync") if sys.platform == "win32" else ("rulesync",)
    for name in candidates:
        local_bin = bin_dir / name
        if local_bin.is_file():
            return str(local_bin)
    return _which_rulesync()


def _check_rulesync_drift(repo_root: Path, rulesync_bin: str | None = None) -> list[str]:
    """rulesync 正本とコミット済み生成物のドリフトを検出する（Issue #3209）.

    `rulesync generate --check` は `.rulesync/hooks.jsonc`・`.rulesync/rules/*.md`
    から生成される `.claude/settings.json`・`.codex/hooks.json`・
    `.claude/rules/*.md`・`AGENTS.md` がコミット済みファイルと乖離している場合に
    exit 1 で失敗し、ドリフトしたファイルを出力する（rulesync CLI 公式の
    `--check` モード・dyoshikawa/rulesync）。

    rulesync CLI が解決できない環境（fresh checkout・CI 未導入など）では skip する
    （CI ジョブ `rulesync-drift-check` 側で `npm ci` 後に実行する）。ただし黙って
    skip せず、未導入である旨の WARN を出力する（Issue #3337）。呼び出し元が
    `rulesync_bin` を指定しない場合は PATH 上の `rulesync` のみを見る（後方互換）。
    ローカル install（`node_modules/.bin/rulesync`）を優先した解決は
    `_resolve_rulesync_bin`（Issue #3411）を呼び出し側で使うこと。PATH 解決も
    `_which_rulesync`（Issue #3900）経由で Windows の `.cmd` 拡張子を考慮する。
    """
    if rulesync_bin is None:
        rulesync_bin = _which_rulesync()
    if not rulesync_bin:
        print(
            "WARN: rulesync が解決できないため rulesync 生成物のドリフト検証を"
            " skip します（`npm ci` でローカル install するか"
            "`npm install -g rulesync@16.7.0` でグローバル install してください"
            "・Issue #3337/#3411）",
            file=sys.stderr,
        )
        return []
    try:
        proc = subprocess.run(
            [rulesync_bin, "generate", "--check"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"DRIFT: rulesync generate --check の実行に失敗しました: {exc}"]
    if proc.returncode == 0:
        return []
    return [
        "DRIFT: rulesync 生成物とコミット済みファイルが不一致です"
        "（`rulesync generate` を実行して再生成してください）\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    ]


def _check_global_tidd_editable_install(repo_root: Path) -> list[str]:
    """グローバル tidd tool が non-editable install になっていないかを検知する（Issue #2518）.

    **検知条件:** maintainer リポジトリ（`templates/workflow/` を持つ本体）でのみ実行する。
    consumer 環境（template_dir なし）では silent skip する。

    **判定ロジック:**
    1. `~/.local/share/uv/tools/tidd-tools/lib/**/site-packages/tidd_tools-*.dist-info/`
       を探す
    2. dist-info ディレクトリが存在しない（未インストール）→ silent skip（空リスト）
    3. `direct_url.json` の `dir_info.editable` が `true` でない場合 → failure
       - `dir_info` キーなし（git+url インストール等）も failure 扱い
    4. `dir_info.editable` が `true` → 合格（空リスト）
    """
    import json

    # maintainer リポジトリ（template_dir あり）でのみ実行する
    template_dir = repo_root / "templates" / "workflow"
    if not template_dir.is_dir():
        return []

    site_packages_root = _global_tool_env_root() / "lib"
    if not site_packages_root.is_dir():
        # グローバル tidd tool が未インストール → silent skip
        return []

    # lib/pythonX.XX/site-packages/tidd_tools-*.dist-info/ を探す
    dist_info: Path | None = None
    for python_dir in site_packages_root.iterdir():
        sp = python_dir / "site-packages"
        if not sp.is_dir():
            continue
        candidates = list(sp.glob(_GLOBAL_DIST_INFO_GLOB))
        if candidates:
            dist_info = candidates[0]
            break

    if dist_info is None:
        # dist-info が見つからない → 未インストールとみなして silent skip
        return []

    # direct_url.json を読んで editable 判定する
    direct_url_path = dist_info / "direct_url.json"
    if not direct_url_path.is_file():
        # direct_url.json がない → 非 editable とみなして failure
        return [
            "NON_EDITABLE_TIDD: グローバル tidd tool が non-editable install です。"
            " `uv tool install --editable projects/py/tidd_tools` を実行して修正してください。"
        ]

    try:
        data = json.loads(direct_url_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 読み取れない場合は安全側フォールバック（失敗とみなさない）
        return []

    dir_info = data.get("dir_info", {})
    if dir_info.get("editable", False):
        # editable install → 合格
        return []

    return [
        "NON_EDITABLE_TIDD: グローバル tidd tool が non-editable install です"
        f"（{direct_url_path}）。"
        " `uv tool install --editable projects/py/tidd_tools` を実行して修正してください。"
    ]


# ── チェック: 計測境界マークの記録断絶検知（Issue #3385） ───────────────────


def _parse_timing_timestamp(value: object) -> datetime | None:
    """統一日誌の ISO8601 タイムスタンプ（``%Y-%m-%dT%H:%M:%SZ``）を parse する.

    解析できない値（欠落・不正フォーマット）は None を返す（判定不能として無視する）。
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _check_timing_boundary_freshness() -> list[str]:
    """統一日誌の計測境界マークが直近 N 日以内に記録されているか検証する（Issue #3385）.

    `record-timing-boundaries.py` 等が書き込む `_TIMING_BOUNDARY_STEPS`
    （`step2-branch-created`・`step4-pr-created`・`step5-airview-end`）のうち、
    **過去に一度でも記録されたことがある step** が直近 `_TIMING_BOUNDARY_WINDOW_DAYS`
    日以内には 1 件も記録されていなければ、記録経路が静かに断絶している疑いがあるため
    failure を返す（実例: #3385 背景。3 マークが数週間記録されなくなっていたのに
    「計測不可」表示と区別がつかず見過ごされていた）。

    過去に一度も記録されたことがない step（フレッシュインストール直後・
    テストの隔離環境で統一日誌 DB 自体が存在しない等）は「未使用」と「断絶」を
    区別できないため対象外とする（fail-open）。これにより `HOME` を隔離した
    テストダブル環境（例: `test_issue_2875.py`・`test_issue_1994.py`）で誤検知しない。

    統一日誌 DB の読み込みに失敗した場合も判定不能として fail-open（空リスト）で返す。
    """
    try:
        events = timing_log.read_all_events()
    except (OSError, sqlite3.Error):
        return []

    cutoff = datetime.now(UTC) - timedelta(days=_TIMING_BOUNDARY_WINDOW_DAYS)
    ever_recorded_steps: set[str] = set()
    recent_steps: set[str] = set()
    for event in events:
        step = event.get("step")
        if step not in _TIMING_BOUNDARY_STEPS:
            continue
        ever_recorded_steps.add(step)
        timestamp = _parse_timing_timestamp(event.get("timestamp"))
        if timestamp is not None and timestamp >= cutoff:
            recent_steps.add(step)

    failures: list[str] = []
    for step in _TIMING_BOUNDARY_STEPS:
        if step not in ever_recorded_steps:
            continue  # 記録実績なし（未使用環境）は断絶と区別できないため対象外
        if step in recent_steps:
            continue
        failures.append(
            f"TIMING_GAP: 直近 {_TIMING_BOUNDARY_WINDOW_DAYS} 日以内に統一日誌へ {step} が"
            " 1 件も記録されていません。record-timing-boundaries.py 等の記録経路が断絶している"
            " 可能性があります（詳細: docs/reference/hooks.md#record-timing-boundariespy・#3385）。"
        )
    return failures


# ── チェック: PATH 上の tidd シャドーイング検知（Issue #2875） ──────────────


def _check_tidd_path_shadowing() -> list[str]:
    """PATH 上で正規のグローバル tidd install をシャドーイングする野良 tidd 実行ファイルを検知する.

    mise 管理 python 環境など、リポジトリ外の python 環境へ誤って `pip install -e .`
    してしまうと、PATH の並び順次第で正規のグローバル tidd
    （`~/.local/share/uv/tools/tidd-tools/` 経由でインストールされ `~/.local/bin/tidd` から
    シンボリックリンクされる）より先に壊れた tidd 実行ファイルが解決されてしまう
    （実例: Issue #2875 背景）。

    **判定ロジック:**
    1. `PATH` 上の全ディレクトリを走査し、`tidd`（Windows は `tidd.exe`）という名前の
       実行可能ファイルを重複を除いて列挙する（`shutil.which` の全件探索相当）。
       この段階では実行中の python 自身のディレクトリを除外しない
       （正規の `tidd` console_script も shebang が同じ bin ディレクトリの python を
       指すため、ここで除外すると通常実行時に正規インストール自身が探索から漏れ、
       次段の正規インストール検出が必ず失敗してしまうため。Issue #2875 レビューで
       判明した回帰）。
    2. 各候補の実体（symlink 解決後の実パス）が正規インストール
       （`~/.local/share/uv/tools/tidd-tools/` 配下）かどうかを判定する。
    3. 正規インストールが PATH 上に見つからない場合は判定不能のため silent skip する
       （consumer 環境やグローバル tidd 未インストール環境での誤検知を避けるため）。
    4. 正規インストールより PATH 上で手前にある各候補について、実行中の python 自身の
       bin ディレクトリ（例: `uv run --project` 時のプロジェクトローカル `.venv/bin`）に
       あり、かつ実行中の python が **venv 内**（`sys.prefix != sys.base_prefix`）で
       **canonical_root 配下ではない** ものは failure から除外する。そのディレクトリの
       `tidd` shim が正規 install より手前に来るのは意図した挙動であり、誤検知になるため。
       venv 判定を加えるのは、mise 管理 python のような**非 venv** 環境（Issue #2875
       実インシデント: mise base python への直接 `pip install -e .`、venv レイヤーなし）
       から野良 tidd 自身を実行した場合に、`candidate.parent == current_interpreter_dir`
       のみでは野良 tidd 自体まで除外されてしまい検知漏れになるため（PR #2893 レビュー
       指摘）。`sys.prefix != sys.base_prefix` は pip・venv モジュール自身が使う標準の
       venv 判定方法。
    5. 上記以外で正規インストールより PATH 上で手前に別の tidd 実行ファイルがあれば failure。
    """
    import os

    exe_name = "tidd.exe" if sys.platform == "win32" else "tidd"
    canonical_root = Path.home() / ".local" / "share" / "uv" / "tools" / "tidd-tools"

    path_env = os.environ.get("PATH", "")
    if not path_env:
        return []

    # 実行中の python 自身の bin ディレクトリ（例: .venv/bin）を識別する。
    # venv の python 実行ファイル自体はベースインタプリタへの symlink であることが多いため
    # `.resolve()` はしない（symlink 先まで辿ると venv の実ディレクトリと一致しなくなる）。
    current_interpreter_dir = Path(sys.executable).absolute().parent
    # `sys.prefix != sys.base_prefix` は venv 内実行かどうかの標準判定方法（pip・venv
    # モジュール自身が使う）。mise 管理 python のような非 venv 環境（Issue #2875 実
    # インシデント）はこの条件を満たさないため、後段の自己ディレクトリ除外が誤って
    # 適用されない。
    running_in_venv = sys.prefix != sys.base_prefix
    interpreter_under_canonical = current_interpreter_dir.is_relative_to(canonical_root)

    found: list[Path] = []
    seen_dirs: set[str] = set()
    for entry in path_env.split(os.pathsep):
        if not entry or entry in seen_dirs:
            continue
        seen_dirs.add(entry)
        entry_dir = Path(entry).absolute()
        candidate = entry_dir / exe_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            found.append(candidate)

    canonical_index: int | None = None
    for idx, candidate in enumerate(found):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_relative_to(canonical_root):
            canonical_index = idx
            break

    if canonical_index is None:
        # 正規インストールが PATH 上に見つからない → 判定不能のため silent skip
        return []

    failures: list[str] = []
    for candidate in found[:canonical_index]:
        if candidate.parent == current_interpreter_dir and running_in_venv and not interpreter_under_canonical:
            # 実行中の python 自身の venv bin（例: .venv/bin）は対象外。
            # venv 内実行かつ canonical_root 配下でない場合のみ除外する（非 venv 環境
            # から野良 tidd 自身を実行するケースは除外しない。Issue #2875）。
            continue
        detail = _describe_stray_tidd(candidate)
        message = (
            f"TIDD_PATH_SHADOWED: 正規のグローバル tidd（{canonical_root}）より PATH 上で"
            f"手前に野良 tidd 実行ファイルが存在します（{candidate}）。"
        )
        if detail:
            message += f" {detail}"
        message += " PATH の並び順を修正するか該当ファイルを削除してください。"
        failures.append(message)
    return failures


def _describe_stray_tidd(candidate: Path) -> str:
    """野良 tidd 実行ファイルの追加情報（editable install の参照先等）を返す（ベストエフォート）.

    候補ファイルの shebang からインタプリタを特定し、その site-packages にある
    `tidd_tools-*.dist-info/direct_url.json` を読んで editable install の参照先を判定する。
    判定できない場合は空文字を返す（調査補助のための best-effort であり、
    判定不能を failure 扱いにはしない）。
    """
    import json

    try:
        first_line = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (OSError, IndexError, UnicodeDecodeError):
        return ""
    if not first_line.startswith("#!"):
        return ""
    interpreter = Path(first_line[2:].strip())
    if not interpreter.is_file():
        return ""

    # venv 構造: <env_root>/bin/python → <env_root>/lib/python*/site-packages/
    env_root = interpreter.parent.parent
    for site_packages in sorted(env_root.glob("lib/python*/site-packages")):
        for dist_info in site_packages.glob("tidd_tools-*.dist-info"):
            direct_url_path = dist_info / "direct_url.json"
            if not direct_url_path.is_file():
                continue
            try:
                data = json.loads(direct_url_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            dir_info = data.get("dir_info", {})
            url = data.get("url", "")
            if not dir_info.get("editable", False) or not url.startswith("file://"):
                continue
            referenced_path = url[len("file://") :]
            if not Path(referenced_path).exists():
                return f"壊れた editable install です（参照先が存在しません: {referenced_path}）。"
            return f"editable install の参照先: {referenced_path}"
    return ""


# ── 内部ユーティリティ ───────────────────────────────────────────────────────


def _resolve_repo_root(explicit: Path | None) -> Path:
    """リポジトリルートを解決する。explicit 指定がなければ git コマンドで取得する。"""
    if explicit is not None:
        return explicit.resolve()
    result = run_subprocess(
        ["git", "rev-parse", "--show-toplevel"],
        timeout=STANDARD_TIMEOUT_SEC,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    # git が使えない場合は CWD にフォールバック
    return Path.cwd()
