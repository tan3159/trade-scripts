"""`tidd worktree-add` サブコマンド（Issue #3296・#3614・#3618）.

`git worktree add -b <branch> <path> <base>` を実行し、config.json の
`worktree-mise-stub-path` が設定されている場合のみ、新規 worktree ルートに
mise スタブ `.mise.toml`（`[env]` テーブルの `_.source = "<パス>"` の 2 行）を生成する。

- 秘密情報そのものは一切書き込まない（スタブは参照先パスのみ）
- config 未設定時はスタブを生成しない（opt-in・mise 未導入環境への非破壊）
- git worktree は main の兄弟ディレクトリに作られるため、mise の `mise.toml` 探索
  （親ディレクトリ方向のみ）では main の `.mise.toml` が読み込まれない
  （`docs/decisions/2026-08-10-envrc-to-mise-env-migration.md`）。

`_.file`（dotenv 専用パーサ）ではなく `_.source`（参照先を bash として source する mise
の機構）を使う。実機検証（`mise env`）の結果、`_.file` は ANSI-C quoting
（`export FOO=$'...\\n...'`）を含む行のパースに失敗し、失敗した行の内容をそのまま
stderr にエコーする（#3614 の `dotenv_if_exists` と同じ秘密情報漏洩リスク）。`_.source`
は参照先を bash として source するため、この問題が起きない。

旧 direnv 版スタブ（`worktree-envrc-stub-path`・`source_env_if_exists`・#3296/#3614）は
envrc→mise 移行完了に伴い #3676 で削除された。config.json にキーが残っていても無視される。

**ロック証跡検証（#4059）:** `git worktree add` を実行する前に、ブランチ名（一致しなければ
`path`）から `issue-<N>` を抽出できた場合、`tidd_tools.issue_next_lock.verify_lock_evidence()`
でロック証跡ファイルと `refs/locks/issue-<N>` の remote 上の現在値を照合する。分散ロック
（`issue_next_lock` の docstring 参照）は「同名 ref への同時 push は 1 つしか成功しない」
atomic 性のみを排他制御の根拠にしており、`issue-next-state init` の exit code を呼び出し元の
LLM が正しく読んで従うことに実運用上依存してしまう弱点がある。この検証はその最終防衛線であり、
証跡が無い（`init` を経ていない）・remote と一致しない（他プロセスがロックを取り直した）
場合は `git worktree add` を実行せず exit 1 で終了する。Issue 番号を抽出できない場合
（`issue-<N>` を含まないブランチ名・パス）は検証をスキップする（判定不能はブロックしない）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from tidd_tools import issue_next_lock, timing_log
from tidd_tools.shared import app_config
from tidd_tools.shared.branch_ref import resolve_issue_key as _resolve_issue_key


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "worktree-add",
        help="git worktree add を実行し、設定に応じて mise スタブ .mise.toml を生成する（#3618）",
        description=__doc__,
    )
    parser.add_argument("branch", help="新規ブランチ名（例: feat/issue-42-demo）")
    parser.add_argument("path", type=Path, help="worktree の作成先パス")
    parser.add_argument("base", nargs="?", default="origin/main", help="base ref（デフォルト: origin/main）")
    parser.set_defaults(func=run_cli)


def _mise_stub_path_from_config() -> str | None:
    """config.json の `worktree-mise-stub-path` を返す（未設定・空は None）."""
    config = app_config.read_effective_config()
    value = config.get("worktree-mise-stub-path")
    if not value:
        return None
    return str(value)


def _toml_basic_string(value: str) -> str:
    """TOML の basic string（ダブルクォート文字列）としてエスケープする（#3618）.

    Windows パス（バックスラッシュ区切り）を含め、任意のパス文字列を安全に
    `.mise.toml` へ埋め込むための最小限のエスケープ（`\\` → `\\\\`・`"` → `\\"`）。
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _write_mise_stub(worktree_path: Path, stub_path: str) -> None:
    """worktree ルートに mise スタブ `.mise.toml` を書く（#3618）.

    `_.source` を使う（`_.file` ではない）。参照先を bash として source するため、
    本体の秘密情報ファイルの任意の bash 構文（ANSI-C quoting 等）を正しく扱える
    （旧 direnv 版の `source_env_if_exists` と同じ理由・#3614）。
    """
    mise_toml = worktree_path / ".mise.toml"
    mise_toml.parent.mkdir(parents=True, exist_ok=True)
    escaped = _toml_basic_string(stub_path)
    mise_toml.write_text(f'[env]\n_.source = "{escaped}"\n', encoding="utf-8")


def _extract_issue_key(value: object) -> str | None:
    """ブランチ名またはパス文字列から ``issue-<N>`` キーを抽出する（#3518・#3550）.

    一致しない場合は None。
    """
    return _resolve_issue_key(str(value))


def _record_step2(issue_key: str, step: str) -> None:
    """統一日誌へ step2 境界 mark を冪等に記録する（#3518）.

    ``record_event_once_safe`` は fail-open（記録失敗で例外を送出せず警告のみ）かつ
    同一 step の既存レコードがあれば追記しない。exit code を変えないため
    ``try``/``except`` の追加ハンドリングは書かない。
    """
    timing_log.record_event_once_safe(issue_key, step, "point", "worktree-add")


def run_cli(args: argparse.Namespace) -> int:
    """`git worktree add -b <branch> <path> <base>` を実行し、opt-in でスタブを生成する."""
    # Issue 番号は args.branch を優先し、一致しなければ args.path を見る（#3518）。
    # どちらからも取れない場合は記録せず worktree 作成処理は通常どおり続行する。
    issue_key = _extract_issue_key(args.branch)
    if issue_key is None:
        issue_key = _extract_issue_key(args.path)

    if issue_key is not None:
        issue_num = int(issue_key.removeprefix("issue-"))
        if not issue_next_lock.verify_lock_evidence(issue_num):
            print(
                f"ERROR: Issue #{issue_num} のロックが確認できません"
                "（issue-next-state init を経ていない、または他プロセスがロックを取り直しました・#4059）",
                file=sys.stderr,
            )
            return 1
        _record_step2(issue_key, "step2-implementation")

    cmd = ["git", "worktree", "add", "-b", args.branch, str(args.path), args.base]
    try:
        subprocess.run(cmd, check=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"ERROR: git worktree add に失敗しました: {exc}", file=sys.stderr)
        return 1

    # git worktree add 成功時のみ step2-branch-created を記録する（#3518）。
    if issue_key is not None:
        _record_step2(issue_key, "step2-branch-created")

    mise_stub_path = _mise_stub_path_from_config()
    if mise_stub_path:
        try:
            _write_mise_stub(Path(args.path), mise_stub_path)
            print(f"OK: {args.path}/.mise.toml に mise スタブを生成しました（{mise_stub_path} 参照・#3618）")
        except OSError as exc:
            print(f"WARN: .mise.toml スタブの生成に失敗しました: {exc}", file=sys.stderr)
    return 0
