"""起動元と role から非実装 subagent の routing を解決する (Issue #4192)."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from tidd_tools.subagent_role_registry import get_role_config

SUPPORTED_ROLES = (
    "issue-writer",
    "duplicate-detector",
    "ai-confirm-verifier",
    "post-merge-verifier",
    "yaru-verifier",
    "issue-next",
)
SUPPORTED_LAUNCHERS = ("claude_code", "codex")
EXCLUDED_ROLES = ("issue-reviewer", "issue-implementer", "issue-fixer")


def error_message(launcher: str, role: str) -> str:
    """入力不備を CLI 向けの stderr メッセージへ変換する."""
    if launcher not in SUPPORTED_LAUNCHERS:
        return f"未登録の起動元: {launcher}"
    if role in EXCLUDED_ROLES:
        return f"対象外 role: {role}（実装担当 role は本 routing の対象外です）"
    return f"未登録の role または組み合わせ: role={role}, launcher={launcher}"


def resolve(launcher: str, role: str) -> dict[str, Any] | None:
    """registry の role・起動元設定を返す。解決不能なら None を返す."""
    if launcher not in SUPPORTED_LAUNCHERS or role not in SUPPORTED_ROLES:
        return None
    return get_role_config(role, launcher)


def run_cli_args(launcher: str, role: str) -> int:
    """CLI 契約を直接呼び出すための薄いハンドラ."""
    config = resolve(launcher, role)
    if config is None:
        print(error_message(launcher, role), file=sys.stderr)
        return 2
    print(json.dumps({"launcher": launcher, "role": role, **config}, ensure_ascii=False))
    return 0


def run_cli(args: argparse.Namespace) -> int:
    return run_cli_args(args.launcher, args.role)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "resolve-subagent-routing",
        help="起動元と role に応じた非実装 subagent の routing を解決する",
    )
    parser.add_argument("--launcher", required=True, help="起動元（claude_code または codex）")
    parser.add_argument("--role", required=True, help="解決対象の role")
    parser.set_defaults(func=run_cli)
