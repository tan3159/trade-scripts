"""`tidd detect-secrets-check` サブコマンド.

Issue #4025: `.pre-commit-config.yaml` に detect-secrets hook が定義されているが、
`pre-commit install` の実行がローカル運用（人力）に依存しており、上流リポジトリ自身が
その運用を満たしていなかった（`.git/hooks/pre-commit` 未生成）。ローカル運用に依存する
対策は再発するため、CI 側の機械強制で塞ぐ。

`.secrets.baseline` を基準に全追跡ファイル（package-lock.json 除く。
`.pre-commit-config.yaml` の detect-secrets hook exclude と揃える）を detect-secrets で
スキャンし、baseline に登録されていない新規の秘密情報を検出したら exit 1 で終了する。
検出された秘密情報の値そのものは出力しない（ファイルパスと行番号のみ・#4025 制約）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tidd_tools.shared import git_client
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GitCommandError
from tidd_tools.shared.subprocess_runner import run as run_subprocess

DEFAULT_BASELINE = ".secrets.baseline"

# `.pre-commit-config.yaml` の detect-secrets hook exclude（`package-lock.json`）と揃える。
_EXCLUDE_SUBSTRING = "package-lock.json"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "detect-secrets-check",
        help="detect-secrets baseline 照合による秘密情報混入検知 (#4025)",
        description=(
            "`.secrets.baseline` を基準に全追跡ファイルを detect-secrets でスキャンし、"
            "baseline に無い新規の秘密情報を検出した場合に exit 1 で終了する。"
            "ローカル pre-commit hook 未導入に依存しない CI 側の機械強制（Issue #4025）。"
        ),
    )
    parser.add_argument(
        "--baseline",
        default=DEFAULT_BASELINE,
        help=f"baseline ファイルの repo root 相対パス（デフォルト: {DEFAULT_BASELINE}）",
    )
    add_common_flags(parser, dry_run_supported=False)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    try:
        repo_root = git_client.rev_parse_show_toplevel()
    except GitCommandError as exc:
        print(f"ERROR: git rev-parse に失敗しました: {exc}", file=sys.stderr)
        return 1

    baseline_path = repo_root / args.baseline
    if not baseline_path.is_file():
        print(
            f"ERROR: baseline ファイルが見つかりません: {baseline_path}"
            "（`detect-secrets scan --baseline .secrets.baseline` で生成してください）",
            file=sys.stderr,
        )
        return 1

    try:
        baseline_data = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: baseline ファイルの読み込みに失敗しました: {baseline_path}: {exc}", file=sys.stderr)
        return 1

    try:
        files = _list_tracked_files(repo_root, baseline_relative_path=args.baseline)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    new_secrets = _scan_for_new_secrets(repo_root, baseline_path, baseline_data, files)

    if new_secrets:
        print("以下のファイルに baseline 未登録の秘密情報の可能性が検出されました:")
        for filename, line_number in new_secrets:
            print(f"  - {filename}:{line_number}")
        print(f"検出件数: {len(new_secrets)}")
        return 1

    print("OK: baseline 未登録の秘密情報は検出されませんでした（検出件数: 0）")
    return 0


def _list_tracked_files(repo_root: Path, *, baseline_relative_path: str) -> list[str]:
    """`git ls-files` の出力から exclude 対象を除いて返す.

    - `.pre-commit-config.yaml` の detect-secrets hook exclude（`package-lock.json`）と揃える
    - baseline ファイル自身は除外する（baseline 内のハッシュ値・行番号がそれ自身の
      スキャン対象になり検出結果に含まれてしまう自己参照を防ぐ）
    """
    result = run_subprocess(["git", "ls-files"], cwd=repo_root)
    if result.returncode != 0:
        raise RuntimeError(f"git ls-files が exit={result.returncode} で失敗しました: {result.stderr}")
    return [
        line
        for line in result.stdout.splitlines()
        if line.strip() and _EXCLUDE_SUBSTRING not in line and line.strip() != baseline_relative_path
    ]


def _scan_for_new_secrets(
    repo_root: Path,
    baseline_path: Path,
    baseline_data: dict[str, object],
    files: list[str],
) -> list[tuple[str, int]]:
    """baseline に無い新規の秘密情報を `(ファイルパス, 行番号)` のリストで返す.

    `SecretsCollection.__sub__`（`detect_secrets.pre_commit_hook.main` が使うのと同じ diff
    ロジック）は `PotentialSecret.filename` **属性**の完全一致で判定する。しかし
    `SecretsCollection(root=str(repo_root))` でスキャンすると、この属性には
    `os.path.join(root, filename)` で結合した絶対パスが入る一方、baseline 側の
    `filename` 属性は JSON に保存された repo-root 相対パスのままであり、常に不一致になって
    baseline 登録済みの秘密情報まで新規扱いされてしまう（実機検証で確認・#4025）。
    そのため独自に `(ファイルパス, secret_hash, type)` のタプル集合で diff を取る
    （辞書のキー側＝`scan_file()` に渡した相対パスは正しく保たれている）。
    baseline ファイルへの書き込み（`should_update_baseline` 相当）は行わない（CI job は read-only）。
    """
    from detect_secrets.core import baseline as detect_secrets_baseline
    from detect_secrets.core.secrets_collection import SecretsCollection

    baseline_collection = detect_secrets_baseline.load(baseline_data, filename=str(baseline_path))
    baseline_keys = {(filename, secret.secret_hash, secret.type) for filename, secret in baseline_collection}

    scanned = SecretsCollection(root=str(repo_root))
    for relative_path in files:
        scanned.scan_file(relative_path)

    new_secrets: list[tuple[str, int]] = []
    for filename, secret in scanned:
        key = (filename, secret.secret_hash, secret.type)
        if key not in baseline_keys:
            new_secrets.append((filename, secret.line_number))
    return new_secrets
