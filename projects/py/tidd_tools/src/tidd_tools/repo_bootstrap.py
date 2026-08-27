"""`tidd repo-bootstrap` サブコマンド（Issue #2219・#2253・#2275・#2353）.

consumer リポジトリ（copier copy 直後）に対して、public repo 向けセキュリティ設定
（secret scanning push protection・vulnerability alerts・Dependabot security
updates・CodeQL default setup・private vulnerability reporting・secret scanning
non-provider patterns / validity checks）を冪等に適用する。

ラベル一式の作成・squash-only 等のリポジトリ設定・branch 保護 ruleset
（`protect-main`）は probot/settings App（hosted: apps/settings）に責務を
移管したため、本サブコマンドは security features 専用に絞っている
（Issue #2353・#2368 見直しで safe-settings から probot/settings 前提へ変更・
判断ジャーナル: docs/decisions/2026-07-23-settings-app-scope-over-safe-settings.md）。

CodeQL default setup は `state: configured` を毎回 PATCH するだけで冪等（既に
configured でも同じ結果になる）。private vulnerability reporting も同様に
`PUT` を毎回実行するだけで冪等（GitHub 側が既に有効なら no-op）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.subprocess_runner import run as run_subprocess


def _gh_api_env(repo: str | None) -> dict[str, str] | None:
    """`gh api` 呼び出し用の環境変数を構築する.

    `gh api` は `--repo` フラグを受け付けない（`gh label` 系のみ対応）ため、
    `GH_REPO` 環境変数で対象リポジトリを指定する（Issue #2334）。
    """
    if not repo:
        return None
    env = dict(os.environ)
    env["GH_REPO"] = repo
    return env


def _gh_api_json(endpoint: str, method: str, payload: dict[str, Any], repo: str | None) -> None:
    """JSON body を伴う `gh api` 呼び出しを実行する（`--input` 経由で一時ファイルを渡す）."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(payload, f)
        tmp_path = f.name
    try:
        args = ["api", endpoint, "-X", method, "--input", tmp_path]
        result = run_subprocess(["gh", *args], env=_gh_api_env(repo))
        if result.returncode != 0:
            raise GhCommandError(args, result.returncode, result.stderr or "")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _repo_is_public(repo: str | None) -> bool:
    """対象リポジトリが public かどうかを返す."""
    args = ["api", "repos/{owner}/{repo}"]
    result = run_subprocess(["gh", *args], env=_gh_api_env(repo))
    if result.returncode != 0:
        raise GhCommandError(args, result.returncode, result.stderr or "")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    return not bool(data.get("private", True))


_SECURITY_SETTINGS_LABELS: dict[str, str] = {
    "secret_scanning": "secret scanning",
    "secret_scanning_push_protection": "secret scanning push protection",
    "dependabot_security_updates": "Dependabot security updates",
    "secret_scanning_non_provider_patterns": "secret scanning non-provider patterns",
    "secret_scanning_validity_checks": "secret scanning validity checks",
}


def _unreflected_security_settings(repo: str | None) -> list[str]:
    """PATCH 後に GET `repos/{owner}/{repo}` の `security_and_analysis` を確認し、

    `status` が `"enabled"` にならなかったキー一覧を返す（Issue #2337）。
    personal free plan の public repo では PATCH が HTTP 200 を返しつつ実際には
    反映されない項目がある（例: secret_scanning_validity_checks は GitHub
    Team/Enterprise の Secret Protection 限定機能）ため、この GET 確認が必要。
    """
    args = ["api", "repos/{owner}/{repo}"]
    result = run_subprocess(["gh", *args], env=_gh_api_env(repo))
    if result.returncode != 0:
        raise GhCommandError(args, result.returncode, result.stderr or "")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    security = data.get("security_and_analysis")
    if not isinstance(security, dict):
        security = {}
    unreflected: list[str] = []
    for key in _SECURITY_SETTINGS_LABELS:
        entry = security.get(key)
        status = entry.get("status") if isinstance(entry, dict) else None
        if status != "enabled":
            unreflected.append(key)
    return unreflected


def _apply_security_settings(repo: str | None, *, dry_run: bool) -> list[str]:
    """public repo 向けセキュリティ設定を適用し、未反映のキー一覧を返す.

    secret scanning push protection・vulnerability alerts・Dependabot security
    updates・secret scanning non-provider patterns・secret scanning validity
    checks を有効化する（Issue #2253・#2275）。PATCH 後に GET で反映を確認し、
    plan 制約等で反映されなかったキーを返す（Issue #2337）。
    """
    if dry_run:
        return []
    payload = {"security_and_analysis": {key: {"status": "enabled"} for key in _SECURITY_SETTINGS_LABELS}}
    _gh_api_json("repos/{owner}/{repo}", "PATCH", payload, repo)

    args = ["api", "repos/{owner}/{repo}/vulnerability-alerts", "-X", "PUT"]
    result = run_subprocess(["gh", *args], env=_gh_api_env(repo))
    if result.returncode != 0:
        raise GhCommandError(args, result.returncode, result.stderr or "")

    return _unreflected_security_settings(repo)


def _apply_codeql_default_setup(repo: str | None, *, dry_run: bool) -> None:
    """CodeQL default setup（code scanning）を冪等に有効化する（Issue #2275）.

    `state: configured` を毎回 PATCH するだけで冪等（既に configured でも
    同じ結果になる）。言語は指定せず GitHub 側の自動検出に委ねる。
    """
    if dry_run:
        return
    payload: dict[str, Any] = {"state": "configured"}
    _gh_api_json("repos/{owner}/{repo}/code-scanning/default-setup", "PATCH", payload, repo)


def _apply_private_vulnerability_reporting(repo: str | None, *, dry_run: bool) -> None:
    """private vulnerability reporting を冪等に有効化する（Issue #2275）.

    `PUT` を毎回実行するだけで冪等（GitHub 側が既に有効なら no-op で 204 を返す）。
    """
    if dry_run:
        return
    args = ["api", "repos/{owner}/{repo}/private-vulnerability-reporting", "-X", "PUT"]
    result = run_subprocess(["gh", *args], env=_gh_api_env(repo))
    if result.returncode != 0:
        raise GhCommandError(args, result.returncode, result.stderr or "")


def run_cli(args: argparse.Namespace) -> int:
    repo: str | None = getattr(args, "repo", None)
    dry_run: bool = getattr(args, "dry_run", False)

    is_public = False
    if not dry_run:
        try:
            is_public = _repo_is_public(repo)
        except GhCommandError as exc:
            sys.stderr.write(
                f"エラー: リポジトリの公開設定確認に失敗しました（gh 認証・admin 権限を確認してください）: {exc}\n"
            )
            return 1

    unreflected_security: list[str] = []
    if is_public:
        try:
            unreflected_security = _apply_security_settings(repo, dry_run=dry_run)
        except GhCommandError as exc:
            sys.stderr.write(
                f"エラー: セキュリティ設定の適用に失敗しました（gh 認証・admin 権限を確認してください）: {exc}\n"
            )
            return 1

        try:
            _apply_codeql_default_setup(repo, dry_run=dry_run)
        except GhCommandError as exc:
            sys.stderr.write(
                f"エラー: CodeQL default setup の適用に失敗しました（gh 認証・admin 権限を確認してください）: {exc}\n"
            )
            return 1

        try:
            _apply_private_vulnerability_reporting(repo, dry_run=dry_run)
        except GhCommandError as exc:
            sys.stderr.write(
                f"エラー: private vulnerability reporting の適用に失敗しました"
                f"（gh 認証・admin 権限を確認してください）: {exc}\n"
            )
            return 1

    if dry_run:
        print(
            "[dry-run] public repo の場合、secret scanning push protection・"
            "vulnerability alerts・Dependabot security updates・secret scanning "
            "non-provider patterns / validity checks・CodeQL default setup・"
            "private vulnerability reporting を有効化します。"
        )
    else:
        if is_public:
            enabled_labels = [
                label for key, label in _SECURITY_SETTINGS_LABELS.items() if key not in unreflected_security
            ]
            if enabled_labels:
                print(f"セキュリティ設定（vulnerability alerts・{'・'.join(enabled_labels)}）を有効化しました。")
            else:
                print("セキュリティ設定（vulnerability alerts）を有効化しました。")
            for key in unreflected_security:
                print(f"WARN: {key} は反映されませんでした（リポジトリの plan 制約により対象外の可能性があります）。")
            print("CodeQL default setup を有効化しました。")
            print("private vulnerability reporting を有効化しました。")
        else:
            print(
                "private リポジトリのため public 専用設定（secret scanning 等・"
                "CodeQL default setup・private vulnerability reporting）はスキップしました。"
            )
    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "repo-bootstrap",
        help="consumer リポジトリに TiDD セキュリティ設定（secret scanning 等）を冪等に適用する",
        description=__doc__,
    )
    add_common_flags(parser)
    parser.add_argument(
        "--repo",
        default=None,
        metavar="OWNER/REPO",
        help="対象リポジトリ（省略時は現在のディレクトリから gh が解決する）",
    )
    parser.set_defaults(func=run_cli)


if __name__ == "__main__":
    sys.exit(
        run_cli(
            argparse.Namespace(
                repo=None,
                dry_run=False,
                verbose=0,
                json_output=False,
            ),
        )
    )
