#!/usr/bin/env python3
"""PreToolUse hook: 個人/組織アカウント識別子（NG ワード）の混入をブロックする.

Issue #2308（関連: #2306）。過去に個人アカウント識別子がドキュメント・commit
history・GitHub Issue/PR 本文へ混入し、大規模な除去対応が発生した。再発防止のため
特定の識別子文字列の混入を機械的に検知してブロックする。

検知対象の識別子文字列自体は個人情報であり、リポジトリにコミットする設定ファイル
（`.claude/rules/` 配下等）に書くこと自体が本末転倒になる。そのため NG ワードリストは
リポジトリにコミットしない gitignore 対象のローカルファイル
（`.claude/ng-words.local.txt`）から読み込む。環境（リポジトリ・マシン）ごとに
異なる識別子を、リポジトリにコミットせずに設定できる。

ローカルファイルが存在しない環境では何もチェックしない（後方互換・未設定環境対応）。

検査対象:
  - git commit のステージング済みファイル内容（パス制限なし・`ban-hardcoded-repo.py`
    と異なり全ファイルが対象）
  - Edit / Write ツールの変更内容（`new_string` / `content`）
  - `gh issue create/edit/comment` / `gh pr create/edit/comment` の本文
    （`--body` / `--body-file` / heredoc パターン）

stdlib のみ使用。
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import get_command, get_tool_name, is_hook_enabled, read_hook_input  # noqa: E402

# リポジトリにコミットしない（gitignore 対象の）ローカル NG ワードファイル
_NG_WORDS_PATH_REL = ".claude/ng-words.local.txt"

_GIT_COMMIT_RE = re.compile(r"(^|&&|;|\|)\s*git commit(\s|$)")
_GH_COMMAND_RE = re.compile(
    r"(?:^|&&|\|\||\||;|\n)[ \t]*gh[ \t]+(?:issue|pr)[ \t]+(?:create|edit|comment)\b"
)

# --body "$(cat <<'EOF'\n...\nEOF\n)" heredoc パターン（validate-issue.py と同型・#1578）
_HEREDOC_BODY_RE = re.compile(
    r"""(?:--body|-b)\s+"?\$\(cat\s+<<['"]{0,1}(\w+)['"]{0,1}\s*\n(.*?)\n\1\s*\n?\s*\)"?""",
    re.DOTALL,
)


def _git(*args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=False, timeout=20
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout


def _load_ng_words(git_root: str) -> list[str]:
    """`.claude/ng-words.local.txt` から NG ワードリストを読み込む.

    1 行 1 単語。`#` から始まる行・空行はスキップする。
    ファイルが存在しない環境では空リストを返す（後方互換・未設定環境対応）。
    """
    path = Path(git_root) / _NG_WORDS_PATH_REL
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    words: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        words.append(stripped)
    return words


def _find_ng_word(text: str, words: list[str]) -> str | None:
    """text に含まれる最初の NG ワードを返す（なければ None）."""
    for word in words:
        if word in text:
            return word
    return None


def _blocked_message(location: str, word: str) -> str:
    return (
        f"Blocked: {location} に NG ワード '{word}' が含まれています。\n"
        "個人/組織アカウント識別子の混入はリポジトリの誤混入事故（Issue #2306）の"
        "再発を防ぐためブロックされています。\n"
        "誤検知の場合は .claude/ng-words.local.txt の該当エントリを見直してください。\n"
        "詳細: Issue #2308 / docs/reference/hooks.md#ban-ng-wordspy\n"
    )


def _extract_gh_body(command: str) -> str:
    """gh issue/pr create/edit/comment コマンドから本文を抽出する.

    `--body` / `-b`、`--body-file` / `-F`、heredoc（`--body "$(cat <<EOF ... EOF)"`）
    の各パターンに対応する。
    """
    heredoc_body = _HEREDOC_BODY_RE.search(command)
    if heredoc_body:
        return heredoc_body.group(2)

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


def _check_edit_write_tool(payload: dict, tool_name: str, words: list[str]) -> int:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    file_path = str(tool_input.get("file_path") or "")
    if not file_path:
        return 0

    if tool_name == "Edit":
        text = str(tool_input.get("new_string") or "")
    else:  # Write
        text = str(tool_input.get("content") or "")
    if not text:
        return 0

    word = _find_ng_word(text, words)
    if word is None:
        return 0

    sys.stderr.write(_blocked_message(file_path, word))
    return 2


def _check_git_commit(command: str, git_root: str, words: list[str]) -> int:
    rc, staged_out = _git(
        "-C", git_root, "diff", "--cached", "--name-only", "--diff-filter=AM"
    )
    if rc != 0:
        return 0
    staged_files = [line for line in staged_out.splitlines() if line]
    if not staged_files:
        return 0

    for file in staged_files:
        rc, blob = _git("-C", git_root, "show", f":{file}")
        if rc != 0:
            continue
        word = _find_ng_word(blob, words)
        if word is not None:
            sys.stderr.write(_blocked_message(file, word))
            return 2
    return 0


def _check_gh_command(command: str, words: list[str]) -> int:
    if not _GH_COMMAND_RE.search(command):
        return 0
    body = _extract_gh_body(command)
    if not body:
        return 0
    word = _find_ng_word(body, words)
    if word is None:
        return 0
    sys.stderr.write(_blocked_message("gh コマンドの本文", word))
    return 2


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)

    rc, root_out = _git("rev-parse", "--show-toplevel")
    git_root = root_out.strip() if rc == 0 else ""
    if not git_root:
        return 0

    words = _load_ng_words(git_root)
    if not words:
        # ローカルファイルが存在しない環境では何もチェックしない
        return 0

    if tool_name in ("Edit", "Write"):
        return _check_edit_write_tool(payload, tool_name, words)

    if tool_name != "Bash":
        return 0

    command = get_command(payload)
    if not command:
        return 0

    if _GIT_COMMIT_RE.search(command):
        rc2 = _check_git_commit(command, git_root, words)
        if rc2:
            return rc2

    return _check_gh_command(command, words)


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("ban-ng-words"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
