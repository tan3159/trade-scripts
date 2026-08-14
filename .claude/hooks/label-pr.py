#!/usr/bin/env python3
"""PostToolUse hook: gh pr create 実行後にブランチ名から type: ラベルを付与する.

**Issue #1295 で silent fail を修正:**
  - 旧実装は常に exit 0 で失敗が観測できず、監査データで PR 20 件全て未付与を確認（監査 §2-2）
  - 修正後: skip・成功・失敗を stderr にログ出力し、失敗は exit 2 で可視化する

**Issue #1296 で size ラベル自動付与を追加:**
  - 閾値マッピング: XS(<10) / S(<50) / M(<250) / L(<500) / XL(<1000) / XXL(>=1000)
  - `size/XX` ラベルを type: ラベルと合わせて付与
  - XXL は ai-review 側で自動 REQUEST_CHANGES gate の対象になる（別モジュール）

**Issue #1947 で branch / 追加行数の取得元を PR 自体に変更:**
  - hook はプロジェクトルート（main checkout・branch=main）で実行される一方、
    TiDD ワークフローでは PR を worktree ブランチから作成するため、旧実装の
    `git rev-parse` / `git diff origin/main..HEAD` は常に main を見て全 PR skip
    （nightly label-pr SLO 付与率 0.00 の根本原因）
  - `gh pr view <N> --json headRefName,additions` から取得する

**Issue #2865 で 2 つの取りこぼしを修正:**
  - PR 作成が `mcp__github__create_pull_request`（GitHub MCP tool）経由に移行したセッションでは、
    本 hook が `PostToolUse[Bash]` にしか登録されていなかったため一切発火しなかった
    （`gh pr create` の Bash 実行が前提の設計だった）。`PostToolUse[mcp__github__create_pull_request]`
    にも登録し、tool_response の schema に依存せず `tool_input.head`（PR 作成時に指定した branch）
    を SoT として `gh pr list --head <branch>` で PR 番号を解決する。
  - `-R`/`--repo` がセッションと**同一リポジトリ**を指しているだけの冗長な指定でも
    無条件でクロスリポジトリ扱いされ skip されていた。`git remote get-url origin` から
    実際のセッションリポジトリを解決し、一致する場合は通常どおりラベル付与を継続する。

**Issue #3695 でラベル付与を REST API 化:**
  - `gh pr edit --add-label` / `--remove-label` は内部で requestedReviewers（Team の
    `name`/`slug`・User の `login`）を GraphQL 取得するため、トークンに `read:org` スコープが
    無いと `INSUFFICIENT_SCOPES` で全体が失敗し、ラベルが付与されない（#3694 で発生）。
  - ラベル付与は本質的に REST 完結の操作のため、`gh api` の
    `POST/DELETE /repos/{owner}/{repo}/issues/{n}/labels` に置き換えた（repo スコープのみで動作）。
    owner/repo は cwd の git remote ではなく PR 自身の `gh pr view --json headRepository` から解決する。

**Issue #3800 で失敗履歴を追記専用 JSONL 化:**
  - `~/.cache/label-pr/last-run.json`（Issue #1445）は直近 1 回分の状態を上書きするため、
    失敗した実行の直後に別 PR 作成が成功すると、過去の失敗詳細（reason/failure_details）が
    失われ、nightly の label-pr-slo が SLO 違反を検知しても事後調査ができない
    （#3800 で実際に発生: 20 merged PR 中 7 件がラベル欠落したが原因を追えなかった）。
  - 失敗（`status == "failed"`）時のみ `~/.cache/label-pr/failures.jsonl` に追記する。
    last-run.json のスキーマ・既存の呼び出し元は変更しない（`_write_last_run` 内部で
    条件付きに追記するのみ）。

hook 失敗原則（`docs/reference/hooks.md` §失敗原則 参照）:
  - **stderr にログ + exit 2 を必ず返す**
  - **silent success（常時 exit 0）は禁止**

label-pr.yml（GitHub Actions）の代替（Issue #868）。
stdlib のみ使用。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.gh_command import is_gh_pr_create as _is_gh_pr_create
from _lib.hook_io import (
    get_command,
    get_tool_name,
    get_tool_output,
    is_hook_enabled,
    read_hook_input,
    resolve_target_cwd,
)

_PR_URL_RE = re.compile(r"https://github\.com/[^/]+/[^/]+/pull/(\d+)")
# Issue #2226: `-R`/`--repo` フラグ検出（セッションリポジトリ以外への PR 作成）
_REPO_FLAG_RE = re.compile(r"(?:^|\s)(?:-R|--repo)(?:=|\s+)(\S+)")
# Issue #2865: gh remote URL（ssh/https どちらの形式でも owner/repo を抽出）
_REMOTE_OWNER_REPO_RE = re.compile(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$")
# Issue #2865: PR 作成が mcp__github__create_pull_request（GitHub MCP tool）経由のときの tool_name
_MCP_CREATE_PR_TOOL = "mcp__github__create_pull_request"

# ブランチ名プレフィックス → type ラベル
_BRANCH_TO_LABEL: dict[str, str] = {
    "feat": "type: feat",
    "fix": "type: fix",
    "refactor": "type: refactor",
    "build": "type: build",
    "ci": "type: ci",
    "docs": "type: docs",
    "research": "type: research",
}

# Issue #1296: size ラベル閾値（追加行数ベース）
# 監査 §4-2 line 911-931 の指摘: 500 行超 PR が 33% を占め最大 2,167 行という状態。
# 閾値の考え方:
#   XS   <10   : typo・1 行修正
#   S    <50   : 小さい機能追加・単純な修正
#   M    <250  : 標準的な機能追加
#   L    <500  : 大きめの機能追加・複数ファイル
#   XL   <1000 : 分割検討対象
#   XXL  >=1000: 自動 REQUEST_CHANGES gate の対象
_SIZE_THRESHOLDS: list[tuple[int, str]] = [
    (10, "size/XS"),
    (50, "size/S"),
    (250, "size/M"),
    (500, "size/L"),
    (1000, "size/XL"),
]
_SIZE_XXL = "size/XXL"


def _fetch_pr_meta(pr_number: str) -> dict[str, Any] | None:
    """gh pr view から headRefName / additions を取得する (Issue #1947).

    hook はプロジェクトルート（main checkout・branch=main）で実行される一方、
    PR は worktree ブランチから作成されるため、ローカル checkout の
    ``git rev-parse`` / ``git diff`` では branch も追加行数も取得できない。
    PR 自体を SoT として GitHub から取得する。

    Returns:
        ``{"headRefName": str, "additions": int}`` 相当の dict。取得失敗時は None。
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_number, "--json", "headRefName,additions"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        meta = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return meta if isinstance(meta, dict) else None


def _pr_number_for_branch(branch: str) -> str | None:
    """branch に紐づく open PR 番号を ``gh pr list --head`` で解決する (Issue #2048 / #2865).

    Args:
        branch: 検索対象の headRefName。

    Returns:
        PR number を文字列で返す。取得失敗・該当 PR なしの場合は None。
    """
    try:
        pr_result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "number,headRefName",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if pr_result.returncode != 0:
        return None
    try:
        data = json.loads(pr_result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    number = data[0].get("number")
    if number is None:
        return None
    return str(number)


def _fetch_pr_number_by_branch(payload: dict[str, Any] | None = None) -> str | None:
    """現在の worktree ブランチに紐づく PR 番号を gh pr list --head で fallback 解決する (Issue #2048).

    ``gh pr create`` が Warning のみ出力して URL を返さなかった場合に呼ばれる。

    hook はプロジェクトルート（main checkout・branch=main）で動く場合があるため、
    ``gh pr view`` 引数なし（カレントブランチ依存）は確実性が低い。代わりに:

    1. ``payload.get("cwd")`` → なければ ``os.getcwd()`` で hook 実行時の cwd を取得
    2. その cwd で ``git rev-parse --abbrev-ref HEAD`` を呼んでブランチ名を取得
    3. ``_pr_number_for_branch`` でブランチ名から PR 番号を解決する

    Returns:
        PR number を文字列で返す。取得失敗時は None を返す。
    """
    # cwd を決定: payload.cwd → os.getcwd() の順で fallback
    cwd: str | None = None
    if payload is not None:
        cwd = payload.get("cwd") or None
    if not cwd:
        cwd = os.getcwd()

    # cwd で git rev-parse してブランチ名を取得
    try:
        git_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            cwd=cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if git_result.returncode != 0:
        return None
    branch = git_result.stdout.strip()
    if not branch or branch == "HEAD":
        return None

    return _pr_number_for_branch(branch)


def _session_repo(cwd: str | None = None) -> str | None:
    """``git remote get-url origin`` からセッションリポジトリ（``owner/repo``）を解決する (Issue #2865).

    `-R`/`--repo`（Bash 経路）・``owner``/``repo``（mcp 経路）がこのリポジトリと同一かどうかの
    判定に使う。ssh/https いずれの remote URL 形式にも対応する。

    Args:
        cwd: git コマンドを実行するディレクトリ。None ならプロセスの現在の cwd を使う。

    Returns:
        ``"owner/repo"`` 文字列。取得・解析に失敗した場合は None。
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            cwd=cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    match = _REMOTE_OWNER_REPO_RE.search(url)
    if not match:
        return None
    return match.group(1)


def _owner_repo_for_pr(pr_number: str) -> str | None:
    """PR のリポジトリ ``owner/repo`` を ``gh pr view --json headRepository`` で解決する (Issue #3695).

    REST のラベル操作（``gh api .../repos/{owner}/{repo}/issues/{n}/labels``）には owner/repo が
    必要。cwd の ``git remote get-url origin`` に依存せず PR 自身から取得する
    （クロスリポジトリ PR は既に skip 済みのため base repo と一致する・#2865）。

    Returns:
        ``"owner/repo"`` 文字列。取得失敗時は None。
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_number, "--json", "headRepository"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    head_repo = data.get("headRepository")
    if not isinstance(head_repo, dict):
        return None
    name_with_owner = head_repo.get("nameWithOwner")
    if isinstance(name_with_owner, str) and "/" in name_with_owner:
        return name_with_owner
    return None


def _added_lines_override() -> int | None:
    """テスト用環境変数 ``LABEL_PR_TEST_ADDED_LINES`` から追加行数を返す."""
    override = os.environ.get("LABEL_PR_TEST_ADDED_LINES")
    if override is None:
        return None
    try:
        return int(override)
    except ValueError:
        return None


def _size_label_for(added_lines: int) -> str:
    """追加行数から size ラベル名を返す（境界: <閾値 で下位ラベル）."""
    for threshold, label in _SIZE_THRESHOLDS:
        if added_lines < threshold:
            return label
    return _SIZE_XXL


def _last_run_path() -> Path:
    """発火履歴 JSON のパスを解決する (Issue #1445).

    XDG Base Directory Specification に従い ``XDG_CACHE_HOME`` を優先し、
    未設定なら ``$HOME/.cache`` に fallback する。テストは ``XDG_CACHE_HOME``
    を tmp_path 配下に向けることで隔離する。
    """
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        base = Path(cache_home)
    else:
        home = os.environ.get("HOME") or os.path.expanduser("~")
        base = Path(home) / ".cache"
    return base / "label-pr" / "last-run.json"


def _failure_log_path() -> Path:
    """失敗履歴 JSONL（追記専用）のパスを解決する (Issue #3800).

    ``_last_run_path()`` は直近 1 回分の状態を上書きするため、次回実行が成功すると
    過去の失敗詳細（reason/failure_details）が失われ、nightly の label-pr-slo が
    SLO 違反を検知しても個別実行の失敗原因を事後調査できない問題が #3800 で
    実際に発生した。失敗（``status == "failed"``）時のみ本ファイルに追記する。
    """
    return _last_run_path().parent / "failures.jsonl"


def _append_failure_log(record: dict[str, Any]) -> None:
    """失敗レコードを追記専用 JSONL に追記する (Issue #3800).

    書き込み失敗（disk full 等）は hook 全体をブロックしないよう silently 握り潰す
    （``_write_last_run`` と同じ fail-safe 方針）。
    """
    path = _failure_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            f.write("\n")
    except OSError as exc:
        sys.stderr.write(f"label-pr.py: WARN: failures.jsonl 書き込みに失敗: {exc}\n")


def _write_last_run(record: dict[str, Any]) -> None:
    """発火履歴 JSON を書き込む (Issue #1445).

    hook 経路そのものが欠落した状態を後から検知するための audit ログ。
    書き込み失敗（disk full 等）は hook 全体をブロックしないよう silently 握り潰す。
    失敗（``status == "failed"``）時は ``_append_failure_log`` で追記専用 JSONL にも
    記録し、直近の成功実行によって過去の失敗詳細が失われないようにする (Issue #3800)。
    """
    record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    path = _last_run_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        sys.stderr.write(f"label-pr.py: WARN: last-run.json 書き込みに失敗: {exc}\n")

    if record.get("status") == "failed":
        _append_failure_log(record)


def _add_label(owner_repo: str, pr_number: str, label: str) -> tuple[bool, str]:
    """REST API でラベルを追加し (成功, 詳細) を返す (Issue #3695).

    ``gh pr edit --add-label`` は内部で requestedReviewers（Team の name/slug・User の
    login）を GraphQL 取得するため read:org スコープが必要になる。REST の
    ``POST /repos/{owner}/{repo}/issues/{n}/labels`` は repo スコープのみで動く。
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{owner_repo}/issues/{pr_number}/labels",
                "-f",
                f"labels[]={label}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except FileNotFoundError:
        return False, "gh コマンドが見つかりません"
    except subprocess.TimeoutExpired:
        return False, "30 秒でタイムアウト"
    if result.returncode != 0:
        stderr_head = (result.stderr or "").strip().splitlines()[:3]
        detail = " | ".join(stderr_head) if stderr_head else f"exit={result.returncode}"
        return False, detail
    return True, ""


def _remove_label(owner_repo: str, pr_number: str, label: str) -> tuple[bool, str]:
    """REST API でラベルを除去し (成功, 詳細) を返す (Issue #2049 / #3695).

    ``gh api --method DELETE .../labels/{urlencode(name)}``。ラベル名の ``:`` ``/`` は
    URL エンコードする。既にラベルが存在しない（HTTP 404）場合は冪等成功として扱う
    （``gh pr edit --remove-label`` と同じセマンティクスを維持・#3695）。
    """
    encoded_name = quote(label, safe="")
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "DELETE",
                f"repos/{owner_repo}/issues/{pr_number}/labels/{encoded_name}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except FileNotFoundError:
        return False, "gh コマンドが見つかりません"
    except subprocess.TimeoutExpired:
        return False, "30 秒でタイムアウト"
    if result.returncode != 0:
        if "HTTP 404" in (result.stderr or ""):
            # 既にラベルが存在しない → 冪等成功（gh pr edit --remove-label 相当）
            return True, ""
        stderr_head = (result.stderr or "").strip().splitlines()[:3]
        detail = " | ".join(stderr_head) if stderr_head else f"exit={result.returncode}"
        return False, detail
    return True, ""


def _fetch_existing_size_labels(pr_number: str) -> list[str] | None:
    """PR の既存 size/* ラベル一覧を返す (Issue #2049).

    ``gh pr view <pr> --json labels`` を呼び、``size/`` prefix のラベル名リストを返す。
    取得失敗時は None を返す（呼び出し元は既存挙動（_add_label のみ）で継続する）。
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_number, "--json", "labels"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    labels = data.get("labels")
    if not isinstance(labels, list):
        return None
    return [
        item["name"]
        for item in labels
        if isinstance(item, dict) and str(item.get("name", "")).startswith("size/")
    ]


def _normalize_repo_ref(value: str) -> str | None:
    """`-R`/`--repo` フラグ値・mcp owner/repo を ``owner/repo`` 形式に正規化する (Issue #2865).

    ``gh pr create -R`` は ``[HOST/]OWNER/REPO`` またはリポジトリ URL を受け付ける。
    ``_session_repo()`` の戻り値（``owner/repo``）と比較できる形式に揃える。

    Args:
        value: `-R`/`--repo` フラグの値。

    Returns:
        ``"owner/repo"`` 文字列。解析できない場合は None。
    """
    value = value.strip()
    if not value:
        return None
    match = _REMOTE_OWNER_REPO_RE.search(value)
    if match:
        return match.group(1)
    parts = [p for p in value.split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return None


def _apply_labels_for_pr(pr_number: str) -> int:
    """PR 番号に対して type: / size: ラベルを解決・付与する (Issue #2865).

    branch 解決（headRefName の取得）以降の処理を担う共通経路。
    `gh pr create`（Bash）・`mcp__github__create_pull_request` のどちらの経路でも
    PR 番号さえ解決できればこの関数に委譲する。

    Returns:
        exit code（0: 成功・スキップ / 2: 失敗）。
    """
    # Issue #1947: hook はプロジェクトルート（branch=main）で実行されるため、
    # ローカル checkout ではなく PR の headRefName / additions を SoT とする。
    meta: dict[str, Any] | None = None
    branch = os.environ.get("LABEL_PR_BATS_BRANCH") or ""
    if not branch:
        meta = _fetch_pr_meta(pr_number)
        if meta is None:
            sys.stderr.write(
                f"label-pr.py: FAILED: gh pr view {pr_number} で PR メタ情報"
                "（headRefName/additions）を取得できませんでした\n"
            )
            _write_last_run(
                {
                    "status": "failed",
                    "reason": "gh pr view failed",
                    "pr_number": pr_number,
                }
            )
            return 2
        branch = str(meta.get("headRefName") or "")
    if not branch:
        sys.stderr.write("label-pr.py: skip: cannot determine PR branch\n")
        _write_last_run(
            {
                "status": "skipped",
                "reason": "cannot determine PR branch",
                "pr_number": pr_number,
            }
        )
        return 0

    exit_code = 0
    applied_labels: list[str] = []
    failure_details: list[str] = []

    # ── type: ラベル付与 ────────────────────────────────────────────
    prefix = branch.split("/", 1)[0]
    type_label = _BRANCH_TO_LABEL.get(prefix)
    if type_label is None:
        sys.stderr.write(
            f"label-pr.py: skip: no matching prefix for branch {branch!r}\n"
        )
        _write_last_run(
            {
                "status": "skipped",
                "reason": "no matching branch prefix",
                "branch": branch,
                "pr_number": pr_number,
            }
        )
        return exit_code

    # Issue #3695: REST のラベル操作には owner/repo が必要。cwd の git remote ではなく
    # PR 自身の headRepository から解決する
    owner_repo = _owner_repo_for_pr(pr_number)
    if owner_repo is None:
        sys.stderr.write(
            f"label-pr.py: FAILED: PR #{pr_number} の owner/repo を取得できませんでした"
            "（gh pr view --json headRepository）\n"
        )
        _write_last_run(
            {
                "status": "failed",
                "reason": "owner/repo resolution failed",
                "pr_number": pr_number,
            }
        )
        return 2

    ok, detail = _add_label(owner_repo, pr_number, type_label)
    if ok:
        sys.stderr.write(
            f"label-pr.py: PR #{pr_number} に '{type_label}' ラベルを付与しました\n"
        )
        applied_labels.append(type_label)
    else:
        sys.stderr.write(f"label-pr.py: gh label 付与に失敗しました: {detail}\n")
        failure_details.append(f"type: {detail}")
        exit_code = 2

    # ── size: ラベル付与（Issue #1296 / #1947 で PR additions ベースに変更）──
    added = _added_lines_override()
    if added is None:
        if meta is None:
            meta = _fetch_pr_meta(pr_number)
        if meta is not None and isinstance(meta.get("additions"), int):
            added = meta["additions"]
    if added is None:
        sys.stderr.write(
            "label-pr.py: skip: PR の additions が取得できませんでした（size ラベル未付与）\n"
        )
        _write_last_run(
            {
                "status": "applied" if exit_code == 0 else "failed",
                "applied_labels": applied_labels,
                "failure_details": failure_details,
                "branch": branch,
                "pr_number": pr_number,
                "size_skip_reason": "pr additions unavailable",
            }
        )
        return exit_code

    size_label = _size_label_for(added)

    # Issue #2049: 既存 size/* ラベルを取得して重複除去してから付与する
    existing_size_labels = _fetch_existing_size_labels(pr_number)
    if existing_size_labels is None:
        sys.stderr.write(
            "label-pr.py: WARN: 既存 size ラベルの取得に失敗しました（add-label のみ実行）\n"
        )
    else:
        # 古い size/* ラベルを除去（新しい size_label と一致しないもの）
        for old_label in existing_size_labels:
            if old_label != size_label:
                ok_rm, detail_rm = _remove_label(owner_repo, pr_number, old_label)
                if ok_rm:
                    sys.stderr.write(
                        f"label-pr.py: PR #{pr_number} の旧 size ラベル '{old_label}' を除去しました\n"
                    )
                else:
                    sys.stderr.write(
                        f"label-pr.py: gh label remove failed: '{old_label}' 除去に失敗しました: {detail_rm}\n"
                    )
                    failure_details.append(f"size-remove({old_label}): {detail_rm}")
                    exit_code = 2

    # size_label が既存に含まれていれば add-label をスキップ（変更なし）
    size_label_already_present = (
        existing_size_labels is not None and size_label in existing_size_labels
    )
    if size_label_already_present:
        sys.stderr.write(
            f"label-pr.py: PR #{pr_number} に '{size_label}' 付与済み・変更なし（スキップ）\n"
        )
        applied_labels.append(size_label)
    elif exit_code == 0 or existing_size_labels is None:
        ok, detail = _add_label(owner_repo, pr_number, size_label)
        if ok:
            sys.stderr.write(
                f"label-pr.py: PR #{pr_number} に '{size_label}' ラベルを付与しました（追加 {added} 行）\n"
            )
            applied_labels.append(size_label)
        else:
            # Issue #1445: size ラベル失敗も exit 2 に格上げして silent 経路を除去。
            # size/XX ラベル未作成の環境では docs/reference/pr-size-labels.md の手順で
            # 事前作成が必要（GitHub Actions ワークフローで対応済み想定）。
            sys.stderr.write(
                f"label-pr.py: gh label add failed: size ラベル付与に失敗しました: {detail}"
                f"（size/XX ラベルが未作成の可能性があります。docs/reference/pr-size-labels.md 参照）\n"
            )
            failure_details.append(f"size: {detail}")
            exit_code = 2

    _write_last_run(
        {
            "status": "applied" if exit_code == 0 else "failed",
            "applied_labels": applied_labels,
            "failure_details": failure_details,
            "branch": branch,
            "pr_number": pr_number,
            "added_lines": added,
        }
    )
    return exit_code


def _handle_mcp_create_pr(payload: dict[str, Any]) -> int:
    """``mcp__github__create_pull_request`` 経路のラベル付与 (Issue #2865).

    tool_response の schema に依存せず、PR 作成時に指定した ``tool_input.head``
    （branch）を SoT として ``gh pr list --head`` で PR 番号を解決する。
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    branch = str(tool_input.get("head") or "")
    if not branch:
        sys.stderr.write("label-pr.py: skip: mcp tool_input.head が空です\n")
        _write_last_run(
            {
                "status": "skipped",
                "reason": "mcp tool_input.head missing",
            }
        )
        return 0

    owner = tool_input.get("owner")
    repo = tool_input.get("repo")
    if owner and repo:
        mcp_repo = f"{owner}/{repo}"
        session_repo = _session_repo(resolve_target_cwd(payload))
        if session_repo is not None and mcp_repo != session_repo:
            sys.stderr.write(
                f"label-pr.py: skip: mcp owner/repo {mcp_repo!r} がセッションリポジトリ "
                f"{session_repo!r} と異なります（クロスリポジトリ PR のため、ラベル付与をスキップします）\n"
            )
            _write_last_run(
                {
                    "status": "skipped",
                    "reason": "cross-repo PR (mcp owner/repo mismatch)",
                    "repo_flag": mcp_repo,
                    "branch": branch,
                }
            )
            return 0

    pr_number = _pr_number_for_branch(branch)
    if pr_number is None:
        sys.stderr.write(
            f"label-pr.py: FAILED: gh pr list --head {branch} で PR 番号を解決できませんでした（mcp 経路）\n"
        )
        _write_last_run(
            {
                "status": "failed",
                "reason": "gh pr list --head failed (mcp)",
                "branch": branch,
            }
        )
        return 2

    return _apply_labels_for_pr(pr_number)


def _main() -> int:
    payload = read_hook_input(hook_name="PostToolUse")  # Issue #1364
    tool_name = get_tool_name(payload)

    if tool_name == _MCP_CREATE_PR_TOOL:
        return _handle_mcp_create_pr(payload)

    if tool_name != "Bash":
        return 0

    command = get_command(payload)
    if not _is_gh_pr_create(command):
        return 0

    # Issue #2226 / #2865: `-R`/`--repo` 指定時、セッションと同一リポジトリを
    # 指しているだけなら通常どおり処理を継続する。異なる・判定不能な場合のみ
    # `gh pr view`/`gh pr edit` の誤動作を避けるためスキップする。
    repo_flag_match = _REPO_FLAG_RE.search(command)
    if repo_flag_match:
        repo_flag = repo_flag_match.group(1)
        normalized_flag = _normalize_repo_ref(repo_flag)
        session_repo = _session_repo(resolve_target_cwd(payload, command))
        if session_repo is not None and normalized_flag == session_repo:
            sys.stderr.write(
                f"label-pr.py: -R/--repo {repo_flag!r} はセッションリポジトリ {session_repo!r} と"
                "一致するため、ラベル付与を継続します\n"
            )
        else:
            sys.stderr.write(
                f"label-pr.py: skip: gh pr create に -R/--repo {repo_flag!r} が指定されています"
                "（セッションリポジトリ以外への PR のため、ラベル付与をスキップします）\n"
            )
            _write_last_run(
                {
                    "status": "skipped",
                    "reason": "cross-repo PR (-R/--repo specified)",
                    "repo_flag": repo_flag,
                    "command": command[:200],
                }
            )
            return 0

    # ここから先は「gh pr create の PostToolUse」経路。すべての exit 点で
    # 発火履歴を残して silent skip を無くす（Issue #1445）。
    output = get_tool_output(payload)

    if not output.strip():
        # output が空の場合は skip: `gh pr create` が失敗した / stdout を捕捉していない
        sys.stderr.write("label-pr.py: skip: tool_response.output が空です\n")
        _write_last_run(
            {
                "status": "skipped",
                "reason": "empty tool output",
                "command": command[:200],
            }
        )
        return 0

    match = _PR_URL_RE.search(output)
    if not match:
        # 非空だが PR URL が抽出できない → gh pr list --head <branch> で fallback 解決を試みる（#2048）
        sys.stderr.write(
            f"label-pr.py: PR URL not found in output: {output[:200]!r} — trying fallback via gh pr list --head\n"
        )
        fallback_number = _fetch_pr_number_by_branch(payload)
        if fallback_number is None:
            # fallback も失敗 → exit 2 で可視化（#1445 Scenario 3）
            sys.stderr.write(
                "label-pr.py: FAILED to extract PR number from output: fallback via gh pr list --head <branch> also failed\n"
            )
            _write_last_run(
                {
                    "status": "failed",
                    "reason": "PR URL extraction failed",
                    "output_head": output[:200],
                }
            )
            return 2
        sys.stderr.write(
            f"label-pr.py: fallback resolved PR number via gh pr list --head: #{fallback_number}\n"
        )
        pr_number = fallback_number
    else:
        pr_number = match.group(1)

    return _apply_labels_for_pr(pr_number)


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("label-pr"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
