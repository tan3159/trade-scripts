"""reviewdog インライン投稿（旧 ai-review.sh §8）.

公開 API:
- :func:`convert_to_rdjson` — レビュー出力の指摘事項を rdjson 文字列に変換
- :func:`resolve_rdjson_paths` — basename のみの path を PR 変更ファイルの完全パスに解決
- :func:`delete_bot_inline_comments` — 既存ボットインラインコメントを削除
- :func:`run_inline_comment_review` — メインフロー（変換 → 削除 → reviewdog 実行）
- :func:`append_backend_to_review_body` — review_body に Reviewer フッターを追加
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 旧 sh: [SEVERITY] file:line: メッセージ
_ISSUE_LINE_RE = re.compile(r"\[(?P<sev>CRITICAL|HIGH|MEDIUM|LOW|DEFERRED)\]\s+(?P<path>\S+):(?P<line>\d+):(?P<msg>.*)")
# マークダウンリンク `[text](url)` を text に正規化
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# バッククォート除去 `path:line` → path:line
_BACKTICK_RE = re.compile(r"`([^`]*)`")

_SECTION_HEADER_RE = re.compile(r"^## 指[摘引]事項", re.MULTILINE)


def _issues_section(review_output: str) -> str:
    lines = review_output.splitlines()
    start = -1
    for idx, line in enumerate(lines):
        if _SECTION_HEADER_RE.match(line):
            start = idx
            break
    if start == -1:
        return ""
    return "\n".join(lines[start:])


def _normalize_line(line: str) -> str:
    line = _MD_LINK_RE.sub(r"\1", line)
    line = _BACKTICK_RE.sub(r"\1", line)
    return line


_SEVERITY_MAP = {
    "CRITICAL": "ERROR",
    "HIGH": "ERROR",
    "MEDIUM": "WARNING",
    "LOW": "INFO",
    "DEFERRED": "INFO",
}


def convert_to_rdjson(review_output: str, *, backend_name: str = "agy") -> str:
    """指摘事項を rdjson 文字列に変換する（diagnostics は file:line 形式のみ採用）."""
    section = _issues_section(review_output)
    diagnostics: list[dict[str, Any]] = []
    if section:
        for raw_line in section.splitlines():
            line = _normalize_line(raw_line)
            m = _ISSUE_LINE_RE.search(line)
            if not m:
                continue
            sev = m.group("sev")
            path = m.group("path")
            try:
                line_num = int(m.group("line"))
            except ValueError:
                continue
            # メッセージ本文（": " より後ろをそのまま）
            full_msg = m.group("msg").lstrip()
            rd_sev = _SEVERITY_MAP.get(sev, "INFO")
            diagnostics.append(
                {
                    "message": full_msg,
                    "location": {
                        "path": path,
                        "range": {"start": {"line": line_num, "column": 1}},
                    },
                    "severity": rd_sev,
                }
            )
    source_name = f"{backend_name}-review"
    return json.dumps(
        {"source": {"name": source_name}, "diagnostics": diagnostics},
        ensure_ascii=False,
    )


def resolve_rdjson_paths(rdjson: str, pr_files: list[str]) -> str:
    """basename のみの path を PR 変更ファイルの完全パスに解決する."""
    if not pr_files:
        return rdjson
    try:
        data = json.loads(rdjson)
    except json.JSONDecodeError:
        return rdjson
    if not isinstance(data, dict):
        return rdjson
    basename_map = {Path(f).name: f for f in pr_files if f}
    diags = data.get("diagnostics", [])
    if not isinstance(diags, list):
        return rdjson
    for diag in diags:
        if not isinstance(diag, dict):
            continue
        loc = diag.get("location")
        if not isinstance(loc, dict):
            continue
        path = loc.get("path", "")
        if isinstance(path, str) and "/" not in path and path in basename_map:
            loc["path"] = basename_map[path]
    return json.dumps(data, ensure_ascii=False)


def _gh_env(app_token: str) -> dict[str, str]:
    env = dict(os.environ)
    if app_token:
        env["GH_TOKEN"] = app_token
    return env


def delete_bot_inline_comments(
    pr_num: str,
    repo: str,
    app_token: str,
    *,
    bot_login: str = "ai-reviewer-gaia-plan[bot]",
) -> None:
    """既存のボットインラインコメントをすべて削除する."""
    if not app_token:
        return
    env = _gh_env(app_token)
    print("==> 既存のボットインラインコメントを削除中...", file=sys.stderr)
    list_proc = subprocess.run(  # noqa: S603
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{pr_num}/comments",
            "--jq",
            f'.[] | select(.user.login == "{bot_login}") | .id',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=60,
    )
    if list_proc.returncode != 0:
        return
    for comment_id in list_proc.stdout.splitlines():
        comment_id = comment_id.strip()
        if not comment_id:
            continue
        delete_proc = subprocess.run(  # noqa: S603
            [
                "gh",
                "api",
                "--method",
                "DELETE",
                f"repos/{repo}/pulls/comments/{comment_id}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
            timeout=60,
        )
        if delete_proc.returncode != 0:
            print(f"WARN: コメント削除に失敗しました: {comment_id}", file=sys.stderr)


def _count_low_dropped(review_output: str) -> int:
    section = _issues_section(review_output)
    if not section:
        return 0
    total = len(re.findall(r"\[LOW\]", section))
    with_fileline = 0
    for raw in section.splitlines():
        line = _normalize_line(raw)
        if re.search(r"\[LOW\]\s+\S+:\d+:", line):
            with_fileline += 1
    return total - with_fileline


def _pr_files(pr_num: str, repo: str) -> list[str]:
    proc = subprocess.run(  # noqa: S603
        ["gh", "api", f"repos/{repo}/pulls/{pr_num}/files", "--jq", ".[].filename"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _pr_head_sha(pr_num: str, repo: str) -> str:
    proc = subprocess.run(  # noqa: S603
        ["gh", "pr", "view", str(pr_num), "--repo", repo, "--json", "headRefOid", "-q", ".headRefOid"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=60,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    proc = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
        errors="replace",
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_common_dir() -> Path:
    proc = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    raw = proc.stdout.strip()
    if proc.returncode == 0 and raw and raw != ".git":
        path = Path(raw)
        if path.is_absolute():
            return path.parent
    return Path.cwd()


def _count_bot_inline_comments(repo: str, pr_num: str, app_token: str, bot_login: str) -> int | None:
    env = _gh_env(app_token)
    proc = subprocess.run(  # noqa: S603
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{pr_num}/comments",
            "--jq",
            f'[.[] | select(.user.login == "{bot_login}")] | length',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    if not text.isdigit():
        return None
    return int(text)


def run_inline_comment_review(
    pr_num: str,
    repo: str,
    app_token: str,
    review_output: str,
    *,
    backend_name: str = "agy",
    bot_login: str = "ai-reviewer-gaia-plan[bot]",
) -> str:
    """rdjson 変換 → reviewdog 実行 + diff 外フィルター情報を返す.

    Returns:
        付加メッセージ（``review_body`` に追加するフッター文字列）。空文字なら付加なし。
    """
    if not review_output:
        print("WARN: review_output が空のためインラインコメントをスキップします。", file=sys.stderr)
        return ""

    low_dropped = _count_low_dropped(review_output)
    if low_dropped > 0:
        print(
            f"WARN: LOW 指摘 {low_dropped} 件に file:line 形式がないためスキップしました"
            "（行番号なし LOW は reviewdog に渡されません）",
            file=sys.stderr,
        )

    if not app_token:
        print(
            "WARN: BOT トークンの取得に失敗しました。インラインコメントをスキップします。",
            file=sys.stderr,
        )
        return ""

    rdjson = convert_to_rdjson(review_output, backend_name=backend_name)
    pr_files = _pr_files(pr_num, repo)
    if pr_files:
        rdjson = resolve_rdjson_paths(rdjson, pr_files)

    try:
        parsed = json.loads(rdjson)
        diag_count = len(parsed.get("diagnostics", []))
    except (json.JSONDecodeError, AttributeError):
        diag_count = 0

    if diag_count == 0:
        print("==> インラインコメント対象の指摘なし（スキップ）", file=sys.stderr)
        return "インラインコメント対象の指摘なし"

    if shutil.which("reviewdog") is None:
        print("WARN: reviewdog コマンドが見つかりません。インラインコメントをスキップします。", file=sys.stderr)
        print(
            "HINT: go install github.com/reviewdog/reviewdog/cmd/reviewdog@latest を実行してください。",
            file=sys.stderr,
        )
        return ""

    delete_bot_inline_comments(pr_num, repo, app_token, bot_login=bot_login)

    repo_owner, _, repo_name = repo.partition("/")
    commit_sha = _pr_head_sha(pr_num, repo)
    run_from_dir = _git_common_dir()

    env = dict(os.environ)
    env["REVIEWDOG_GITHUB_API_TOKEN"] = app_token
    env["CI_PULL_REQUEST"] = str(pr_num)
    env["CI_COMMIT"] = commit_sha
    env["CI_REPO_OWNER"] = repo_owner
    env["CI_REPO_NAME"] = repo_name

    print(f"==> reviewdog でインラインコメントを投稿中（{diag_count}件）...", file=sys.stderr)
    rd_proc = subprocess.run(  # noqa: S603
        ["reviewdog", "-f=rdjson", "-reporter=github-pr-review", "-filter-mode=added"],
        input=rdjson + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(run_from_dir),
        check=False,
        timeout=60,
    )
    if rd_proc.stdout:
        print(rd_proc.stdout, file=sys.stderr)
    if rd_proc.stderr:
        print(rd_proc.stderr, file=sys.stderr)
    if rd_proc.returncode != 0:
        print(
            "WARN: reviewdog の実行に失敗しました（スキップ）。VERDICT フローは継続します。",
            file=sys.stderr,
        )
        return ""

    after = _count_bot_inline_comments(repo, pr_num, app_token, bot_login)
    if after == 0:
        msg = "diff 外の行への指摘のみのためreviewdog🐶投稿しません（filter-mode=added でフィルター済み）"
        print(f"==> {msg}", file=sys.stderr)
        return msg
    return ""


def append_backend_to_review_body(review_body: str, backend_name: str) -> str:
    """``> Reviewer: <backend> (<model>)`` フッターを review_body に追加する.

    ``backend_name`` は ``"agy:gemini-3-pro"`` のように ``<backend>:<model>`` 形式か、
    後方互換のために ``"agy"`` のようなモデルなし形式を受け付ける。

    - ``"agy:gemini-3-pro"`` → ``"> Reviewer: agy (gemini-3-pro)"``
    - ``"agy:"`` (コロン後が空) → ``"> Reviewer: agy"``
    - ``"agy"`` (コロンなし旧形式) → ``"> Reviewer: agy"``
    - ``""`` (空文字) → 本文をそのまま返す
    """
    if not backend_name:
        return review_body
    if ":" in backend_name:
        backend, _, model = backend_name.partition(":")
        display = f"{backend} ({model})" if model else backend
    else:
        display = backend_name
    return f"{review_body}\n\n> Reviewer: {display}\n"
