#!/usr/bin/env python3
"""PreToolUse hook: 個人/組織アカウント識別子（NG ワード）の混入をブロックする.

Issue #2308（関連: #2306）。過去に個人アカウント識別子がドキュメント・commit
history・GitHub Issue/PR 本文へ混入し、大規模な除去対応が発生した。再発防止のため
特定の識別子文字列の混入を機械的に検知してブロックする。

Issue #2418: マシンローカルの中央設定ファイル（`~/.config/tidd_tools/ng-words.toml`）
を新設し、リポジトリパターンと NG ワード・除外パスを対で定義できるようにした。
中央設定が存在する場合は per-repo ローカルファイルより優先される（ただし両方読み込む）。

NG ワードリストは以下の 2 経路から収集する:
1. マシンローカル中央設定（`~/.config/tidd_tools/ng-words.toml`）—— リポジトリパターンで
   フィルタし、マッチしたルールの words + exclude_paths を適用する（#2418）
2. リポジトリ固有のローカルファイル（`.claude/ng-words.local.txt`）—— gitignore 対象。
   後方互換として中央設定が存在しない環境でも継続動作する。

ローカルファイルが存在しない環境では何もチェックしない（後方互換・未設定環境対応）。

検査対象:
  - git commit のステージング済みファイル内容（パス制限なし・`ban-hardcoded-repo.py`
    と異なり全ファイルが対象）。exclude_paths 指定があるファイルはスキップ。
  - Edit / Write / apply_patch ツールの変更内容（`new_string` / `content` /
    patch の追加行）。exclude_paths 対象外パスへの変更はスキップ。
  - `gh issue create/edit/comment` / `gh pr create/edit/comment` の本文
    （`--body` / `--body-file` / heredoc パターン）。パスの概念がないため
    exclude_paths の対象外（常に全ワードで検査）。

stdlib のみ使用。
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import-not-found,no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.git_helpers import git_toplevel
from _lib.git_helpers import run_git as _run_git
from _lib.hook_io import (
    get_command,
    get_file_path,
    get_new_content,
    get_tool_name,
    is_file_edit_tool,
    is_hook_enabled,
    read_hook_input,
    resolve_target_cwd,
)
from _lib.shell_parse import find_heredoc_body as _find_heredoc_body

# リポジトリにコミットしない（gitignore 対象の）ローカル NG ワードファイル（後方互換）
_NG_WORDS_PATH_REL = ".claude/ng-words.local.txt"

# マシンローカル中央設定（環境変数 TIDD_NG_WORDS_CONFIG で上書き可能・テスト用）
_DEFAULT_CENTRAL_CONFIG = Path.home() / ".config" / "tidd_tools" / "ng-words.toml"

# Issue #2454（PR #2476 レビュー指摘）: worktree コミットで多用される
# `git -C <path> commit` も入口判定に一致させる（#2443 の ban-claude-p.py と同型）。
_GIT_COMMIT_RE = re.compile(
    r"(^|&&|;|\|)\s*git\s+(?:-C\s+(?:\"[^\"]+\"|'[^']+'|\S+)\s+)?commit(\s|$)"
)
_GH_COMMAND_RE = re.compile(
    r"(?:^|&&|\|\||\||;|\n)[ \t]*gh[ \t]+(?:issue|pr)[ \t]+(?:create|edit|comment)\b"
)

# --body "$(cat <<'EOF'\n...\nEOF\n)" heredoc パターン（validate-issue.py と同型・#1578）
# heredoc 構文自体のパースは Issue #2951 で `_lib/shell_parse.find_heredoc_body` へ
# 集約した。ここでは「--body に紐づく heredoc の開始位置」を特定するアンカーのみを扱う。
_BODY_HEREDOC_ANCHOR_RE = re.compile(r"""(?:--body|-b)\s+"?\$\(cat\s+<<""")


def _git(*args: str, cwd: str | None = None) -> tuple[int, str]:
    """Issue #2958: subprocess 実行部は `_lib.git_helpers.run_git()` に委譲する."""
    result = _run_git(*args, cwd=cwd, timeout=20)
    if result is None:
        return 1, ""
    return result.returncode, result.stdout


def _central_config_path() -> Path:
    """中央設定ファイルのパスを返す（環境変数 TIDD_NG_WORDS_CONFIG で上書き可能）."""
    env_path = os.environ.get("TIDD_NG_WORDS_CONFIG")
    if env_path:
        return Path(env_path)
    return _DEFAULT_CENTRAL_CONFIG


class _NgramRule:
    """中央設定の 1 ルール（repo_pattern + words + exclude_paths）."""

    def __init__(self, words: list[str], exclude_paths: list[str]) -> None:
        self.words = words
        self.exclude_paths = exclude_paths

    def is_path_excluded(self, file_path: str) -> bool:
        """file_path が exclude_paths のいずれかのパターンにマッチするか判定する.

        file_path は git relative パス（`docs/note.md`）か絶対パスのどちらも扱う。
        exclude_paths のパターンは相対パス prefix（`.claude/`）または glob パターン。
        """
        # 絶対パスの場合は Path.parts でパス末尾部分を使う（相対的な一致判定）
        p = Path(file_path)
        # 絶対パスとして部分文字列も確認するために str 表現も使う
        file_str = file_path
        # posix 表現（絶対パスで区切り統一）
        try:
            file_posix = p.as_posix()
        except (AttributeError, TypeError):
            file_posix = file_str

        for pattern in self.exclude_paths:
            # 1. 直接 fnmatch（相対パスに対して）
            if fnmatch.fnmatch(file_str, pattern):
                return True
            # 2. prefix 一致（相対パスに対して）
            if file_str.startswith(pattern):
                return True
            # 3. 絶対パスの場合、パス中に pattern が含まれるか確認
            #    例: file="/tmp/.../repo-a/.claude/config.md", pattern=".claude/"
            if pattern in file_posix:
                return True
            # 4. fnmatch でパス後半部分に対してマッチ
            #    例: pattern="LICENSE", file_path="/tmp/.../repo-a/LICENSE"
            if fnmatch.fnmatch(p.name, pattern):
                return True
        return False


def _load_central_config(git_root: str) -> list[_NgramRule]:
    """中央設定から現リポジトリにマッチするルール群を返す.

    中央設定が存在しない / tomllib 使用不可の場合は空リストを返す。
    """
    if tomllib is None:
        return []
    config_path = _central_config_path()
    if not config_path.is_file():
        return []
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except OSError:
        return []

    rules_data = data.get("rules")
    if not isinstance(rules_data, list):
        return []

    # リポジトリ名でマッチング（git_root の basename を使う）
    repo_name = Path(git_root).name
    matched: list[_NgramRule] = []
    for rule in rules_data:
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("repo_pattern", "")
        if not isinstance(pattern, str):
            continue
        if not fnmatch.fnmatch(repo_name, pattern):
            continue
        words_raw = rule.get("words", [])
        if not isinstance(words_raw, list):
            continue
        words = [w for w in words_raw if isinstance(w, str) and w]
        if not words:
            continue
        exclude_paths_raw = rule.get("exclude_paths", [])
        exclude_paths = [p for p in exclude_paths_raw if isinstance(p, str)]
        matched.append(_NgramRule(words=words, exclude_paths=exclude_paths))
    return matched


def _load_local_ng_words(git_root: str) -> list[str]:
    """`.claude/ng-words.local.txt` から NG ワードリストを読み込む（後方互換）.

    1 行 1 単語。`#` から始まる行・空行はスキップする。
    ファイルが存在しない環境では空リストを返す。
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
        "誤検知の場合は .claude/ng-words.local.txt または"
        " ~/.config/tidd_tools/ng-words.toml の該当エントリを見直してください。\n"
        "詳細: Issue #2308 #2418 / docs/reference/hooks.md#ban-ng-wordspy\n"
    )


def _extract_gh_body(command: str) -> str:
    """gh issue/pr create/edit/comment コマンドから本文を抽出する.

    `--body` / `-b`、`--body-file` / `-F`、heredoc（`--body "$(cat <<EOF ... EOF)"`）
    の各パターンに対応する。
    """
    anchor = _BODY_HEREDOC_ANCHOR_RE.search(command)
    if anchor is not None:
        body_from_heredoc = _find_heredoc_body(command[anchor.start() :])
        if body_from_heredoc is not None:
            return body_from_heredoc

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


def _check_edit_write_tool(
    payload: dict,
    tool_name: str,
    central_rules: list[_NgramRule],
    local_words: list[str],
) -> int:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    file_path = get_file_path(payload)
    if not file_path:
        return 0

    # Issue #3221: 新内容は共通ヘルパーから取得する（apply_patch は patch の追加行）
    text = get_new_content(payload)
    if not text:
        return 0

    # 中央設定ルールを検査（exclude_paths 対象のパスはスキップ）
    for rule in central_rules:
        if rule.is_path_excluded(file_path):
            continue
        word = _find_ng_word(text, rule.words)
        if word is not None:
            sys.stderr.write(_blocked_message(file_path, word))
            return 2

    # local.txt ワードで検査（exclude_paths なし・後方互換）
    if local_words:
        word = _find_ng_word(text, local_words)
        if word is not None:
            sys.stderr.write(_blocked_message(file_path, word))
            return 2

    return 0


def _check_git_commit(
    command: str,
    git_root: str,
    central_rules: list[_NgramRule],
    local_words: list[str],
) -> int:
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

        # 中央設定ルールを検査（exclude_paths 対象ファイルはスキップ）
        for rule in central_rules:
            if rule.is_path_excluded(file):
                continue
            word = _find_ng_word(blob, rule.words)
            if word is not None:
                sys.stderr.write(_blocked_message(file, word))
                return 2

        # local.txt ワードで検査（後方互換）
        if local_words:
            word = _find_ng_word(blob, local_words)
            if word is not None:
                sys.stderr.write(_blocked_message(file, word))
                return 2

    return 0


def _check_gh_command(
    command: str,
    central_rules: list[_NgramRule],
    local_words: list[str],
) -> int:
    """gh Issue/PR 本文を検査する。exclude_paths は対象外（本文にパスの概念なし）."""
    if not _GH_COMMAND_RE.search(command):
        return 0
    body = _extract_gh_body(command)
    if not body:
        return 0

    # 中央設定ルール（exclude_paths 無視・本文なので全ワード検査）
    for rule in central_rules:
        word = _find_ng_word(body, rule.words)
        if word is not None:
            sys.stderr.write(_blocked_message("gh コマンドの本文", word))
            return 2

    # local.txt ワードで検査
    if local_words:
        word = _find_ng_word(body, local_words)
        if word is not None:
            sys.stderr.write(_blocked_message("gh コマンドの本文", word))
            return 2

    return 0


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)
    command = get_command(payload) if tool_name == "Bash" else None

    # Issue #2454: hook プロセスは常にメイン checkout の CWD で動くため、worktree で
    # コミットされた内容を検査できるよう対象リポジトリの CWD を解決する。
    target_cwd = resolve_target_cwd(payload, command)
    git_root = git_toplevel(cwd=target_cwd) or ""
    if not git_root:
        return 0

    central_rules = _load_central_config(git_root)
    local_words = _load_local_ng_words(git_root)

    if not central_rules and not local_words:
        # どちらも未設定の環境では何もチェックしない
        return 0

    if is_file_edit_tool(tool_name):
        return _check_edit_write_tool(payload, tool_name, central_rules, local_words)

    if tool_name != "Bash":
        return 0

    if not command:
        return 0

    if _GIT_COMMIT_RE.search(command):
        rc2 = _check_git_commit(command, git_root, central_rules, local_words)
        if rc2:
            return rc2

    return _check_gh_command(command, central_rules, local_words)


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("ban-ng-words"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
