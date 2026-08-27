"""`tidd consumer-init` サブコマンド（Issue #2252）.

新規 consumer リポジトリの立ち上げ（`gh repo create` → `copier copy` →
初期 commit/push → `repo-bootstrap` → `health-check`）を 1 コマンドで
冪等にオーケストレーションする。`docs/setup/copier-workflow-adoption.md`
§9「新規 consumer 立ち上げチェックリスト」の手順 1・2・4・7・9 相当を自動化する
（手順 3〔tidd_tools インストール〕・手順 8〔`.envrc` 実値設定〕・
手順 10〔スモークテスト〕は Issue の `## やること` スコープ外のため対象外）。

冪等性の設計方針: ローカルの進捗マーカーファイルではなく、
**実際の外部状態（GitHub API・ローカルファイルの有無）を都度問い合わせて判定する**。
`repo_bootstrap` と同じ方針（Issue #2219）を踏襲し、ローカル状態ファイルと
実際の外部状態が乖離するリスクを避ける。

各ステップの判定:
- `gh repo create`: `gh repo view <repo>` が成功すれば既に存在するとみなしスキップする
- `copier copy`: `<dest>/.copier-answers.yml` が存在すれば展開済みとみなしスキップする
- 初期 commit/push: `gh api repos/<repo>/commits` が 1 件以上返せば push 済みとみなしスキップする
- `repo-bootstrap` / `health-check`: 元々冪等 / 副作用なしのため常に実行する
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from tidd_tools import health_check
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import SubprocessTimeoutError
from tidd_tools.shared.subprocess_runner import run as run_subprocess

TEMPLATE_REPO_URL = "https://github.com/being-gaia-plan/ai-dev-handbook.git"

# Issue #2298: `git ls-remote` はネットワーク I/O のため、CI コンテナ等でネットワークが
# 不調な場合に無期限ブロックしうる。有限のタイムアウトで打ち切り、失敗時は「取得できな
# ければ None」の既存方針（vcs_ref は "main" にフォールバック）に委ねる。
_LATEST_STABLE_TAG_TIMEOUT_SECONDS = 15.0

# Issue #2300: `--vcs-ref` を渡し忘れたテストが実 GitHub へネットワークアクセスして
# flaky / timeout を起こすリスクを構造的に排除する。設定されていれば `git ls-remote`
# を一切呼ばずこの値（空文字列なら「タグ未検出」= None）を返す
# （`extract_feature.py::_fetch_issue_body` の `EXTRACT_FEATURE_TEST_BODY` と同じ
# テスト専用環境変数 override パターン。subprocess 起動する BDD テストからもモック可能）。
_TEST_LATEST_STABLE_TAG_ENV = "CONSUMER_INIT_TEST_LATEST_STABLE_TAG"

_TAG_RE = re.compile(r"^v(\d{4})\.(\d{2})\.(\d{2})(?:\.(\d+))?$")


def _tag_sort_key(tag: str) -> tuple[int, int, int, int]:
    """`v<YYYY.MM.DD>[.<N>]` 形式のタグを比較可能なキーへ変換する（不一致時は最小値）."""
    match = _TAG_RE.match(tag)
    if not match:
        return (0, 0, 0, 0)
    year, month, day, suffix = match.groups()
    return (int(year), int(month), int(day), int(suffix) if suffix else 0)


def _latest_stable_tag(template_repo_url: str = TEMPLATE_REPO_URL) -> str | None:
    """ai-dev-handbook の最新安定タグ（`v<YYYY.MM.DD>[.<N>]`）を検出する。取得できなければ None.

    テスト用に ``CONSUMER_INIT_TEST_LATEST_STABLE_TAG`` 環境変数で override 可能
    （Issue #2300: `git ls-remote` による実ネットワークアクセスをテストから排除するため）。
    """
    override = os.environ.get(_TEST_LATEST_STABLE_TAG_ENV)
    if override is not None:
        return override or None
    try:
        result = run_subprocess(
            ["git", "ls-remote", "--tags", "--refs", template_repo_url],
            timeout=_LATEST_STABLE_TAG_TIMEOUT_SECONDS,
        )
    except SubprocessTimeoutError:
        return None
    if result.returncode != 0:
        return None
    tags: list[str] = []
    for line in (result.stdout or "").splitlines():
        if "refs/tags/" not in line:
            continue
        tag = line.rsplit("refs/tags/", 1)[-1].strip()
        if _TAG_RE.match(tag):
            tags.append(tag)
    if not tags:
        return None
    return sorted(tags, key=_tag_sort_key)[-1]


def _gh_authenticated() -> bool:
    result = run_subprocess(["gh", "auth", "status"])
    return result.returncode == 0


def _repo_exists(repo: str) -> bool:
    result = run_subprocess(["gh", "repo", "view", repo])
    return result.returncode == 0


def _repo_has_commits(repo: str) -> bool:
    """対象リポジトリに 1 件以上 commit が push 済みかどうかを返す（空リポジトリは 409 で判定）."""
    result = run_subprocess(["gh", "api", f"repos/{repo}/commits", "--jq", "length"])
    if result.returncode != 0:
        return False
    try:
        return int((result.stdout or "0").strip() or "0") > 0
    except ValueError:
        return False


def _resolve_remote_url(repo: str) -> str:
    """push 先の remote URL を解決する（SSH 優先。#2220 の OAuth App workflow file 制限回避のため）."""
    result = run_subprocess(["gh", "repo", "view", repo, "--json", "sshUrl", "-q", ".sshUrl"])
    if result.returncode == 0 and (result.stdout or "").strip():
        return result.stdout.strip()
    return f"https://github.com/{repo}.git"


def _build_data_args(repo: str, extra_data: list[str]) -> list[str]:
    """`copier copy --data` 引数一式を組み立てる（`project_name`/`github_org` はリポジトリ名から自動導出）."""
    owner, _, name = repo.partition("/")
    supplied_keys = {kv.split("=", 1)[0] for kv in extra_data if "=" in kv}
    args: list[str] = []
    if "project_name" not in supplied_keys:
        args += ["--data", f"project_name={name}"]
    if "github_org" not in supplied_keys:
        args += ["--data", f"github_org={owner}"]
    for kv in extra_data:
        args += ["--data", kv]
    return args


def _step_repo_create(repo: str, visibility: str, *, dry_run: bool) -> tuple[int, str | None]:
    """`gh repo create` ステップ. 戻り値は (exit_code, 説明メッセージ)."""
    if _repo_exists(repo):
        return 0, f"リポジトリ {repo} は既に存在します（スキップ）。"
    if dry_run:
        return 0, f"[dry-run] gh repo create {repo} --{visibility} を実行します。"
    result = run_subprocess(["gh", "repo", "create", repo, f"--{visibility}"])
    if result.returncode != 0:
        return 1, f"エラー: リポジトリ作成に失敗しました: {(result.stderr or '').strip()}"
    return 0, f"リポジトリ {repo} を作成しました。"


def _ensure_git_identity(dest: Path, *, env: Mapping[str, str] | None = None) -> None:
    """git commit に必要な user.name / user.email が未設定ならリポジトリローカルに fallback 値を設定する.

    Issue #2272: CircleCI 等のクリーンな CI コンテナには git のグローバル identity が
    設定されておらず、`git commit` が `Author identity unknown` で失敗する。
    local/global/system いずれかで既に設定済みの identity は上書きしない。
    """
    email_result = run_subprocess(["git", "config", "user.email"], cwd=dest, env=env)
    if not (email_result.stdout or "").strip():
        run_subprocess(["git", "config", "user.email", "consumer-init@tidd.local"], cwd=dest, env=env)
    name_result = run_subprocess(["git", "config", "user.name"], cwd=dest, env=env)
    if not (name_result.stdout or "").strip():
        run_subprocess(["git", "config", "user.name", "tidd consumer-init"], cwd=dest, env=env)


def _step_copier_copy(
    repo: str,
    dest: Path,
    vcs_ref: str,
    extra_data: list[str],
    *,
    dry_run: bool,
) -> tuple[int, str | None]:
    """`copier copy` ステップ. 戻り値は (exit_code, 説明メッセージ)."""
    answers_path = dest / ".copier-answers.yml"
    if answers_path.is_file():
        return 0, f"{dest} は既に copier copy 展開済みです（スキップ）。"
    if dry_run:
        return 0, f"[dry-run] copier copy（vcs-ref={vcs_ref}）を {dest} に展開します。"
    if shutil.which("copier") is None:
        return 2, "エラー: copier CLI が見つかりません。`uv tool install copier` でインストールしてください。"

    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").is_dir():
        init_result = run_subprocess(["git", "init"], cwd=dest)
        if init_result.returncode != 0:
            return 1, f"エラー: git init に失敗しました: {(init_result.stderr or '').strip()}"
        _ensure_git_identity(dest)
        run_subprocess(["git", "commit", "--allow-empty", "-m", "init"], cwd=dest)

    data_args = _build_data_args(repo, extra_data)
    template_ref = f"git+{TEMPLATE_REPO_URL}@{vcs_ref}"
    cmd = ["copier", "copy", "--defaults", "--trust", *data_args, template_ref, str(dest)]
    result = run_subprocess(cmd)
    if result.returncode != 0:
        return result.returncode, f"エラー: copier copy に失敗しました: {(result.stderr or '').strip()}"
    return 0, f"copier copy を実行しました（vcs-ref={vcs_ref}）。"


def _step_initial_push(repo: str, dest: Path, *, dry_run: bool) -> tuple[int, str | None]:
    """初期 commit/push ステップ. 戻り値は (exit_code, 説明メッセージ)."""
    if _repo_has_commits(repo):
        return 0, f"{repo} には既に commit が push 済みです（スキップ）。"
    if dry_run:
        return 0, f"[dry-run] 初期コミットを {repo} に push します。"

    run_subprocess(["git", "add", "-A"], cwd=dest)
    commit_result = run_subprocess(["git", "commit", "-m", "chore: initial commit (tidd consumer-init)"], cwd=dest)
    combined_output = (commit_result.stdout or "") + (commit_result.stderr or "")
    if commit_result.returncode != 0 and "nothing to commit" not in combined_output:
        return 1, f"エラー: コミットに失敗しました: {(commit_result.stderr or '').strip()}"

    remote_url = _resolve_remote_url(repo)
    if run_subprocess(["git", "remote", "get-url", "origin"], cwd=dest).returncode == 0:
        run_subprocess(["git", "remote", "set-url", "origin", remote_url], cwd=dest)
    else:
        run_subprocess(["git", "remote", "add", "origin", remote_url], cwd=dest)

    run_subprocess(["git", "branch", "-M", "main"], cwd=dest)
    push_result = run_subprocess(["git", "push", "-u", "origin", "main"], cwd=dest)
    if push_result.returncode != 0:
        return push_result.returncode, f"エラー: push に失敗しました: {(push_result.stderr or '').strip()}"
    return 0, f"初期コミットを {repo} に push しました。"


def _step_repo_bootstrap(repo: str, dest: Path, *, dry_run: bool) -> tuple[int, str | None]:
    """`repo-bootstrap` ステップ（元々冪等なため常に実行する）."""
    if dry_run:
        return 0, "[dry-run] repo-bootstrap（ラベル・merge 設定・branch 保護）を適用します。"
    cmd = [sys.executable, "-m", "tidd_tools", "repo-bootstrap", "--repo", repo]
    result = run_subprocess(cmd, cwd=dest)
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        return result.returncode, f"エラー: repo-bootstrap に失敗しました: {(result.stderr or '').strip()}"
    return 0, output or "repo-bootstrap を適用しました。"


def _step_health_check(dest: Path, *, dry_run: bool) -> tuple[int, str | None]:
    """`health-check` ステップ（副作用なしのため常に実行する）."""
    if dry_run:
        return 0, "[dry-run] health-check を実行します。"
    namespace = argparse.Namespace(repo_root=dest, verbose=0, dry_run=False, json_output=False)
    exit_code = health_check.run_cli(namespace)
    return exit_code, None


def _emit(exit_code: int, message: str | None) -> None:
    """ステップの結果メッセージを exit_code に応じて stdout/stderr へ出力する."""
    if message:
        stream = sys.stderr if exit_code != 0 else sys.stdout
        stream.write(message + "\n")


def run_cli(args: argparse.Namespace) -> int:
    repo: str = args.repo
    visibility = "public" if getattr(args, "public", False) else "private"
    dry_run: bool = getattr(args, "dry_run", False)
    extra_data: list[str] = list(getattr(args, "data", None) or [])

    if not _gh_authenticated():
        sys.stderr.write("エラー: gh 認証が無効です。`gh auth login` を実行してください。\n")
        return 1

    dest: Path = getattr(args, "dest", None) or (Path.cwd() / repo.split("/")[-1])
    dest = Path(dest).resolve()

    vcs_ref: str | None = getattr(args, "vcs_ref", None)
    if vcs_ref is None:
        vcs_ref = _latest_stable_tag() or "main"

    exit_code, message = _step_repo_create(repo, visibility, dry_run=dry_run)
    _emit(exit_code, message)
    if exit_code != 0:
        return exit_code

    exit_code, message = _step_copier_copy(repo, dest, vcs_ref, extra_data, dry_run=dry_run)
    _emit(exit_code, message)
    if exit_code != 0:
        return exit_code

    exit_code, message = _step_initial_push(repo, dest, dry_run=dry_run)
    _emit(exit_code, message)
    if exit_code != 0:
        return exit_code

    exit_code, message = _step_repo_bootstrap(repo, dest, dry_run=dry_run)
    _emit(exit_code, message)
    if exit_code != 0:
        return exit_code

    exit_code, message = _step_health_check(dest, dry_run=dry_run)
    _emit(exit_code, message)
    if exit_code != 0:
        return exit_code

    if dry_run:
        print("[dry-run] consumer-init のプレビューが完了しました（外部可視操作は行っていません）。")
    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "consumer-init",
        help=(
            "新規 consumer リポジトリを 1 コマンドで立ち上げる"
            "（repo 作成 → copier copy → push → repo-bootstrap → health-check）"
        ),
        description=__doc__,
    )
    add_common_flags(parser)
    parser.add_argument("repo", metavar="OWNER/REPO", help="作成する GitHub リポジトリ（例: acme-corp/my-app）")
    visibility_group = parser.add_mutually_exclusive_group(required=True)
    visibility_group.add_argument("--public", action="store_true", help="public リポジトリとして作成する")
    visibility_group.add_argument("--private", action="store_true", help="private リポジトリとして作成する")
    parser.add_argument(
        "--vcs-ref",
        default=None,
        help="copier テンプレートの参照タグ（省略時は最新安定タグを自動検出。取得不能なら main）",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="copier copy の展開先ディレクトリ（省略時はカレントディレクトリ直下の <repo 名>）",
    )
    parser.add_argument(
        "--data",
        action="append",
        metavar="KEY=VALUE",
        help="copier copy に渡す追加データ（複数指定可。project_name/github_org は省略時にリポジトリ名から自動導出）",
    )
    parser.set_defaults(func=run_cli)


if __name__ == "__main__":
    sys.exit(
        run_cli(
            argparse.Namespace(
                repo="owner/repo",
                public=False,
                private=True,
                vcs_ref=None,
                dest=None,
                data=None,
                verbose=0,
                dry_run=False,
                json_output=False,
            ),
        )
    )
