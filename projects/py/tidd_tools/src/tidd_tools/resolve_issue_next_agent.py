"""起動元別に issue-next の実装委譲設定を解決する（Issue #4181）."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_DEFAULTS: dict[str, dict[str, str]] = {
    "claude": {"agent_type": "issue-implementer", "model": "sonnet"},
    "codex": {"agent_type": "issue_implementer", "model": "gpt-5.6-luna"},
}
_LAUNCHER_KEYS = {
    "claude": ("issue-next-claude-agent", "issue-next-claude-model"),
    "codex": ("issue-next-codex-agent", "issue-next-codex-model"),
}


def _detect_launcher() -> str | None:
    """実行環境を明示変数から判定する。曖昧な環境は未対応として扱う."""
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_HOME"):
        return "codex"
    if os.environ.get("CLAUDE_CODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    return None


def _load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve(launcher: str | None, config: dict[str, Any]) -> dict[str, str] | None:
    """実装設定とレビュー設定を独立して返す。"""
    selected = None if launcher in (None, "auto") else launcher
    selected = selected or _detect_launcher()
    if selected not in _DEFAULTS:
        return None
    agent_key, model_key = _LAUNCHER_KEYS[selected]
    result = {
        **_DEFAULTS[selected],
        "impl-backend": str(config.get("impl-backend", "")),
        "review-backend": str(config.get("issue-next-review-backend", "auto")),
        "priority": str(config.get("issue-next-review-priority", "agy")),
    }
    if isinstance(config.get(agent_key), str) and config[agent_key].strip():
        result["agent_type"] = config[agent_key].strip()
    if isinstance(config.get(model_key), str) and config[model_key].strip():
        result["model"] = config[model_key].strip()
    return result


def run_cli(args: argparse.Namespace) -> int:
    result = resolve(getattr(args, "launcher", None), _load_config(getattr(args, "config_path", None)))
    if result is None:
        print("unsupported launcher: expected Claude Code or Codex", file=sys.stderr)
        return 2
    print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "resolve-issue-next-agent",
        help="起動元に応じた issue-next 実装委譲先・モデル・レビュー設定を表示する",
    )
    parser.add_argument("--launcher", choices=("auto", "claude", "codex"), default="auto")
    parser.add_argument("--config-path", help="設定 JSON のパス（省略時は既定値のみ）")
    parser.set_defaults(func=run_cli)
