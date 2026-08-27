"""`tidd label-pr-slo` サブコマンド (#1605).

直近 20 merged PR について size/ プレフィックスのラベル付与率を計算し、
閾値（既定 0.8）を下回った場合に exit 1 で終了する SLO チェック。

- 付与率 >= 閾値 → exit 0、JSON を stdout に出力
- 付与率 < 閾値 → exit 1、JSON を stdout に出力、stderr に "SLO violated: X.XX < Y.YY"
- サンプル 0 件 → exit 0、stderr に "skip: no merged PR in window"、JSON を stdout に出力

閾値は環境変数 LABEL_PR_SLO_MIN_RATE で override できる（既定 0.8）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)

DEFAULT_MIN_RATE = 0.8
DEFAULT_LIMIT = 20
ENV_MIN_RATE = "LABEL_PR_SLO_MIN_RATE"


def _ensure_gh_auth() -> bool:
    """GH_TOKEN / GITHUB_TOKEN が未設定の場合 GH_PAT をフォールバックとして設定する.

    CircleCI 環境では GH_PAT が認証トークンとして設定されているが、gh CLI が
    認証に使う変数は GH_TOKEN / GITHUB_TOKEN のみ (#1779)。本関数はその橋渡しを行う。

    Returns:
        True: 認証情報が利用可能（設定済み or フォールバック成功）
        False: GH_TOKEN / GITHUB_TOKEN / GH_PAT いずれも未設定
    """
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return True

    gh_pat = os.environ.get("GH_PAT")
    if gh_pat:
        os.environ["GH_TOKEN"] = gh_pat
        logger.debug("GH_TOKEN を GH_PAT からフォールバック設定しました")
        return True

    return False


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "label-pr-slo",
        help="直近 merged PR の size/ ラベル付与率を SLO チェックする (#1605)",
        description=(
            "直近 20 merged PR について size/ プレフィックスのラベル付与率を計算する。"
            f"付与率が閾値（既定 {DEFAULT_MIN_RATE}）を下回ると exit 1 で終了する。"
            f"閾値は環境変数 {ENV_MIN_RATE} で override できる。"
        ),
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def _fetch_merged_prs(limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """gh pr list で直近 merged PR を取得して解析した JSON リストを返す.

    Raises:
        GhCommandError: gh CLI が非ゼロ終了した場合
    """
    result = run_subprocess(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "merged",
            "--limit",
            str(limit),
            "--json",
            "labels,number",
        ]
    )
    if result.returncode != 0:
        raise GhCommandError(
            args=["pr", "list"],
            returncode=result.returncode,
            stderr=result.stderr,
        )
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def _compute_rate(prs: list[dict[str, Any]]) -> tuple[float, list[int]]:
    """size/ ラベル付与率と未付与 PR 番号リストを返す.

    Args:
        prs: `_fetch_merged_prs` が返した PR リスト

    Returns:
        (rate, missing_pr_numbers):
            rate: 付与率 [0.0, 1.0]（サンプル 0 件のときは 0.0）
            missing_pr_numbers: size/ ラベルが付いていない PR 番号のリスト
    """
    if not prs:
        return 0.0, []

    missing: list[int] = []
    for pr in prs:
        has_size = any(label["name"].startswith("size/") for label in pr.get("labels", []))
        if not has_size:
            missing.append(pr["number"])

    rate = (len(prs) - len(missing)) / len(prs)
    return rate, missing


def run_slo(min_rate: float = DEFAULT_MIN_RATE, limit: int = DEFAULT_LIMIT) -> int:
    """SLO チェックのビジネスロジック.

    Args:
        min_rate: 合格とみなす最低付与率
        limit: チェック対象の merged PR 件数

    Returns:
        exit code: 0 (OK / skip) or 1 (SLO 違反)
    """
    prs = _fetch_merged_prs(limit=limit)

    if not prs:
        print("skip: no merged PR in window", file=sys.stderr)
        payload: dict[str, Any] = {"rate": 0.0, "sample_size": 0, "missing_prs": []}
        print(json.dumps(payload))
        return 0

    rate, missing = _compute_rate(prs)
    payload = {
        "rate": round(rate, 10),
        "sample_size": len(prs),
        "missing_prs": missing,
    }
    print(json.dumps(payload))

    if rate < min_rate:
        print(
            f"SLO violated: {rate:.2f} < {min_rate:.2f}",
            file=sys.stderr,
        )
        return 1

    return 0


def run_cli(args: argparse.Namespace) -> int:
    """CLI エントリポイント.

    環境変数 LABEL_PR_SLO_MIN_RATE から閾値を読み取って run_slo を呼ぶ。
    GH_TOKEN / GITHUB_TOKEN が未設定の場合は GH_PAT をフォールバックとして設定する (#1779)。
    GH_TOKEN / GITHUB_TOKEN / GH_PAT がすべて未設定の場合は exit 0 + 警告で non-blocking に終了する。
    """
    if not _ensure_gh_auth():
        print(
            "WARN: GH_TOKEN / GITHUB_TOKEN / GH_PAT いずれも未設定。label-pr-slo をスキップします (#1779)",
            file=sys.stderr,
        )
        return 0

    raw = os.environ.get(ENV_MIN_RATE, str(DEFAULT_MIN_RATE))
    try:
        min_rate = float(raw)
    except ValueError:
        print(
            f"ERROR: {ENV_MIN_RATE}='{raw}' は float に変換できません",
            file=sys.stderr,
        )
        return 2

    return run_slo(min_rate=min_rate)
