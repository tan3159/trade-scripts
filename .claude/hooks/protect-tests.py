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

Issue #1782: Bash tool の gh pr create コマンドも監視し、
allow-test-update マーカーがあれば bypass audit log を記録する。

stdlib のみ使用。
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.bypass_audit import record_bypass as _record_bypass  # noqa: E402
from _lib.gh_cache import get_pr_body as _get_pr_body_cached  # noqa: E402
from _lib.gh_cache import get_pr_body_stale as _get_pr_body_stale  # noqa: E402
from _lib.hook_io import (  # noqa: E402
    get_command,
    get_file_path,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)
from _lib.override_markers import (  # noqa: E402
    extract_reason,
    find_invalid_syntax,
    has_override_marker,
)

# Issue #1782: gh pr create コマンドを捕捉する
_GH_PR_CREATE_RE = re.compile(r"(?:^|&&|\|\||\||;|\n)[ \t]*gh[ \t]+pr[ \t]+create\b")

# Issue #1293: skip / xfail 検出（新規追加テストで @pytest.mark.skip 等が入るのを防ぐ）
_SKIP_XFAIL_RE = re.compile(
    r"@pytest\.mark\.(skip|xfail)"
    r"|pytest\.(skip|xfail)\s*\("
    r"|pytest\.mark\.skipif"
    r"|pytestmark\s*=\s*pytest\.mark\.(skip|xfail)"
)
_ALLOW_TEST_SKIP_RE = re.compile(r"<!--\s*allow-test-skip:")


def _get_new_content(payload: dict) -> str | None:
    """tool_input から Write / Edit の new content を取り出す."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    for key in ("content", "new_string", "new_str", "replacement"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return None


def _git_toplevel() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _normalize_path(file_path: str, git_root: str | None) -> str:
    """旧 sh のパス正規化ロジックを移植."""
    if not git_root:
        return file_path
    # 相対パスは pwd 起点で絶対パスへ
    if not os.path.isabs(file_path):
        file_path = os.path.join(os.getcwd(), file_path)
    # realpath -m 相当（存在しないパスでも解決）
    try:
        resolved = os.path.realpath(file_path)
    except OSError:
        resolved = file_path
    # GIT_ROOT 配下なら相対パス化
    prefix = git_root.rstrip("/") + "/"
    if resolved.startswith(prefix):
        return resolved[len(prefix) :]
    return resolved


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
            cmd, capture_output=True, text=True, check=False, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _fetch_pr_body_from_gh() -> str:
    """gh subprocess で PR body を取得する（キャッシュ miss 時のフォールバック）."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "body", "--jq", ".body"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _has_allow_marker() -> bool:
    """PR ボディに <!-- allow-test-update: <理由> --> マーカーがあるか確認.

    Issue #1460: `_lib/override_markers.has_override_marker` を使い、空白揺れ・改行含みも吸収する。
    無効書式（コロンなし・理由なし）は bypass しない。

    TTL 有効 cache → gh subprocess → stale cache の順で取得する。
    GitHub API 障害時も stale cache があれば継続動作する。
    """
    body = _get_pr_body_cached()
    if body is None:
        body = _fetch_pr_body_from_gh() or _get_pr_body_stale() or ""
    return has_override_marker(body, "allow-test-update")


def _get_pr_body() -> str:
    """PR ボディを取得（allow-test-skip マーカー判定に使用）.

    TTL 有効 cache → gh subprocess → stale cache の順で取得する。
    """
    body = _get_pr_body_cached()
    if body is not None:
        return body
    return _fetch_pr_body_from_gh() or _get_pr_body_stale() or ""


def _extract_body_from_gh_pr_create(command: str) -> str:
    """gh pr create コマンドから --body / --body-file の値を抽出する.

    Issue #1782: detect-ai-confirm-misuse.py と同じパターンを採用。
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    body = ""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--body", "-b") and i + 1 < len(tokens):
            body = tokens[i + 1]
            i += 2
        elif tok.startswith("--body="):
            body = tok[len("--body=") :]
            i += 1
        elif tok in ("--body-file", "-F") and i + 1 < len(tokens):
            body_file = Path(tokens[i + 1])
            try:
                body = body_file.read_text(encoding="utf-8")
            except OSError:
                pass
            i += 2
        elif tok.startswith("--body-file="):
            body_file = Path(tok[len("--body-file=") :])
            try:
                body = body_file.read_text(encoding="utf-8")
            except OSError:
                pass
            i += 1
        else:
            i += 1
    return body


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


def _check_skip_xfail_addition(file_path: str, new_content: str | None) -> str | None:
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
    # PR ボディに allow-test-skip マーカーがあれば bypass
    if _ALLOW_TEST_SKIP_RE.search(_get_pr_body()):
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
        if command and _GH_PR_CREATE_RE.search(command):
            return _handle_bash_gh_pr_create(command)
        return 0

    if tool_name not in {"Edit", "Write"}:
        return 0

    raw_path = get_file_path(payload)
    if not raw_path:
        return 0

    git_root = _git_toplevel()
    file_path = _normalize_path(raw_path, git_root)

    # regressions/ は対象外（skip/xfail 検出も skip）
    if _REGRESSIONS_RE.search(file_path):
        return 0

    # Issue #1293: skip / xfail の新規追加チェック（tests/ 以下の全 py ファイル）
    # 保護対象パス外でも tests/ 配下なら skip 検出は有効
    if "/tests/" in file_path or file_path.startswith("tests/"):
        skip_error = _check_skip_xfail_addition(raw_path, _get_new_content(payload))
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
        ):
            if not _has_allow_marker():
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
    body_for_check = _get_pr_body()
    invalid_markers = find_invalid_syntax(body_for_check, ["allow-test-update"])
    if invalid_markers:
        sys.stderr.write(
            f"Blocked: override marker の書式が不正です: "
            f"{', '.join(invalid_markers)}\n"
            "正しい書式: <!-- allow-test-update: <理由> -->\n"
        )
        return 2

    # PR ボディの allow-test-update マーカーがあればバイパス
    if _has_allow_marker():
        # Issue #1625: バイパス使用を audit log に記録
        reason = extract_reason(body_for_check, "allow-test-update")
        _record_bypass(event="allow-test-update", reason=reason)
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
