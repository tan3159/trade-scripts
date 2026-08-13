#!/usr/bin/env python3
"""SessionStart hook: Copier テンプレートが古いことを通知する (#1224).

consumer リポジトリの `.copier-answers.yml` に記録された `_commit` と、
上流テンプレートの最新タグを比較して、drift があれば
「`tidd copier-update` を実行してください」と stderr に案内する。

親 #1197 Phase 7 の GitHub Actions（`copier-update.yml`）を撤去した (#1224) 後の
代替手段。Claude Code セッション開始のたびに consumer に staleness を気付かせる。

前提:
- stdlib のみで動作する（consumer に追加パッケージを要求しない）
- SessionStart hook として exit 0 でセッションをブロックしない
- ネットワーク不通や `git` 未導入なら黙って終了する（開発を邪魔しない）

環境変数:
- `TIDD_COPIER_LATEST_TAG_OVERRIDE`: テスト用。指定するとネットワーク問い合わせをスキップして
  この値を最新タグとみなす。空文字列を渡すと「取得失敗」と同じ扱いになる。
- `TIDD_COPIER_UPSTREAM_URL`: 上流リポジトリ URL の上書き（優先される）。

Issue #3683: 上流 URL は `.copier-answers.yml` の `_src_path` から解決する
（consumer に残る上流固有文字列を `_src_path` の 1 箇所に局所化する）。
`TIDD_COPIER_UPSTREAM_URL` が設定されていればそれを最優先で使う。

on/off:
- `tidd config enable notify-copier-staleness --machine` で有効化
- `tidd config disable notify-copier-staleness --machine` で無効化（#2366）
- 旧 `TIDD_COPIER_OFFLINE=1` は #2366 で完全撤去済み。無効化は `tidd config` を使用する。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import is_hook_enabled

_SRC_PATH_LINE_RE = re.compile(r"^_src_path:\s*(\S+)\s*$", re.MULTILINE)
# `gh:owner/repo`（GitHub 短縮形）
_GH_SHORTHAND_RE = re.compile(r"^gh:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")
# `git+https://github.com/owner/repo(.git)` / `https://...`
_HTTP_URL_RE = re.compile(
    r"^(?:git\+)?https://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)
# `git@github.com:owner/repo.git`（SSH）
_SSH_URL_RE = re.compile(
    r"^git@(?P<host>[^:]+):(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)


def _read_answers_commit(cwd: Path) -> str | None:
    """`.copier-answers.yml` の `_commit` フィールドを最小限のパーサで読む."""
    answers = cwd / ".copier-answers.yml"
    if not answers.is_file():
        return None
    try:
        text = answers.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        # `_commit: vYYYY.MM.DD` の形式のみ拾う。YAML の複雑構文は使わない前提。
        if stripped.startswith("_commit:"):
            value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def _resolve_upstream_url(cwd: Path) -> str | None:
    """`.copier-answers.yml` の `_src_path` から上流 URL（`git ls-remote` 用）を返す.

    - `gh:owner/repo` → `https://github.com/owner/repo.git`
    - `git+https://...` / `https://...` → `https://<host>/<owner>/<repo>.git`
    - `git@github.com:owner/repo.git`（SSH）→ `https://github.com/owner/repo.git`
    - ローカルパス（`/tmp/...`・`../...` 等）→ そのまま返す（`git ls-remote` が受け付ける）
    - `.copier-answers.yml` が無い・`_src_path` が無い → None
    """
    answers = cwd / ".copier-answers.yml"
    if not answers.is_file():
        return None
    try:
        text = answers.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _SRC_PATH_LINE_RE.search(text)
    if match is None:
        return None
    src_path = match.group(1).strip()

    gh = _GH_SHORTHAND_RE.match(src_path)
    if gh:
        return f"https://github.com/{gh.group('owner')}/{gh.group('repo')}.git"
    http = _HTTP_URL_RE.match(src_path)
    if http:
        return f"https://{http.group('host')}/{http.group('owner')}/{http.group('repo')}.git"
    ssh = _SSH_URL_RE.match(src_path)
    if ssh:
        return (
            f"https://{ssh.group('host')}/{ssh.group('owner')}/{ssh.group('repo')}.git"
        )
    # ローカルパスは git ls-remote が直接受け付ける（存在しなければ失敗 → None 扱い）
    if src_path.startswith(("/", "./", "../", "~")):
        return src_path
    return None


def _fetch_latest_upstream_tag(cwd: Path) -> str | None:
    """上流の最新タグを取得する.

    テスト用環境変数:
    - `TIDD_COPIER_LATEST_TAG_OVERRIDE`: 指定された値をそのまま返す（空文字なら失敗扱い）

    上流 URL は `TIDD_COPIER_UPSTREAM_URL`（最優先）→ `.copier-answers.yml` の
    `_src_path`（#3683）の順で解決する。解決できない場合・ネットワーク不通・
    git 未導入の場合は None を返す（セッションを止めない）。
    """
    override = os.environ.get("TIDD_COPIER_LATEST_TAG_OVERRIDE")
    if override is not None:
        return override or None

    upstream = os.environ.get("TIDD_COPIER_UPSTREAM_URL")
    if upstream is None:
        upstream = _resolve_upstream_url(cwd)
    if upstream is None:
        return None
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", "--sort=-v:refname", upstream],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        # 出力形式: "<sha>\trefs/tags/<tagname>"
        parts = line.split("refs/tags/")
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip()
    return None


def _read_payload() -> dict[str, object]:
    """SessionStart hook から stdin JSON を読む（読めなくても続行）."""
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def main() -> int:
    # hook 機能別 on/off（Issue #2167）
    if not is_hook_enabled("notify-copier-staleness"):
        return 0

    _read_payload()  # SessionStart hook 仕様に合わせて読むが本 hook では未使用

    cwd = Path.cwd()
    current_commit = _read_answers_commit(cwd)
    if current_commit is None:
        # 該当 consumer ではない（Copier 導入なし）ため黙って終了する
        return 0

    latest_tag = _fetch_latest_upstream_tag(cwd)
    if latest_tag is None:
        # ネットワーク不通・git 未導入等では通知しない（開発を止めない方針）
        return 0

    if current_commit == latest_tag:
        return 0

    sys.stderr.write(
        f"NOTICE: Copier テンプレートが古い可能性があります (current: {current_commit} / latest: {latest_tag})\n"
    )
    sys.stderr.write("NOTICE: `tidd copier-update` を実行して最新化してください。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
