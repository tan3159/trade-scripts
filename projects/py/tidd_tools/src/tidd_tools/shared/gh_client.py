"""gh CLI ラッパー（拡充版 / Phase 2-A #1053・token 注入 / API 拡充 #2940）.

Phase 1 (#1050) の `tidd_tools.shared.gh` を引き継ぎ:

- 失敗時は `GhCommandError` を送出（Phase 1 の `GhError` を共通 `errors.GhCommandError` に統合）
- `pr_view(pr_num, fields=...)` で dict を返す（Phase 1 の `pr_body` 等を吸収）
- `pr_diff_files / pr_list / issue_view / pr_edit_body / pr_comment / issue_create /
  issue_list_open` のサブコマンド群を提供
- `shell=False` 強制（`subprocess_runner.run` を経由）

旧 `tidd_tools.shared.gh` は本モジュールに再エクスポートするため、Phase 1 の `from tidd_tools.shared import gh` 経由
の呼び出しも互換に保つ。

Issue #2940（token 注入 / API 拡充）:

- 各 API が `token=` kwarg を受け取り、指定時のみ `GH_TOKEN` を注入した env で
  gh を実行する（`ai_review/backends.py` の `_gh_env_with_token()` パターンを踏襲）。
  省略時は `run_subprocess` のデフォルト（プロセス env のスナップショット）をそのまま使う。
- 迂回の原因になっていた不足 API を追加: `gh_json`（汎用 JSON ヘルパー）/ `pr_diff`
  （diff 全文）/ `pr_head_sha`（HEAD SHA）/ `pr_merge`（マージ）/
  `issue_label_add` / `issue_label_remove`（ラベル付与・除去、over-fetch scope 問題を
  回避する REST API 直叩き。`issue_progress_label.py` の `_run_label_api` パターンを踏襲）。
  呼び出し元（`ai_review/core.py` 等）の移行は後続 Issue で行う。

Issue #2960（core.py / subcommands.py の gh 直接呼び出し移行）:

- `_run_gh` に既定 timeout（`DEFAULT_TIMEOUT_SECONDS`）を導入し、呼び出し元ごとにバラバラ
  だった `timeout=60` / `timeout=30` / timeout なしの不統一を解消する。
- `resolve_repo()` を追加し、`ai_review/core.py` と `ai_review/subcommands.py` に
  重複していた `_resolve_repo()`（REPO 環境変数 → `gh repo view` 解決）を一本化する。
- `gh_raw()` を追加。`gh_json()` は stdout 全体を単一 JSON として `json.loads` するため
  `--jq '.foo[] | {...}'` のような JSON Lines（1 行 1 オブジェクト）出力には使えない。
  `gh_raw()` は生の stdout 文字列をそのまま返す（パースは呼び出し側の責務）。
- `commit_status_create()` を追加。`POST /repos/{repo}/statuses/{sha}` による
  Commit Status 投稿（`ai_review/core.py` の `_post_bats_commit_status` /
  `_post_commit_status` に重複していた実装を統合）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import urllib.parse
from collections.abc import Sequence
from typing import Any

from tidd_tools.retry import retry_with_backoff
from tidd_tools.shared.errors import DiffTooLargeError, GhCommandError, SubprocessTimeoutError
from tidd_tools.shared.subprocess_runner import run as run_subprocess

logger = logging.getLogger(__name__)


# Phase 1 (#1050) 互換: 旧 `tidd_tools.shared.gh` で公開していた例外名を維持する。
GhError = GhCommandError

# Issue #2960: core.py / subcommands.py の gh 直接呼び出しは timeout=60 / timeout=30 /
# timeout なしとバラバラだった。呼び出し元の大半が使っていた 60 秒を既定値として統一する。
DEFAULT_TIMEOUT_SECONDS = 60.0

# Issue #3743: `gh pr diff` が GitHub API の diff media type 上限（20000 行）を超えたときの
# stderr に含まれる識別文字列。406 エラーでも rate limit 等とは区別して検知するために使う。
DIFF_TOO_LARGE_MARKER = "the diff exceeded the maximum number of lines"


def is_diff_too_large_error(stderr: str) -> bool:
    """gh の 406 エラーが diff 行数上限超過（20000 行）由来かを判定する（Issue #3743）.

    ``gh pr diff`` は diff が 20000 行を超えると
    ``HTTP 406: Sorry, the diff exceeded the maximum number of lines (20000)`` で失敗する。
    この「PR 構造上の制約」はクォータ枯渇（時間経過で回復）や恒久的な環境破損とは原因が
    異なるため、stderr のマーカーで識別可能にする。

    Args:
        stderr: gh コマンドの stderr 文字列。

    Returns:
        マーカー（``DIFF_TOO_LARGE_MARKER``）が含まれていれば True。
    """
    return DIFF_TOO_LARGE_MARKER in stderr


def _run_gh(args: list[str], *, check: bool = True, capture: bool = True, token: str | None = None) -> str:
    """gh コマンドを実行して stdout 文字列を返す.

    **デフォルトは `check=True`**（書き込み系の `pr_edit_body` / `pr_comment` /
    `issue_create` 等は capture=False で呼ぶが check は明示しないためデフォルトの
    True が効き、失敗時に `GhCommandError` を送出する）。
    `pr_body` / `pr_diff_files` のように「失敗時に空を返したい」ヘルパーは
    呼び出し側で `try / except GhCommandError` を使う。
    `_run_gh(args, check=False)` はモジュール内の内部用途に限定する。

    `token` を指定した場合のみ `os.environ` のコピーに `GH_TOKEN` を上書きした env を
    渡す（Issue #2940）。省略時は `env=None` のまま `run_subprocess` に委譲し、
    プロセス env のスナップショットをそのまま使う（挙動を変えない）。

    `DEFAULT_TIMEOUT_SECONDS` を常に指定する（Issue #2960）。

    `errors="replace"` を指定する（Issue #4125）。`gh` は外部データ（PR diff 等）を
    そのまま stdout に流すため、非 UTF-8 バイト（Shift_JIS フィクスチャ等）が混ざり得る。
    strict デコード（既定）だと 1 バイトの不正入力で `UnicodeDecodeError` が送出され、
    `tidd ai-review` が exit code 0 のままトレースバックだけ出して verdict を出さずに
    終わる（呼び出し側からは「レビューが走ったのに結果が無い」状態に見える）。
    """
    env = None
    if token:
        env = dict(os.environ)
        env["GH_TOKEN"] = token
    result = run_subprocess(["gh", *args], capture=capture, env=env, timeout=DEFAULT_TIMEOUT_SECONDS, errors="replace")
    if result.returncode != 0:
        if check:
            raise GhCommandError(args, result.returncode, result.stderr or "")
        return ""
    return result.stdout.rstrip("\n") if capture else ""


def gh_json(args: Sequence[str], *, token: str | None = None) -> Any:
    """任意の gh コマンドを実行し、stdout を JSON としてパースして返す（汎用ヘルパー）.

    `pr_view` / `issue_view` / `pr_list` 等の定義済みラッパーでカバーできない gh
    サブコマンド（例: `gh api ... --jq ...`）を JSON 付きで呼びたい場合に使う。
    stdout が空の場合は None を返す。JSON としてパースできない場合は `GhCommandError`
    を送出する。
    """
    args_list = list(args)
    raw = _run_gh(args_list, token=token)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GhCommandError(args_list, 0, f"invalid JSON from gh: {exc}") from exc


def gh_raw(args: Sequence[str], *, token: str | None = None) -> str:
    """任意の gh コマンドを実行し、生の stdout 文字列を返す（JSON Lines 等パース前提の呼び出し向け・#2960）.

    `gh_json` は stdout 全体を単一 JSON として `json.loads` するため、
    `--jq '.foo[] | {...}'` のように 1 行 1 オブジェクトの JSON Lines を返す呼び出しには
    使えない。本関数はパースを一切行わず生テキストを返す。
    取得失敗時は WARN ログを出して空文字列を返す（fail-soft）。
    """
    args_list = list(args)
    try:
        return _run_gh(args_list, token=token)
    except GhCommandError as exc:
        logger.warning("gh コマンド実行失敗: %s", exc)
        return ""


# ── 取得系 ──────────────────────────────────────────────────────────────


def repo_name_with_owner(*, token: str | None = None) -> str | None:
    """`gh repo view --json nameWithOwner -q .nameWithOwner` を返す。取得失敗時は None."""
    try:
        return _run_gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], token=token) or None
    except GhCommandError:
        return None


def resolve_repo(env_repo: str | None, *, token: str | None = None) -> str:
    """REPO 環境変数 → 未設定なら `gh repo view` で解決する（Issue #2960）.

    `ai_review/core.py` と `ai_review/subcommands.py` に重複していた `_resolve_repo()`
    （挙動: 解決できない場合は `RuntimeError`）を一本化する。
    """
    if env_repo:
        return env_repo
    repo = repo_name_with_owner(token=token)
    if not repo:
        raise RuntimeError("リポジトリ名を取得できませんでした。gh CLI が正しく設定されているか確認してください。")
    return repo


def pr_view(
    pr_num: str | int,
    *,
    repo: str | None = None,
    fields: Sequence[str] = ("number", "title", "body", "state"),
    token: str | None = None,
) -> dict[str, Any]:
    """PR メタデータを取得する。`gh pr view --json <fields>` の結果を dict で返す."""
    pr_str = str(pr_num)
    args = ["pr", "view", pr_str, "--json", ",".join(fields)]
    if repo:
        args = ["pr", "view", pr_str, "--repo", repo, "--json", ",".join(fields)]
    raw = _run_gh(args, token=token)
    try:
        data: Any = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise GhCommandError(args, 0, f"invalid JSON from gh: {exc}") from exc
    if not isinstance(data, dict):
        raise GhCommandError(args, 0, f"expected dict, got {type(data).__name__}")
    return data


def pr_body(
    pr_num: str | int,
    repo: str | None = None,
    *,
    token: str | None = None,
    raise_on_error: bool = False,
) -> str:
    """PR ボディ文字列を取得する（Phase 1 互換ヘルパー）.

    取得失敗時は既定で WARN ログを出して空文字を返す（旧 sh の挙動・fail-soft）。

    ``raise_on_error=True``（Issue #3788）を指定すると、取得失敗時に空文字で
    握りつぶさず ``GhCommandError`` を送出する。「認証失敗等でボディを取得できな
    かった」と「PR ボディが実際に空文字列」を区別する必要がある呼び出し元
    （``test_plan.run()``）向けの opt-in。既存呼び出し元の挙動は変えない
    （既定値 False で従来どおり）。
    """
    try:
        data = pr_view(pr_num, repo=repo, fields=("body",), token=token)
    except GhCommandError as exc:
        logger.warning("PR ボディ取得失敗: %s", exc)
        if raise_on_error:
            raise
        return ""
    body = data.get("body", "")
    return body if isinstance(body, str) else ""


def pr_diff_files(pr_num: str | int, repo: str | None = None, *, token: str | None = None) -> list[str]:
    """PR diff の変更ファイル一覧を返す.

    Issue #3743: `gh pr diff --name-only` は diff が 20000 行を超えると 406（diff-too-large）
    で失敗する。この場合は ``gh api .../pulls/{pr}/files --paginate`` によるファイル単位
    取得へフォールバックする（``pr_diff_files_via_api``）。フォールバックも失敗した場合は
    従来どおり WARN + 空リストの fail-soft 契約を維持する。
    """
    pr_str = str(pr_num)
    args = ["pr", "diff", pr_str, "--name-only"]
    if repo:
        args = ["pr", "diff", pr_str, "--repo", repo, "--name-only"]
    try:
        out = _run_gh(args, token=token)
    except GhCommandError as exc:
        if is_diff_too_large_error(exc.stderr):
            logger.warning(
                "PR diff が20000行上限を超えています。REST API のファイル単位取得へフォールバックします（#3743）。"
            )
            return pr_diff_files_via_api(pr_str, repo, token=token)
        logger.warning("PR diff 取得失敗: %s", exc)
        return []
    return [line for line in out.splitlines() if line.strip()]


def pr_diff_files_via_api(pr_num: str | int, repo: str | None = None, *, token: str | None = None) -> list[str]:
    """REST API `GET /repos/{owner}/{repo}/pulls/{pr}/files --paginate` で変更ファイル一覧を取得する（Issue #3743）.

    ``gh pr diff --name-only`` が diff 20000 行上限（406）で失敗する大規模 PR 向けの代替取得手段。
    ``repo`` 省略時は ``repo_name_with_owner()`` で解決する。取得失敗時は WARN + 空リスト
    （fail-soft）を返す。
    """
    resolved = repo or repo_name_with_owner(token=token)
    if not resolved:
        return []
    args = ["api", f"repos/{resolved}/pulls/{pr_num}/files", "--paginate", "--jq", ".[].filename"]
    try:
        raw = _run_gh(args, token=token)
    except GhCommandError as exc:
        logger.warning("PR diff ファイル一覧の REST API 取得失敗: %s", exc)
        return []
    return [line for line in raw.splitlines() if line.strip()]


def pr_file_patch(pr_num: str | int, path: str, repo: str | None = None, *, token: str | None = None) -> str | None:
    """指定 PR の指定ファイルの unified diff patch（`files[].patch`）を REST API で取得する（Issue #3925）.

    `check_pr_conflicts` が「複数 PR による同一ファイルへの変更が意味的に非衝突（純追記で
    追加内容が重複しない）か」を判定するために使う。`pr_diff_files_via_api` と同様に
    ``GET /repos/{owner}/{repo}/pulls/{pr}/files --paginate`` を使うが、``--jq`` で
    ``{filename, patch}`` の JSON Lines に変換してから該当ファイルの patch のみ取り出す
    （複数ページ・多数ファイルでも単一 JSON として一括パースしない）。

    ``repo`` 省略時は ``repo_name_with_owner()`` で解決する。取得失敗・該当ファイルなし・
    patch フィールドなしの場合は None を返す（fail-soft。呼び出し側は保守的に競合として
    扱うこと）。
    """
    resolved = repo or repo_name_with_owner(token=token)
    if not resolved:
        return None
    args = [
        "api",
        f"repos/{resolved}/pulls/{pr_num}/files",
        "--paginate",
        "--jq",
        ".[] | {filename, patch}",
    ]
    try:
        raw = _run_gh(args, token=token)
    except GhCommandError as exc:
        logger.warning("PR ファイル patch の REST API 取得失敗: %s", exc)
        return None
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("filename") == path:
            patch = obj.get("patch")
            return patch if isinstance(patch, str) else None
    return None


def pr_diff(pr_num: str | int, repo: str | None = None, *, token: str | None = None) -> str:
    """PR の完全な diff テキストを取得する（`gh pr diff` の `--name-only` なし版・#2940）.

    Issue #3743: diff が 20000 行を超える 406 失敗は ``DiffTooLargeError`` を送出する
    （クォータ枯渇・環境破損とは区別する）。それ以外の失敗は従来どおり WARN ログを出して
    空文字を返す（`pr_diff_files` と同じ fail-soft 方針）。
    """
    pr_str = str(pr_num)
    args = ["pr", "diff", pr_str]
    if repo:
        args = ["pr", "diff", pr_str, "--repo", repo]
    try:
        return _run_gh(args, token=token)
    except GhCommandError as exc:
        if is_diff_too_large_error(exc.stderr):
            raise DiffTooLargeError(exc.command_args, exc.returncode, exc.stderr) from exc
        logger.warning("PR diff 取得失敗: %s", exc)
        return ""


def pr_head_sha(pr_num: str | int, repo: str | None = None, *, token: str | None = None) -> str:
    """PR の HEAD commit SHA（`headRefOid`）を取得する（#2940）.

    取得失敗時は WARN ログを出して空文字を返す。`git rev-parse HEAD` 等へのフォールバック
    は呼び出し元の責務とする（`ai_review/core.py` の `_fetch_head_sha` パターン）。
    """
    try:
        data = pr_view(pr_num, repo=repo, fields=("headRefOid",), token=token)
    except GhCommandError as exc:
        logger.warning("PR HEAD SHA 取得失敗: %s", exc)
        return ""
    sha = data.get("headRefOid", "")
    return sha if isinstance(sha, str) else ""


def pr_list(
    *,
    repo: str | None = None,
    state: str = "open",
    limit: int = 50,
    fields: Sequence[str] = ("number", "title", "state"),
    token: str | None = None,
) -> list[dict[str, Any]]:
    """PR 一覧を取得する."""
    args = ["pr", "list", "--state", state, "--limit", str(limit), "--json", ",".join(fields)]
    if repo:
        args = ["pr", "list", "--repo", repo, "--state", state, "--limit", str(limit), "--json", ",".join(fields)]
    raw = _run_gh(args, token=token)
    try:
        data: Any = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise GhCommandError(args, 0, f"invalid JSON from gh: {exc}") from exc
    if not isinstance(data, list):
        raise GhCommandError(args, 0, f"expected list, got {type(data).__name__}")
    return data


def issue_view(
    issue_num: str | int,
    *,
    repo: str | None = None,
    fields: Sequence[str] = ("number", "title", "body", "state", "labels"),
    token: str | None = None,
) -> dict[str, Any]:
    """Issue メタデータを取得する."""
    issue_str = str(issue_num)
    args = ["issue", "view", issue_str, "--json", ",".join(fields)]
    if repo:
        args = ["issue", "view", issue_str, "--repo", repo, "--json", ",".join(fields)]
    raw = _run_gh(args, token=token)
    try:
        data: Any = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise GhCommandError(args, 0, f"invalid JSON from gh: {exc}") from exc
    if not isinstance(data, dict):
        raise GhCommandError(args, 0, f"expected dict, got {type(data).__name__}")
    return data


def issue_list_open(*, title_phrase: str, repo: str | None = None, token: str | None = None) -> list[dict[str, Any]]:
    """指定タイトル句を含む Open Issue 一覧を返す."""
    search = f'"{title_phrase}" in:title'
    args = ["issue", "list", "--state", "open", "--search", search, "--json", "number,title"]
    if repo:
        args = [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--search",
            search,
            "--json",
            "number,title",
        ]
    try:
        raw = _run_gh(args, token=token)
    except GhCommandError:
        return []
    try:
        data: Any = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def existing_open_issue(title: str, repo: str | None, *, token: str | None = None) -> bool:
    """同タイトルの Open Issue が既に存在するか.

    Phase 1 の `gh.existing_open_issue` を互換ヘルパーとして残す。
    """
    return bool(issue_list_open(title_phrase=title, repo=repo, token=token))


# ── 書き込み系 ──────────────────────────────────────────────────────────


def _write_temp_body(body: str) -> str:
    """body 文字列を一時 md ファイルに書き出してパスを返す."""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md", encoding="utf-8") as tmp:
        tmp.write(body)
        return tmp.name


def pr_edit_body(pr_num: str | int, repo: str | None, body: str, *, token: str | None = None) -> None:
    """PR ボディを更新する（REST API・Issue #3776）.

    旧実装の `gh pr edit --body-file` は内部で requestedReviewers（Team の
    `name`/`slug`・User の `login`）を GraphQL 経由で取得するため、トークンに
    `read:org` スコープが無いと `INSUFFICIENT_SCOPES` で失敗する（#3695 の
    `label-pr.py` と同一の根本原因）。REST の
    `PATCH /repos/{owner}/{repo}/pulls/{pr_num}` は repo スコープのみで動作するため
    こちらに置き換える。`repo` 省略時は `repo_name_with_owner()` で解決し、解決できない
    場合は `GhCommandError` を送出する（`issue_label_add()` と同じパターン）。

    Issue #3808: `gh api` の `-f`（`--raw-field`. 文字列パラメータ）は `@<path>` に
    よるファイル読み込みをサポートしない。ファイルからの読み込みは `-F`
    （`--field`. 型付きパラメータ）専用の機能であり、`-f body=@<path>` を使うと gh は
    `@<path>` をリテラル文字列としてそのまま送信してしまう（実害: PR #3807 で PR
    ボディが一時ファイルパス文字列に上書きされた）。そのため必ず `-F` を使うこと。
    """
    resolved_repo = repo or repo_name_with_owner(token=token)
    if not resolved_repo:
        raise GhCommandError(["api", "-X", "PATCH", "repos/.../pulls/..."], 0, "repo の解決に失敗しました")
    path = _write_temp_body(body)
    try:
        args = ["api", "-X", "PATCH", f"repos/{resolved_repo}/pulls/{pr_num}", "-F", f"body=@{path}"]
        _run_gh(args, capture=False, token=token)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def update_pr_body(pr_num: str | int, repo: str | None, body: str) -> None:
    """Phase 1 互換エイリアス."""
    pr_edit_body(pr_num, repo, body)


def issue_edit_body(issue_num: str | int, repo: str | None, body: str, *, token: str | None = None) -> None:
    """Issue ボディを更新する (Issue #1534)."""
    path = _write_temp_body(body)
    try:
        args = ["issue", "edit", str(issue_num), "--body-file", path]
        if repo:
            args = ["issue", "edit", str(issue_num), "--repo", repo, "--body-file", path]
        _run_gh(args, capture=False, token=token)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def pr_comment(pr_num: str | int, repo: str | None, body: str, *, token: str | None = None) -> None:
    """PR にコメントを投稿する."""
    path = _write_temp_body(body)
    try:
        args = ["pr", "comment", str(pr_num), "--body-file", path]
        if repo:
            args = ["pr", "comment", str(pr_num), "--repo", repo, "--body-file", path]
        _run_gh(args, capture=False, token=token)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def comment_pr(pr_num: str | int, repo: str | None, body: str) -> None:
    """Phase 1 互換エイリアス."""
    pr_comment(pr_num, repo, body)


def issue_comment(issue_num: str | int, repo: str | None, body: str, *, token: str | None = None) -> None:
    """Issue にコメントを投稿する (Issue #1921)."""
    path = _write_temp_body(body)
    try:
        args = ["issue", "comment", str(issue_num), "--body-file", path]
        if repo:
            args = ["issue", "comment", str(issue_num), "--repo", repo, "--body-file", path]
        _run_gh(args, capture=False, token=token)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def issue_create(title: str, body: str, labels: list[str], repo: str | None, *, token: str | None = None) -> None:
    """Issue を新規作成する.

    Issue #4130: `capture=True` で呼ぶ（デフォルト）。呼び出し元
    `_classify_gh_error`（`watch_circleci_failures.py`）は `GhCommandError.stderr` の
    文字列マッチ（`"rate limit" in stderr_lower` 等）で分類するが、`capture=False` だと
    `subprocess.run(capture_output=False)` が stdout/stderr を `None` のまま返すため
    `stderr` が常に空文字になり判定が機能しなかった（#4124/#4129 と同型のバグ）。
    成功時の stdout は使わないため `capture=True` にしても副作用はない
    （他の呼び出し元 `model_drift.py` / `analyze_loop_errors.py` も戻り値を使わない）。
    """
    path = _write_temp_body(body)
    try:
        args = ["issue", "create", "--title", title, "--body-file", path]
        if repo:
            args = ["issue", "create", "--repo", repo, "--title", title, "--body-file", path]
        for label in labels:
            args.extend(["--label", label])
        _run_gh(args, token=token)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def create_issue(title: str, body: str, labels: list[str], repo: str | None) -> None:
    """Phase 1 互換エイリアス."""
    issue_create(title, body, labels, repo)


def pr_merge(
    pr_num: str | int,
    repo: str | None = None,
    *,
    squash: bool = True,
    delete_branch: bool = True,
    token: str | None = None,
) -> None:
    """PR をマージする（デフォルト: squash + delete-branch・#2940）.

    失敗時は `GhCommandError` を送出する（呼び出し元が WARN ログを出して継続するかは
    呼び出し元の責務。`ai_review/subcommands.py` の `gh pr merge --squash --delete-branch`
    パターンを踏襲）。
    """
    args = ["pr", "merge", str(pr_num)]
    if repo:
        args += ["--repo", repo]
    if squash:
        args.append("--squash")
    if delete_branch:
        args.append("--delete-branch")
    _run_gh(args, capture=False, token=token)


# Issue #3957: commit status 投稿の gh api が一時的にタイムアウトしただけで tidd ai-review
# 全体を落とさないよう、短いバックオフ付きでリトライする回数（初回に加えて最大この回数だけ再試行）。
_COMMIT_STATUS_MAX_RETRIES = 2


def commit_status_create(
    repo: str,
    sha: str,
    state: str,
    context: str,
    description: str,
    *,
    token: str | None = None,
    max_retries: int = _COMMIT_STATUS_MAX_RETRIES,
) -> None:
    """GitHub Commit Status を 1 件投稿する（`POST /repos/{repo}/statuses/{sha}`・#2960）.

    `ai_review/core.py` に重複していた `_post_bats_commit_status` / `_post_commit_status`
    の実装を統合する。失敗時は `GhCommandError` を送出する（`stderr`/`returncode` を保持する
    ため `capture` は既定の True のまま。呼び出し元が WARN ログを出すかは呼び出し元の責務）。

    Issue #3957: `gh api` が一時的にタイムアウトしただけで `tidd ai-review` プロセス全体が
    落ちていた（`_run_gh` は `DEFAULT_TIMEOUT_SECONDS` 超過で `SubprocessTimeoutError` を送出し、
    旧実装はこれをリトライせずそのまま伝播していた）。`SubprocessTimeoutError` のみを対象に
    短いバックオフ（1s, 2s, ...）付きでリトライする。成功時は追加待機ゼロ。恒久的な失敗
    （422 等の `GhCommandError`）はリトライ対象外で即座に送出する（リトライしても解消しないため）。
    リトライ上限到達時は最後の `SubprocessTimeoutError` をそのまま送出する（呼び出し元の
    `ai_review/test_statuses.py::_post_commit_status` が捕捉して fail-soft に扱う）。
    """
    args = [
        "api",
        "--method",
        "POST",
        f"repos/{repo}/statuses/{sha}",
        "-f",
        f"state={state}",
        "-f",
        f"context={context}",
        "-f",
        f"description={description}",
    ]

    def _post() -> None:
        _run_gh(args, token=token)

    def _on_retry(attempt: int, wait: float, result: object, exc: BaseException | None) -> None:
        logger.warning(
            "commit status 投稿がタイムアウトしました（context=%s）。%.0fs 後にリトライします（%d/%d）。",
            context,
            wait,
            attempt + 1,
            max_retries,
        )

    retry_with_backoff(
        _post,
        max_retries=max_retries,
        is_success=None,
        on_retry=_on_retry,
        on_exhausted=None,
        retry_exceptions=(SubprocessTimeoutError,),
    )


def issue_label_add(
    issue_num: str | int,
    labels: Sequence[str],
    repo: str | None = None,
    *,
    token: str | None = None,
) -> None:
    """Issue（PR も同じ番号空間のため可）にラベルを付与する（#2940）.

    `gh issue edit --add-label` は内部で GraphQL 経由の over-fetch により `read:org`
    scope を要求するため、`repo` scope のみで通る REST API
    `POST /repos/{repo}/issues/{n}/labels` を使う
    （`issue_progress_label.py` の `_run_label_api` パターンを踏襲）。
    `repo` 省略時は `repo_name_with_owner()` で解決する。解決できない場合は
    `GhCommandError` を送出する。
    """
    resolved_repo = repo or repo_name_with_owner(token=token)
    if not resolved_repo:
        raise GhCommandError(["api", "-X", "POST", "/repos/.../issues/.../labels"], 0, "repo の解決に失敗しました")
    args = ["api", "-X", "POST", f"/repos/{resolved_repo}/issues/{issue_num}/labels"]
    for label in labels:
        args.extend(["-f", f"labels[]={label}"])
    _run_gh(args, capture=False, token=token)


def issue_label_remove(
    issue_num: str | int,
    label: str,
    repo: str | None = None,
    *,
    token: str | None = None,
) -> None:
    """Issue（PR も可）からラベルを 1 件除去する（REST API・#2940）.

    `DELETE /repos/{repo}/issues/{n}/labels/{name}` を使う。`repo` 省略時は
    `repo_name_with_owner()` で解決する。解決できない場合は `GhCommandError` を送出する。

    Issue #4129: `capture=True` で呼ぶ（デフォルト）。呼び出し元
    `_remove_needs_human_merge_label`（`ai_review/subcommands.py`）は
    `GhCommandError.stderr` の内容（404 `Label does not exist` か否か）で分岐するが、
    `capture=False` だと `subprocess.run(capture_output=False)` が stdout/stderr を
    `None` のまま返すため `stderr` が常に空文字になり判定が機能しなかった
    （#4124 で一度修正したはずが再発）。成功時の stdout は使わないため
    `capture=True` にしても副作用はない。
    """
    resolved_repo = repo or repo_name_with_owner(token=token)
    if not resolved_repo:
        raise GhCommandError(
            ["api", "-X", "DELETE", "/repos/.../issues/.../labels/..."], 0, "repo の解決に失敗しました"
        )
    encoded_label = urllib.parse.quote(label, safe="")
    args = ["api", "-X", "DELETE", f"/repos/{resolved_repo}/issues/{issue_num}/labels/{encoded_label}"]
    _run_gh(args, token=token)
