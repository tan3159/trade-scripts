"""`tidd copier-update` サブコマンド (#1224).

Copier テンプレート由来のファイルを consumer 側で `copier update` する。
Issue #1220 で導入した GitHub Actions（`copier-update.yml`）を撤去し、
consumer が手動または各自のスケジューラー（systemd timer 等）から呼び出す
統一エントリポイントを提供する。

使い方:
    tidd copier-update              # copier update --defaults --trust を実行
    tidd copier-update --dry-run    # 変更を適用せず diff だけ確認する (copier --pretend)
    tidd copier-update --project-dir path/to/consumer  # 対象ディレクトリを明示指定

終了コード:
- 0 → copier update 成功
- 非ゼロ → copier update 失敗（copier CLI の exit code をそのまま返す）
- 2 → 前提不備（copier CLI 未インストール・`.copier-answers.yml` 不存在等）
- 1 → `.claude/settings.json` の post-update validation 失敗（#3956。競合マーカー残存・
  不正 JSON を検知した場合。snapshot からの復旧を試みた場合も「無言の成功」を避けるため
  非ゼロで終了する）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.subprocess_runner import run as run_subprocess

# #3956: `.claude/settings.json` に残る git 競合マーカーの開始行（copier の
# `conflict="inline"` 出力形式・固定文字列）。
_CONFLICT_MARKER = "<<<<<<< before updating"
_SETTINGS_RELATIVE = Path(".claude/settings.json")
_SETTINGS_SNAPSHOT_RELATIVE = Path(".claude/settings.json.snapshot")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "copier-update",
        help="consumer 側で copier update を実行する（Actions 撤去後の統一エントリポイント）",
        description=__doc__,
    )
    add_common_flags(parser)
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="update 対象の consumer プロジェクト（default: 現在のディレクトリ）",
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    if shutil.which("copier") is None:
        print(
            "[copier-update] copier CLI が見つかりません。`uv tool install copier` などでインストールしてください。",
            file=sys.stderr,
        )
        return 2

    project_dir = (args.project_dir or Path.cwd()).resolve()
    answers = project_dir / ".copier-answers.yml"
    if not answers.is_file():
        print(
            f"[copier-update] {answers} が存在しません。初回は `copier copy` を先に実行してください。",
            file=sys.stderr,
        )
        return 2

    cmd = ["copier", "update", "--defaults", "--trust"]
    if args.dry_run:
        # `copier update --pretend` は working tree を書き換えずに差分だけ表示する。
        # `--dry-run` フラグの意味論を copier 側の `--pretend` に対応付ける。
        cmd.append("--pretend")
    cmd.append(str(project_dir))

    result = run_subprocess(cmd, capture=False)
    if result.returncode != 0:
        print(
            f"[copier-update] copier update に失敗しました (exit {result.returncode})",
            file=sys.stderr,
        )
        return result.returncode
    if args.dry_run:
        # --pretend は working tree を書き換えないため rulesync 再生成は行わない。
        return 0
    # #3711: copier update の diff 適用（repo-specific マーカー内の consumer 追記を
    # 3-way merge で復元する処理）は copier の `_tasks` 実行後に行われる。このため
    # `_tasks` が生成した AGENTS.md は merge 前の overview.md を元にしており、
    # consumer 固有の追記が反映されない場合がある。update 完了後に rulesync generate を
    # 再実行して AGENTS.md を overview.md に追従させる（rulesync 未導入なら WARN のみ）。
    _regenerate_rulesync_agents(project_dir)
    return _validate_settings_json(project_dir)


def _validate_settings_json(project_dir: Path) -> int:
    """`copier update` 完了後に `.claude/settings.json` の健全性を検査する（#3956）.

    `copier.yml` の `_skip_if_exists` で通常は競合マーカーが残ることは無いが、
    将来の regression に備えた最終防衛ラインとして、`copier update` 自体が exit 0 を
    返していても `.claude/settings.json` が壊れていないかを検査する（無言の成功を
    やめる）。競合マーカー残存・不正 JSON を検知したら `.claude/settings.json.snapshot`
    （`merge_settings.py` がマージ成功のたびに保存する直近の正しいマージ結果）からの
    復旧を試みる。復旧の成否に関わらず、異常を検知した時点で非ゼロを返す。
    """
    settings_path = project_dir / _SETTINGS_RELATIVE
    if not settings_path.is_file():
        # settings.json 自体を配布しない consumer 構成（テンプレート未導入等）は対象外。
        return 0

    text = settings_path.read_text(encoding="utf-8")
    marker_count = text.count(_CONFLICT_MARKER)
    if marker_count == 0:
        try:
            json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            snapshot_path = project_dir / _SETTINGS_SNAPSHOT_RELATIVE
            snapshot_path.unlink(missing_ok=True)
            return 0
        print(
            f"[copier-update] {_SETTINGS_RELATIVE} が不正な JSON です。",
            file=sys.stderr,
        )
    else:
        print(
            f"[copier-update] {_SETTINGS_RELATIVE} に競合マーカーが残っています（{marker_count} ブロック）。",
            file=sys.stderr,
        )

    snapshot_path = project_dir / _SETTINGS_SNAPSHOT_RELATIVE
    if snapshot_path.is_file():
        try:
            snapshot_text = snapshot_path.read_text(encoding="utf-8")
            json.loads(snapshot_text)
        except (OSError, json.JSONDecodeError):
            snapshot_text = None
        if snapshot_text is not None:
            settings_path.write_text(snapshot_text, encoding="utf-8")
            snapshot_path.unlink(missing_ok=True)
            print(
                f"[copier-update] MANAGED/PRESERVED マージをやり直しました: {_SETTINGS_RELATIVE}",
                file=sys.stderr,
            )
    return 1


def _regenerate_rulesync_agents(project_dir: Path) -> None:
    """consumer の AGENTS.md を `.rulesync/rules/*.md` から再生成する（#3711）.

    `node_modules/.bin/rulesync` → PATH の `rulesync` → `npx --yes rulesync@<VERSION>`
    の順で解決する。解決できない環境では WARN を出力して終了する（exit 0）。
    """
    import shutil as _shutil
    import subprocess as _subprocess

    local_bin = project_dir / "node_modules" / ".bin" / "rulesync"
    if local_bin.is_file():
        cmd = [str(local_bin), "generate", "--targets", "codexcli"]
    elif _shutil.which("rulesync"):
        cmd = ["rulesync", "generate", "--targets", "codexcli"]
    else:
        npx = _shutil.which("npx") or _shutil.which("npx.cmd")
        if not npx:
            print(
                "[copier-update] WARN: rulesync を解決できません。AGENTS.md を最新化するには"
                " `npm ci` または `npm install -g rulesync@16.7.0` を実行してから"
                " `rulesync generate --targets codexcli` を実行してください（#3711）",
                file=sys.stderr,
            )
            return
        cmd = [npx, "--yes", "rulesync@16.7.0", "generate", "--targets", "codexcli"]
    print(f"[copier-update] {' '.join(cmd)}", file=sys.stderr)
    proc = _subprocess.run(cmd, cwd=project_dir)
    if proc.returncode != 0:
        print(
            f"[copier-update] WARN: rulesync generate が exit {proc.returncode} で失敗しました（#3711）",
            file=sys.stderr,
        )
