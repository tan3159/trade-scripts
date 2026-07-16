#!/usr/bin/env python3
"""SessionStart hook: Copier テンプレートが古いことを通知する (#1224).

consumer リポジトリの `.copier-answers.yml` に記録された `_commit` と、
上流 ai-dev-handbook の最新タグを比較して、drift があれば
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
- `TIDD_COPIER_OFFLINE=1`: ネットワーク問い合わせを行わずに即座に終了する。
- `TIDD_COPIER_UPSTREAM_URL`: 上流リポジトリ URL の上書き（default: ai-dev-handbook）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import is_hook_enabled  # noqa: E402

DEFAULT_UPSTREAM = "https://github.com/being-gaia-plan/ai-dev-handbook.git"


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


def _fetch_latest_upstream_tag() -> str | None:
    """上流の最新タグを取得する.

    テスト・オフライン運用のため以下の環境変数で挙動を制御する:
    - `TIDD_COPIER_LATEST_TAG_OVERRIDE`: 指定された値をそのまま返す（空文字なら失敗扱い）
    - `TIDD_COPIER_OFFLINE=1`: 何もせず None を返す
    """
    override = os.environ.get("TIDD_COPIER_LATEST_TAG_OVERRIDE")
    if override is not None:
        return override or None
    if os.environ.get("TIDD_COPIER_OFFLINE") == "1":
        return None

    upstream = os.environ.get("TIDD_COPIER_UPSTREAM_URL", DEFAULT_UPSTREAM)
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", "--sort=-v:refname", upstream],
            capture_output=True,
            text=True,
            timeout=10,
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

    latest_tag = _fetch_latest_upstream_tag()
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
