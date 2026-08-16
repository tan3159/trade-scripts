#!/usr/bin/env python3
"""PreToolUse hook: Edit/Write による機能テスト変更をブロックする.

旧 protect-tests.sh を 1:1 で踏襲する（Phase 4 / #1057 で Python 化）。

保護対象:
  - tests/<file>.bats              (bats: scripts/ 系)
  - projects/gas/<project>/tests/  (Jest: GAS)
  - projects/py/<project>/tests/   (pytest: Python)

対象外:
  - tests/regressions/ 配下（バグ再現テスト追加のため）
  - git 管理下にない新規ファイル
  - PR ボディに <!-- allow-test-update: --> マーカーがある場合（バイパス）
  - PR 未作成時: worktree ローカルの .tidd/state/allow-test-update.json（reason 必須・Issue #3484）

Issue #1782: Bash tool の gh pr create コマンドも監視し、
allow-test-update マーカーがあれば bypass audit log を記録する。

Issue #3484: require-preflight-marker.py（pre-flight GREEN マーカー必須で push をブロック）と
本 hook（PR ボディマーカー必須で保護テストの Edit/Write をブロック）が互いを前提にする
循環依存（PR が無いと allow-test-update マーカーを置けず、pre-flight が GREEN にならないと
push できず、push できないと PR を作れない）を解消するため、PR がまだ存在しない場合の
フォールバックとして worktree ローカルのマーカーファイルを追加する。

stdlib のみ使用。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.bypass_audit import record_bypass as _record_bypass
from _lib.gh_cache import get_pr_body as _get_pr_body_cached
from _lib.gh_cache import get_pr_body_stale as _get_pr_body_stale
from _lib.gh_command import extract_pr_body as _extract_body_from_gh_pr_create
from _lib.gh_command import is_gh_pr_create as _is_gh_pr_create
from _lib.git_helpers import git_toplevel as _git_toplevel_shared
from _lib.hook_io import (
    get_command,
    get_file_path,
    get_new_content,
    get_tool_name,
    is_file_edit_tool,
    is_hook_enabled,
    read_hook_input,
    to_repo_relative_posix_path,
)
from _lib.override_markers import (
    extract_reason,
    find_invalid_syntax,
    has_override_marker,
)

# Issue #1293: skip / xfail 検出（新規追加テストで @pytest.mark.skip 等が入るのを防ぐ）
_SKIP_XFAIL_RE = re.compile(
    r"@pytest\.mark\.(skip|xfail)"
    r"|pytest\.(skip|xfail)\s*\("
    r"|pytest\.mark\.skipif"
    r"|pytestmark\s*=\s*pytest\.mark\.(skip|xfail)"
)
_ALLOW_TEST_SKIP_MARKER = "allow-test-skip"


def _get_new_content(payload: dict) -> str | None:
    """Write / Edit / apply_patch の新 content を取り出す（Issue #3221）.

    Claude Code の `content` / `new_string` 等と、Codex の apply_patch
    （`tool_input.command` の patch 文字列から追加行を抽出）を共通ヘルパー
    `hook_io.get_new_content()` に委譲する。
    """
    return get_new_content(payload)


def _git_toplevel(start_dir: str | None = None) -> str | None:
    """Issue #2958: subprocess 実行部は `_lib/git_helpers.py` の `git_toplevel()` に委譲する."""
    return _git_toplevel_shared(cwd=start_dir, timeout=5)


def _resolve_start_dir(payload: dict, raw_path: str) -> str | None:
    """Issue #2454: Edit/Write payload から git rev-parse の探索起点を決定する.

    file_path は worktree 内の絶対パスとして渡されるのが通常のため、
    その親ディレクトリを最優先で使う（プロセス CWD＝メイン checkout に依存しない）。
    相対パスの場合のみ payload の cwd フィールドにフォールバックする。
    """
    if os.path.isabs(raw_path):
        return os.path.dirname(raw_path)
    payload_cwd = payload.get("cwd")
    if isinstance(payload_cwd, str) and payload_cwd:
        return payload_cwd
    return None


def _normalize_path(
    file_path: str, git_root: str | None, base_dir: str | None = None
) -> str:
    """旧 sh のパス正規化ロジックを移植.

    Issue #2454（PR #2476 レビュー指摘）: 相対パスの絶対化基点はプロセス CWD
    （メイン checkout）ではなく、呼び出し元が解決した worktree 起点（base_dir）を使う。

    Issue #3890: Windows ネイティブ環境では ``os.path.realpath()`` がバックスラッシュ
    区切り（``C:\\Users\\...``）を返す一方、``git_root``（``git rev-parse
    --show-toplevel`` の結果）はフォワードスラッシュ（``C:/Users/...``）のため、
    素朴な prefix 一致は必ず失敗する。共通ヘルパー `to_repo_relative_posix_path()`
    で区切り文字を正規化してから prefix 照合する。
    """
    if not git_root:
        return file_path
    # 相対パスは base_dir（worktree 起点）を優先して絶対パスへ
    if not os.path.isabs(file_path):
        file_path = os.path.join(base_dir or os.getcwd(), file_path)
    # realpath -m 相当（存在しないパスでも解決）
    try:
        resolved = os.path.realpath(file_path)
    except OSError:
        resolved = file_path
    # GIT_ROOT 配下なら相対パス化（Issue #3890: 区切り文字を正規化してから照合）
    return to_repo_relative_posix_path(resolved, git_root)


_PROTECTED_RES = [
    re.compile(r"^tests/[^/]+\.bats$"),
    re.compile(r"^projects/gas/[^/]+/tests/"),
    re.compile(r"^projects/py/[^/]+/tests/"),
]
_REGRESSIONS_RE = re.compile(r"(^|/)tests/regressions/")


def _is_tracked(file_path: str, git_root: str | None) -> bool:
    cmd: list[str] = ["git"]
    if git_root:
        cmd.extend(["-C", git_root])
    cmd.extend(["ls-files", "--error-unmatch", "--", file_path])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _fetch_pr_body_from_gh(git_root: str | None = None) -> str:
    """gh subprocess で PR body を取得する（キャッシュ miss 時のフォールバック）.

    Issue #3284: hook プロセスは worktree ではなくメイン checkout の CWD で
    実行されるため、``git_root``（worktree 起点）を明示して `gh pr view` を
    実行する。省略時はプロセス CWD（従来どおり）。
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "body", "--jq", ".body"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
            cwd=git_root,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _local_marker_path(git_root: str | None) -> Path | None:
    """PR 未作成時のフォールバック用ローカルマーカーファイルパスを返す（Issue #3484）.

    保護対象パス（``_PROTECTED_RES``）の外（``.tidd/state/``）に置くため、
    このマーカーファイル自体は protect-tests.py にブロックされない。
    """
    if not git_root:
        return None
    return Path(git_root) / ".tidd" / "state" / "allow-test-update.json"


def _local_marker_reason(git_root: str | None = None) -> str | None:
    """worktree ローカルマーカーファイルから reason を読み取る（Issue #3484）.

    ``{"reason": "<理由>"}`` 形式の JSON を想定する。ファイル不在・JSON パース失敗・
    ``reason`` フィールド欠落・空文字（空白のみ含む）はすべて無効として None を返す。
    """
    path = _local_marker_path(git_root)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    reason = data.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason
    return None


def _has_allow_marker(git_root: str | None = None) -> bool:
    """PR ボディの allow-test-update マーカー、またはローカルマーカーの有無を確認.

    Issue #1460: `_lib/override_markers.has_override_marker` を使い、空白揺れ・改行含みも吸収する。
    無効書式（コロンなし・理由なし）は bypass しない。

    TTL 有効 cache → gh subprocess → stale cache の順で取得する。
    GitHub API 障害時も stale cache があれば継続動作する。
    Issue #3284: ``git_root``（worktree 起点）を各段の gh/git 呼び出しに渡す。

    Issue #3484: PR ボディマーカーが見つからない場合、PR がまだ存在しない状況への
    フォールバックとして worktree ローカルマーカー（`_local_marker_reason()`）も確認する。
    """
    body = _get_pr_body_cached(cwd=git_root)
    if body is None:
        body = (
            _fetch_pr_body_from_gh(git_root) or _get_pr_body_stale(cwd=git_root) or ""
        )
    if has_override_marker(body, "allow-test-update"):
        return True
    return _local_marker_reason(git_root) is not None


def _get_pr_body(git_root: str | None = None) -> str:
    """PR ボディを取得（allow-test-skip マーカー判定に使用）.

    TTL 有効 cache → gh subprocess → stale cache の順で取得する。
    Issue #3284: ``git_root``（worktree 起点）を各段の gh/git 呼び出しに渡す。
    """
    body = _get_pr_body_cached(cwd=git_root)
    if body is not None:
        return body
    return _fetch_pr_body_from_gh(git_root) or _get_pr_body_stale(cwd=git_root) or ""


def _handle_bash_gh_pr_create(command: str) -> int:
    """Bash tool の gh pr create コマンドを処理する.

    Issue #1782: PR ボディに allow-test-update マーカーがあれば bypass audit log を記録する。
    ブロックはしない（exit 0）。
    """
    body = _extract_body_from_gh_pr_create(command)
    if not body:
        return 0
    if has_override_marker(body, "allow-test-update"):
        reason = extract_reason(body, "allow-test-update")
        _record_bypass(event="allow-test-update", reason=reason)
    return 0


def _check_skip_xfail_addition(
    file_path: str, new_content: str | None, git_root: str | None = None
) -> str | None:
    """Issue #1293: skip / xfail の新規追加をブロックする.

    Returns:
        違反があればエラーメッセージ文字列、なければ None。
    """
    if new_content is None:
        return None
    # 新内容の skip / xfail 出現回数を数える
    new_count = len(_SKIP_XFAIL_RE.findall(new_content))
    if new_count == 0:
        return None
    # 既存ファイルの skip / xfail 出現回数を数える。
    # 新内容の数 <= 既存の数なら「純増していない」→ 通過（Edit で削除する等の合法変更を許容）。
    disk_path = Path(file_path)
    if not disk_path.is_absolute():
        disk_path = Path.cwd() / disk_path
    if disk_path.is_file():
        try:
            existing = disk_path.read_text(encoding="utf-8")
            existing_count = len(_SKIP_XFAIL_RE.findall(existing))
            if new_count <= existing_count:
                return None  # 純増していない
        except (OSError, UnicodeDecodeError):
            pass
    # PR ボディに allow-test-skip マーカーがあれば bypass（理由必須・Issue #2954）
    if has_override_marker(_get_pr_body(git_root), _ALLOW_TEST_SKIP_MARKER):
        return None
    return (
        "テストの skip / xfail の新規追加は禁止されています（Issue #1293）。\n"
        "TDD/BDD ワークフローで「テストを skip して実装を通す」抜け穴を塞ぐためです。\n"
        "\n"
        "bypass する場合は PR ボディに以下マーカーを追加してください:\n"
        "  <!-- allow-test-skip: <理由> -->\n"
        "\n"
        "tests/regressions/ 配下の xfail は許容されます（バグ再現テストの意図的な失敗）。\n"
    )


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")  # Issue #1364
    tool_name = get_tool_name(payload)

    # Issue #1782: Bash tool の gh pr create コマンドを捕捉して audit log を記録する
    if tool_name == "Bash":
        command = get_command(payload)
        if command and _is_gh_pr_create(command):
            return _handle_bash_gh_pr_create(command)
        return 0

    if not is_file_edit_tool(tool_name):
        return 0

    raw_path = get_file_path(payload)
    if not raw_path:
        return 0

    # Issue #2454: worktree 内のファイルを検査できるよう探索起点を解決する
    start_dir = _resolve_start_dir(payload, raw_path)
    git_root = _git_toplevel(start_dir) or _git_toplevel()
    file_path = _normalize_path(raw_path, git_root, base_dir=start_dir)

    # regressions/ は対象外（skip/xfail 検出も skip）
    if _REGRESSIONS_RE.search(file_path):
        return 0

    # Issue #1293: skip / xfail の新規追加チェック（tests/ 以下の全 py ファイル）
    # 保護対象パス外でも tests/ 配下なら skip 検出は有効
    if "/tests/" in file_path or file_path.startswith("tests/"):
        skip_error = _check_skip_xfail_addition(
            raw_path, _get_new_content(payload), git_root
        )
        if skip_error:
            sys.stderr.write(f"Blocked: {skip_error}")
            return 2

    # Issue #1360: 保護対象テストの全消し / 実質削除検知（B 層）
    # Write ツールで new_content が空 (or 空白のみ) の場合、テストファイルの削除に等しい。
    # regressions/ 配下は既に上でスキップ済み。保護対象パスのみに適用。
    if tool_name == "Write" and any(rx.search(file_path) for rx in _PROTECTED_RES):
        new_content = _get_new_content(payload)
        # git 管理下（既存ファイル）の削除相当のみ対象
        if (
            new_content is not None
            and not new_content.strip()
            and _is_tracked(file_path, git_root)
            and not _has_allow_marker(git_root)
        ):
            sys.stderr.write(
                f"Blocked: テストファイルの削除・全消しは禁止されています: {file_path}\n"
                "\n"
                "TDD/BDD ワークフローで「テスト自体を消して実装を通す」抜け穴を塞ぐためです（Issue #1360）。\n"
                "\n"
                "テストが不要になった正当な理由がある場合:\n"
                "  1. PR ボディに <!-- allow-test-update: <理由> --> を追加\n"
                "  2. 再度 Write を実行する\n"
                "\n"
                "詳細: docs/reference/hooks.md#protect-testspy\n"
            )
            return 2

    # 保護対象パスでなければスキップ
    if not any(rx.search(file_path) for rx in _PROTECTED_RES):
        return 0

    # git 管理下に無ければスキップ（新規作成は許可）
    if not _is_tracked(file_path, git_root):
        return 0

    # Issue #1460: 無効書式 marker（コロンなし・理由なし）を検出したら exit 2
    body_for_check = _get_pr_body(git_root)
    invalid_markers = find_invalid_syntax(body_for_check, ["allow-test-update"])
    if invalid_markers:
        sys.stderr.write(
            f"Blocked: override marker の書式が不正です: "
            f"{', '.join(invalid_markers)}\n"
            "正しい書式: <!-- allow-test-update: <理由> -->\n"
        )
        return 2

    # PR ボディの allow-test-update マーカーがあればバイパス
    if has_override_marker(body_for_check, "allow-test-update"):
        # Issue #1625: バイパス使用を audit log に記録
        reason = extract_reason(body_for_check, "allow-test-update")
        _record_bypass(event="allow-test-update", reason=reason)
        return 0

    # Issue #3484: PR がまだ存在しない場合のフォールバック（worktree ローカルマーカー）
    local_reason = _local_marker_reason(git_root)
    if local_reason is not None:
        _record_bypass(event="allow-test-update-local", reason=local_reason)
        return 0

    sys.stderr.write(f'Blocked: 機能テストファイル "{file_path}" は変更不可です。\n')
    sys.stderr.write("\n")
    sys.stderr.write(
        "理由: TDD/BDD ワークフローでは実装フェーズでのテスト書き換えを禁止しています。\n"
    )
    sys.stderr.write(
        "     テストを変更すると TDG（Test-Driven Generation）が機能しなくなります。\n"
    )
    sys.stderr.write("\n")
    sys.stderr.write("保護対象:\n")
    sys.stderr.write("  - tests/<file>.bats\n")
    sys.stderr.write("  - projects/gas/<project>/tests/...\n")
    sys.stderr.write("  - projects/py/<project>/tests/...\n")
    sys.stderr.write("\n")
    sys.stderr.write("テストが実体と乖離していて更新が必要な場合:\n")
    sys.stderr.write("  1. PR ボディに以下のマーカーを追加する:\n")
    sys.stderr.write("     <!-- allow-test-update: <更新理由> -->\n")
    sys.stderr.write("  2. 再度 Edit/Write を実行する\n")
    sys.stderr.write("\n")
    sys.stderr.write(
        "PR がまだ存在しない場合（初回 push 前）は、代わりに worktree ルートへ\n"
    )
    sys.stderr.write(
        '  .tidd/state/allow-test-update.json に {"reason": "<更新理由>"} を作成しても bypass できます（Issue #3484）。\n'
    )
    sys.stderr.write("\n")
    sys.stderr.write(
        "バグ修正に伴う再現テストを追加したい場合は tests/regressions/ に追加してください。\n"
    )
    sys.stderr.write("\n")
    sys.stderr.write("詳細: docs/reference/hooks.md#protect-testspy\n")
    return 2


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("protect-tests"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
