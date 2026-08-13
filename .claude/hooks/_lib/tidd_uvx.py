"""uvx 経由で `tidd` を解決するための共通ヘルパー（Issue #3087）.

consumer 環境ではマシンごとの永続インストール状態（`uv tool install`）を持たず、

    uvx --from "git+https://github.com/<owner>/<repo>@<ref>#subdirectory=projects/py/tidd_tools" tidd <subcommand>

で tidd_tools を解決して実行する（uvx ゼロインストール実行方式）。
これにより「古いスナップショットの tidd が再インストールされず残り続ける」問題を解消する。

Issue #3683: 上流 URL は `.copier-answers.yml` の `_src_path` から解決する
（consumer に残る上流固有文字列を `_src_path` の 1 箇所に局所化する）。
`_src_path` がローカルパス等で URL に解決できない場合・`.copier-answers.yml` が無い場合は
`TIDD_UVX_SPEC` 環境変数での指定を要求し、未指定なら no-op で stderr に案内を出す。

環境変数 `TIDD_UVX_SPEC` で upstream の git spec を上書きできる
（`notify-copier-staleness.py` の `TIDD_COPIER_UPSTREAM_URL` と同じ考え方。テスト・フォーク用）。

Issue #3415: 配布テンプレート（rules・hooks）は copier タグで固定される一方、
`@main` は常に main ブランチ最新の tidd_tools を取得するため、consumer では
「CLI は新機能を持つのに rules が古い」という版ずれが生じる。`resolve_uvx_spec()` は
repo root の `.copier-answers.yml` の `_commit:` 値（copier タグ）を ref として使い、
テンプレートと CLI の版を一致させる（`_commit` 行が無い・値が空の場合は `@main` を使う）。

stdlib のみ使用（hook 起動オーバヘッド最小化のため）。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

_COMMIT_LINE_RE = re.compile(r"^_commit:\s*(\S+)\s*$", re.MULTILINE)
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


def _resolve_copier_ref(repo_root: Path) -> str:
    """`.copier-answers.yml` の `_commit:` 値を ref として返す.

    ファイルが存在しない・`_commit` 行が無い・値が空の場合は "main" を返す。
    """
    answers_path = repo_root / ".copier-answers.yml"
    if not answers_path.is_file():
        return "main"
    try:
        text = answers_path.read_text(encoding="utf-8")
    except OSError:
        return "main"
    match = _COMMIT_LINE_RE.search(text)
    if match is None:
        return "main"
    ref = match.group(1).strip()
    return ref or "main"


def _resolve_src_path_url(repo_root: Path) -> str | None:
    """`.copier-answers.yml` の `_src_path` から上流リポジトリの git spec URL を返す.

    解決できない場合（`.copier-answers.yml` が無い・`_src_path` がローカルパス等）は None。

    対応フォーマット:
    - ``gh:owner/repo``（GitHub 短縮形）→ ``git+https://github.com/owner/repo``
    - ``git+https://github.com/owner/repo(.git)`` / ``https://github.com/owner/repo`` → ``git+https``
    - ``git@host:owner/repo(.git)``（SSH）→ ``git+ssh://git@host/owner/repo``
      （host は `~/.ssh/config` エイリアスもそのまま使える・Issue #3686）
    """
    answers_path = repo_root / ".copier-answers.yml"
    if not answers_path.is_file():
        return None
    try:
        text = answers_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _SRC_PATH_LINE_RE.search(text)
    if match is None:
        return None
    src_path = match.group(1).strip()

    gh = _GH_SHORTHAND_RE.match(src_path)
    if gh:
        return f"git+https://github.com/{gh.group('owner')}/{gh.group('repo')}"
    http = _HTTP_URL_RE.match(src_path)
    if http:
        return f"git+https://{http.group('host')}/{http.group('owner')}/{http.group('repo')}"
    ssh = _SSH_URL_RE.match(src_path)
    if ssh:
        return f"git+ssh://git@{ssh.group('host')}/{ssh.group('owner')}/{ssh.group('repo')}"
    # ローカルパス等は URL に解決できない
    return None


def resolve_uvx_spec(repo_root: Path | None = None) -> str | None:
    """copier タグに固定した git spec（`uvx --from` 用）を組み立てる（Issue #3415・#3683）.

    Args:
        repo_root: `.copier-answers.yml` を探す repo root。省略時はこのファイルの
            配置場所（`<repo_root>/.claude/hooks/_lib/tidd_uvx.py`）から自動解決する。

    Returns:
        `_src_path` から解決した URL と `_commit` タグ（無ければ `@main`）を組み合わせた
        git spec。`_src_path` を URL に解決できない場合は None。
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[3]
    url = _resolve_src_path_url(repo_root)
    if url is None:
        return None
    ref = _resolve_copier_ref(repo_root)
    return f"{url}@{ref}#subdirectory=projects/py/tidd_tools"


def build_uvx_tidd_cmd(*args: str, repo_root: Path | None = None) -> list[str] | None:
    """`uvx --from <spec> tidd <args>` のコマンド列を返す.

    `uvx` が PATH 上に見つからない場合は None を返す（呼び出し側は skip する）。

    上流 URL を解決できず `TIDD_UVX_SPEC` も未設定の場合は、stderr に解決方法を
    案内して None を返す（hook は no-op で exit 0 する・Issue #3683）。

    Args:
        *args: `tidd` に渡すサブコマンド・オプション（例: "health-check"）。
        repo_root: `.copier-answers.yml` を探す repo root（テスト用）。

    Returns:
        `subprocess` に渡せるコマンド列。`uvx` が無い・URL 解決不可なら None。
    """
    uvx_bin = shutil.which("uvx")
    if uvx_bin is None:
        return None
    spec = os.environ.get("TIDD_UVX_SPEC")
    if spec is None:
        spec = resolve_uvx_spec(repo_root)
        if spec is None:
            sys.stderr.write(
                "tidd_uvx: `.copier-answers.yml` の `_src_path` から上流 URL を解決できません。"
                "`TIDD_UVX_SPEC` 環境変数で git spec を指定してください"
                "（例: `TIDD_UVX_SPEC='git+https://github.com/<owner>/<repo>@main"
                "#subdirectory=projects/py/tidd_tools'`）。\n"
            )
            return None
    return [uvx_bin, "--from", spec, "tidd", *args]
