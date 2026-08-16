#!/usr/bin/env python3
"""PreToolUse hook: pre-flight マーカー未検証のままの git push を検知する（Issue #2328）.

**背景:** PR #2324（#2313実装）の作業で、pre-flight がフルスイート成功済みのはずの
ブランチで `ai-review` 実行時に `pytest/tidd_tools` commit status が failure→success
と二重投稿される事象が発生した。原因は当該 worktree に `.tidd/state/` ディレクトリ
自体が存在せず、レビュー指摘の修正コミット後に `pre-flight` が一度も再実行されて
いなかったこと（Issue #2311 のマーカーが無いため `ai-review` は毎回フルスイートを
実地実行し、そのうち1回が flake した）。

現行ワークフローは `workflow.md` に「`gh pr create` 前に pre-flight を実行する」と
プロンプトで記載されているのみで、修正コミット後の再実行漏れを機械的に検知する
手段がなかった。本 hook は `git push` 実行前に HEAD SHA に対応する pre-flight
マーカーファイル（`.tidd/state/preflight-pytest-<sha>.json`、Issue #2311）の有無を
機械チェックし、なければ `tidd pre-flight` の実行を促すメッセージ付きで exit 2 で
ブロックする（`require-red-first.py` 等の既存パターンを踏襲）。

**Issue #3405:** ブロックメッセージは handbook ローカルパス（`projects/py/tidd_tools`）を
直書きせず、`_lib/tidd_uvx.py` の `build_uvx_tidd_cmd()`（uvx ゼロインストール実行方式）で
案内コマンドを組み立てる。consumer には当該パスが存在しないため、直書きだと案内どおり
実行しても解決できず詰まる。

stdlib のみ使用。
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.git_helpers import git_toplevel
from _lib.hook_io import (
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
    resolve_target_cwd,
)
from _lib.tidd_uvx import build_uvx_tidd_cmd

DETAIL = "詳細: docs/reference/hooks.md#require-preflight-markerpy\n"

# harness が付与する先頭の `cd <path> &&` prefix（Issue #2317）も含め、
# コマンド区切り（先頭・&&・;・|）直後の `git push` を検知する。
# Issue #2454（PR #2476 レビュー指摘）: `git -C <path> push` も入口判定に一致させる
# （#2443 の ban-claude-p.py と同型）。
_PUSH_RE = re.compile(
    r"(^|&&|;|\|)\s*git\s+(?:-C\s+(?:\"[^\"]+\"|'[^']+'|\S+)\s+)?push(\s|$)"
)


def _repo_root(cwd: str | None = None) -> Path | None:
    """Issue #2958: toplevel 解決は `_lib.git_helpers.git_toplevel()` に委譲する."""
    root = git_toplevel(cwd=cwd, timeout=10)
    if root is None:
        return None
    return Path(root)


def _git_head_sha(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _has_fresh_preflight_marker(repo_root: Path, sha: str) -> bool:
    """指定 SHA に対する pre-flight pytest 成功マーカーが存在するか判定する（Issue #2311 と同一仕様）."""
    if not sha:
        return False
    path = repo_root / ".tidd" / "state" / f"preflight-pytest-{sha}.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(data, dict)
        and data.get("sha") == sha
        and data.get("success") is True
    )


def _preflight_guidance_command() -> str:
    """`tidd pre-flight` の案内コマンド文字列を組み立てる（Issue #3405）.

    handbook ローカルパス（`projects/py/tidd_tools`）を直書きせず、`_lib/tidd_uvx.py`
    経由で uvx ゼロインストール実行方式のコマンドを組み立てる（consumer でも実行可能）。
    表示用に実行バイナリ名は `uvx`（解決済み絶対パスではなく）で統一する。
    """
    uvx_cmd = build_uvx_tidd_cmd("pre-flight")
    if uvx_cmd is None:
        # uvx が PATH に無い環境向けフォールバック（PATH 導入済み tidd 前提）
        return "tidd pre-flight"
    return shlex.join(["uvx", *uvx_cmd[1:]])


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")  # Issue #1364
    if get_tool_name(payload) != "Bash":
        return 0

    command = get_command(payload)
    if not _PUSH_RE.search(command):
        return 0

    # Issue #2454: worktree で push される内容を検査できるよう対象 CWD を解決する
    target_cwd = resolve_target_cwd(payload, command)
    repo_root = _repo_root(target_cwd)
    if repo_root is None:
        return 0  # git 取得不能 → 安全側で skip

    sha = _git_head_sha(repo_root)
    if not sha:
        return 0  # HEAD 取得不能 → 安全側で skip

    if _has_fresh_preflight_marker(repo_root, sha):
        return 0

    sys.stderr.write(
        "BLOCK: pre-flight マーカーが見つかりません（現在の HEAD SHA に対する検証が未実施です）。\n"
        "以下を実行してから git push してください:\n"
        f"  {_preflight_guidance_command()}\n"
    )
    sys.stderr.write(DETAIL)
    return 2


def main() -> int:
    # Issue #1633: hook 機能別 on/off（デフォルト OFF・opt-in）
    if not is_hook_enabled("require-preflight-marker"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
