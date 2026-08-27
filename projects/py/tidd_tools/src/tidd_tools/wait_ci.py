"""`tidd wait-ci` サブコマンド（Issue #3645）.

`gh pr checks <PR番号> --watch --fail-fast` は全チェックの状態を進捗が変わるたびに
一覧ごと再描画するため、チェック数 × 更新回数ぶんの行がそのまま LLM の文脈に
流れ込む（CI が数分かかる PR では数百行規模になる）。後続の分岐で実際に必要な
情報は「全通過したか」「失敗したならどのジョブとログ URL」の 2 点だけである。

本サブコマンドはポーリング出力を subprocess で capture して破棄し、stdout /
stderr には最終結果のみを出力する（`#3426` のテストランナー出力 capture 化と
同じ方針）。

終了コード:
- 0: 全チェック通過。stdout に `CI: all checks passed (PR #<N>, <通過件数> checks)` を 1 行出力。
  以下の 2 パターンも exit 0 になる（Issue #4048。「失敗」と誤検知しないための区別）:
  - PR に CI チェックが 1 件も存在しない場合（`gh pr checks` が `no checks reported on
    the '<branch>' branch` で非ゼロ終了するケース。CI 未設定・対象パスが CI 側で
    スキップされた等）。stdout に「チェックが1件も見つかりませんでした」の専用文言を出力
  - PR が呼び出し時点で既に `MERGED` の場合（`ai-review` の APPROVE 時に自動マージ・
    branch 削除まで完了しているケース）。checks 系コマンドは呼ばずに stdout に
    「既にマージ済みです」の専用文言を出力
- 1: チェック失敗（実際に FAILURE / ERROR / CANCELLED 状態のチェックが存在する場合のみ）。
  stderr に失敗チェック名とログ URL を 1 行 1 件で出力
- 2: タイムアウト（`--timeout` で指定した秒数を超過）。stderr に PR の checks URL を出力

`gh pr checks` は `.claude/hooks/enforce-mcp-in-skills.py` で「MCP 代替不能」として
明示的に許可されている操作のため、MCP tool へ置き換える経路は存在しない。
ラップするのは tidd 側になる（本サブコマンド）。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from typing import Any

from tidd_tools.shared import gh_client
from tidd_tools.shared.errors import SubprocessTimeoutError
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)

#: 既定タイムアウト（秒）。本リポジトリの PR で動く CI チェック（ruff format / ruff lint /
#: mypy / pytest 等の pre-flight commit status）は数分で完了するため、フレーク耐性を
#: 考慮して 10 分を既定値とする（Issue #3645 の「既存 CI の所要時間を確認して決める」）。
DEFAULT_TIMEOUT_SEC = 600

_PASS_STATE = "SUCCESS"
_FAIL_STATES = {"FAILURE", "ERROR", "CANCELLED"}

#: `gh pr checks` がチェック 0 件の PR に対して非ゼロ終了するときの stderr マーカー
#: （Issue #4048）。「本当に失敗したチェックがある」場合と区別するために使う。
_NO_CHECKS_STDERR_MARKER = "no checks reported"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "wait-ci",
        help="gh pr checks --watch のポーリング出力を要約して CI を待機する（Issue #3645）",
        description=__doc__,
    )
    parser.add_argument("pr", type=int, help="CI を待機する PR 番号")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"待機のタイムアウト秒（既定: {DEFAULT_TIMEOUT_SEC} 秒。超過時は subprocess を終了し exit 2）",
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    pr = int(args.pr)

    # Issue #4048: ai-review の APPROVE 時点で PR が既に自動マージ・branch 削除済みの
    # ケースでは、存在しない checks を待って「CI 失敗」と誤検知しないよう先にガードする。
    merged_message = _already_merged_message(pr)
    if merged_message is not None:
        print(merged_message)
        return 0

    watch_cmd = ["gh", "pr", "checks", str(pr), "--watch", "--fail-fast"]
    try:
        result = run_subprocess(watch_cmd, timeout=float(args.timeout))
    except SubprocessTimeoutError:
        # Issue #3645: 待機中の進捗が見えなくなる欠点への対策としてタイムアウト上限を必須化する
        print(
            f"CI チェック待機が timeout しました（--timeout {args.timeout} 秒）。"
            f"PR の checks ページを確認してください: {_checks_page_url(pr)}",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(f"ERROR: gh コマンドの実行に失敗しました: {exc}", file=sys.stderr)
        return 1

    fetched = _fetch_checks(pr)
    if result.returncode == 0:
        # 全通過: stdout に 1 行サマリのみ（ポーリング出力は素通ししない）
        passed = _count_passed(fetched.checks)
        print(f"CI: all checks passed (PR #{pr}, {passed} checks)")
        return 0

    # Issue #4048: チェックが1件も存在しない場合は「失敗」ではなく区別された文言で
    # exit 0 を返す（`gh pr checks` はチェック0件でも非ゼロ終了するため result.returncode
    # だけでは失敗と区別できない）。
    if fetched.no_checks:
        print(
            f"PR #{pr} にはチェックが1件も見つかりませんでした"
            "（CI 未設定、または対象パスがスキップされた可能性があります）。"
            "CI 待機は不要です。",
        )
        return 0

    # 失敗: 失敗チェック名とログ URL を stderr に 1 行 1 件で出力
    failed = _failed_checks(fetched.checks, pr)
    if not failed:
        print(
            f"CI チェックが失敗しましたが失敗詳細を取得できませんでした。"
            f"PR の checks ページを確認してください: {_checks_page_url(pr)}",
            file=sys.stderr,
        )
        return 1
    for name, link in failed:
        print(f"{name}: {link}", file=sys.stderr)
    return 1


# ── 内部 ────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class _ChecksFetch:
    """`_fetch_checks()` の結果（Issue #4048: 取得失敗とチェック0件を区別する）.

    Attributes:
        checks: 取得できたチェック一覧。取得自体に失敗した場合は None、
            チェックが正常に0件だった場合は空リスト `[]`（`no_checks=True` と併用）。
        no_checks: True の場合、PR に CI チェックが1件も存在しないことを意味する
            （取得失敗ではない）。`gh pr checks` はチェック0件でも非ゼロ終了し
            stderr に `no checks reported` を含むため、この文言で判定する。
    """

    checks: list[dict[str, Any]] | None
    no_checks: bool = False


def _fetch_checks(pr: int) -> _ChecksFetch:
    """`gh pr checks <PR> --json name,state,link` の結果を返す.

    失敗チェックのログ URL は本クエリの `link` フィールドから取得する（Issue #3645）。

    Issue #4048: `gh pr checks` はチェックが1件も存在しない PR に対しても非ゼロ終了し、
    stderr に `no checks reported on the '<branch>' branch` を出す。このケースは
    「取得自体の失敗」（ネットワークエラー・JSON パース不能等）とは区別し、
    `_ChecksFetch(checks=[], no_checks=True)` を返す。
    """
    cmd = ["gh", "pr", "checks", str(pr), "--json", "name,state,link"]
    try:
        result = run_subprocess(cmd)
    except OSError:
        return _ChecksFetch(checks=None)
    if result.returncode != 0:
        if _NO_CHECKS_STDERR_MARKER in (result.stderr or ""):
            return _ChecksFetch(checks=[], no_checks=True)
        return _ChecksFetch(checks=None)
    try:
        data: Any = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return _ChecksFetch(checks=None)
    if not isinstance(data, list):
        return _ChecksFetch(checks=None)
    if not data:
        # 取得コマンド自体は成功したが、結果が空リスト（0件）だったケースへの防御的対応。
        return _ChecksFetch(checks=[], no_checks=True)
    return _ChecksFetch(checks=data)


def _count_passed(checks: list[dict[str, Any]] | None) -> int:
    """`state == SUCCESS` のチェック件数を返す（取得失敗時は 0）."""
    if not checks:
        return 0
    return sum(1 for c in checks if c.get("state") == _PASS_STATE)


def _failed_checks(checks: list[dict[str, Any]] | None, pr: int) -> list[tuple[str, str]]:
    """失敗（FAILURE / ERROR / CANCELLED）チェックの `(name, log_url)` 一覧を返す.

    ログ URL が空のチェックは PR の checks ページ URL にフォールバックする。
    """
    if not checks:
        return []
    result: list[tuple[str, str]] = []
    for c in checks:
        if c.get("state") in _FAIL_STATES:
            name = c.get("name") or "unknown"
            link = c.get("link") or _checks_page_url(pr)
            result.append((name, link))
    return result


def _checks_page_url(pr: int) -> str:
    """PR の checks ページ URL を返す（`REPO` 環境変数 → `gh repo view` の順で解決）."""
    repo = os.environ.get("REPO")
    if not repo:
        repo = gh_client.repo_name_with_owner() or ""
    return f"https://github.com/{repo}/pull/{pr}/checks"


def _already_merged_message(pr: int) -> str | None:
    """PR が既に `MERGED` 状態なら案内メッセージを返す（Issue #4048）.

    `ai-review` が APPROVE 時点で自動マージ・branch 削除まで完了しているケースで
    `wait-ci` が呼ばれても、存在しない checks を待って「CI 失敗」と誤検知しないための
    事前ガード。`gh pr view` の取得に失敗した場合（OSError・非ゼロ終了・JSON パース
    不能・タイムアウト）は判定不能として None を返し、通常の CI 待機フローへフォールバック
    する（誤ってマージ済み扱いにしない安全側の設計）。
    """
    cmd = ["gh", "pr", "view", str(pr), "--json", "state,mergedAt"]
    try:
        result = run_subprocess(cmd)
    except (OSError, SubprocessTimeoutError):
        return None
    if result.returncode != 0:
        return None
    try:
        data: Any = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("state") == "MERGED" or data.get("mergedAt"):
        return f"PR #{pr} は既にマージ済みです。CI 待機は不要です。"
    return None
