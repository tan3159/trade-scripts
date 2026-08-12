"""Stop hook の gone/merged ローカルブランチ自動削除（Issue #1033・#1565・#2967）.

`.claude/hooks/on-stop.py` に同居していたブランチ掃除ロジックを移設したもの。

- ``detect_default_branch()``: `origin/HEAD` からデフォルトブランチ名を解決する。
- ``cleanup_gone_branches()``: リモート追跡参照が `[gone]` のローカルブランチを削除する。
- ``cleanup_merged_pr_branches()``: MERGED PR に紐づくローカルブランチを削除する
  （squash merge 運用で `[gone]` にならないケースをカバーする）。
  checkout 中のブランチ・OPEN PR があるブランチは保持する。

stdlib のみ使用。git/gh subprocess 実行は ``_lib/git_helpers.py`` に委譲する。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_helpers import run_gh as _gh  # type: ignore[import-not-found]
from git_helpers import run_git_in_repo as _git  # type: ignore[import-not-found]


def detect_default_branch(repo: str) -> str:
    rc, out, _ = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    if rc != 0:
        return "main"
    name = out.strip()
    name = name.replace("refs/remotes/origin/", "", 1)
    return name or "main"


def cleanup_gone_branches(repo: str, default_branch: str) -> None:
    # Issue #1175: fetch はネットワーク I/O のためデフォルト 8s より長めの 15s。
    # 全体は AI_REVIEW_STOP_HOOK_TIMEOUT で覆われるので、fetch 個別の timeout は
    # global timeout 上限（デフォルト 15s）と揃えておく。
    _git(repo, "fetch", "--prune", timeout=15)

    rc, out, _ = _git(
        repo,
        "for-each-ref",
        "--format=%(refname:short) %(upstream:track)",
        "refs/heads",
    )
    if rc != 0:
        return

    gone: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "[gone]" and parts[0] != default_branch:
            gone.append(parts[0])

    if not gone:
        return

    sys.stderr.write(
        f"on-stop: リモート削除済みブランチを検出しました: {' '.join(gone)} \n"
    )
    for branch in gone:
        _, out_b, err_b = _git(repo, "branch", "-D", "--", branch)
        for line in (out_b + err_b).splitlines():
            if line:
                sys.stderr.write(f"on-stop: {line}\n")


def _checked_out_branches(repo: str) -> set[str]:
    rc, out, _ = _git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        return set()
    result: set[str] = set()
    for line in out.splitlines():
        if line.startswith("branch refs/heads/"):
            result.add(line[len("branch refs/heads/") :])
    return result


def _all_local_branches(repo: str, default_branch: str) -> list[str]:
    rc, out, _ = _git(repo, "branch", "--format=%(refname:short)")
    if rc != 0:
        return []
    return [b for b in out.splitlines() if b and b != default_branch]


def _fetch_pr_state_map(repo_nwo: str) -> dict[str, set[str]]:
    """Issue #1565: `gh pr list --state all` を **1 回** だけ叩いてブランチ→状態集合を返す.

    従来はブランチごとに `gh pr list --head <branch> --state open` / `... --state merged`
    を N+1 で呼んでいたため 20 ブランチで最大 40 回の gh 呼び出しになっていた。
    本関数は 1 回の bulk クエリに統合し、Python 側で dict にマップして各ブランチの
    state を lookup できる形に変換する。

    Returns:
        {"branch_name": {"OPEN", "MERGED", "CLOSED"}, ...} 形式の dict。
        1 ブランチに複数の PR が紐付いている場合は state 集合になる（例: 過去 MERGED あり + 現在 OPEN）。
        gh 呼び出しが失敗した場合や JSON parse 失敗の場合は空 dict を返す。
    """
    args = ["pr", "list"]
    if repo_nwo:
        args.extend(["--repo", repo_nwo])
    # `--limit 200`: 実運用の並行 PR 数 (STEP 0 上限 5) を大きく上回る余裕を持たせる
    args.extend(["--state", "all", "--limit", "200", "--json", "headRefName,state"])
    rc, out = _gh(*args)
    if rc != 0:
        return {}
    try:
        parsed = json.loads(out or "[]")
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, list):
        return {}

    result: dict[str, set[str]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        branch = item.get("headRefName")
        state = item.get("state")
        if not isinstance(branch, str) or not isinstance(state, str):
            continue
        result.setdefault(branch, set()).add(state)
    return result


def cleanup_merged_pr_branches(repo: str, default_branch: str) -> None:
    """Issue #1565: bulk `gh pr list --state all` で N+1 を解消する.

    従来はブランチごとに `_pr_count(open)` / `_pr_count(merged)` を呼び出していたため
    20 ブランチで最大 40 回の gh 呼び出しになっていた。本改修で `gh pr list` の呼び出しは
    1 回（+ `gh repo view` 1 回）に定数化される。
    """
    if shutil.which("gh") is None:
        return
    checked_out = _checked_out_branches(repo)
    all_branches = _all_local_branches(repo, default_branch)
    rc, repo_nwo = _gh(
        "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"
    )
    repo_nwo = repo_nwo.strip() if rc == 0 else ""

    # bulk クエリ: 1 回の gh 呼び出しで全 PR の (branch, state) を取得する
    pr_state_map = _fetch_pr_state_map(repo_nwo)

    for branch in all_branches:
        if branch in checked_out:
            continue
        states = pr_state_map.get(branch, set())
        # OPEN が 1 件でもあれば保持
        if "OPEN" in states:
            continue
        # MERGED があれば削除対象
        if "MERGED" in states:
            sys.stderr.write(
                f"on-stop: MERGED PR 紐付きブランチを削除します: {branch}\n"
            )
            _, out_b, err_b = _git(repo, "branch", "-D", "--", branch)
            for line in (out_b + err_b).splitlines():
                if line:
                    sys.stderr.write(f"on-stop: {line}\n")
