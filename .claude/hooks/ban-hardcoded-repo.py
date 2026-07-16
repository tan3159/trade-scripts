#!/usr/bin/env python3
"""PreToolUse hook: ハードコードを検出してブロックする.

旧 ban-hardcoded-repo.sh を 1:1 で踏襲する（Phase 4 / #1057 で Python 化）。
Issue #336・#868。

Issue #1642: `.claude/rules/hardcoded-patterns.yaml` から追加パターンを読み込む。
メールドメイン・gist URL 個人 username 等の個人値パターンを外部 YAML で管理する。

Issue #1780: Edit/Write ツール呼び出し時にも発火するよう拡張。
- Edit ツール: `new_string` フィールドを検査対象にする
- Write ツール: `content` フィールドを検査対象にする
- 検出パスは `file_path` で対象ファイルを特定する
- git commit パスは引き続き git staged ファイルをスキャンする

Issue #1930: `git add <files> && git commit` 連結コマンドのバイパスを修正。
PreToolUse 発火時点でステージングが空の場合も add 対象の working tree を検査する。

lint-hardcoded-repo.yml（GitHub Actions）の代替。
git commit コマンドが実行されようとしたとき、ステージング済みの
scripts/ .claude/ agt/ 配下のファイルにリポジトリ固有文字列
（being-gaia-plan, ai-dev-handbook）がハードコードされていないか確認する。

検出ロジックは `_lib.hardcoded_repo` に集約（DRY: scripts/lint-hardcoded-repo
系の Python 化と共通）。

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
from _lib.hardcoded_repo import (  # noqa: E402
    PATTERNS,
    is_excluded_basename,
    is_excluded_line,
    is_excluded_path,
)
from _lib.hook_io import get_command, get_tool_name, is_hook_enabled, read_hook_input  # noqa: E402

_GIT_COMMIT_RE = re.compile(r"(^|&&|;|\|)\s*git commit(\s|$)")
_STAGED_PATH_RE = re.compile(r"^(scripts/|\.claude/|agt/)")

# Edit/Write ツールで検査対象にするパスプレフィックス（git commit パスと同じスコープ）
_EDIT_PATH_RE = re.compile(r"^(scripts/|\.claude/|agt/)")


def _load_yaml_patterns(git_root: str) -> list[dict[str, str]]:
    """hardcoded-patterns.yaml から追加パターンリストを読み込む（stdlib のみ）.

    PyYAML を使わず、detect-ai-confirm-misuse.py と同じ手書き YAML パーサーを使う。
    YAML は git_root（リポジトリルート）の `.claude/rules/hardcoded-patterns.yaml` を参照する。
    ファイルが存在しない場合は空リストを返す（後方互換・yaml 未設定環境対応）。

    フォーマット:
        patterns:
          - id: <str>
            regex: '<str>'
            description: '<str>'
            message: '<str>'

    Returns:
        パターン dict のリスト（regex キーを持つ）。読み込み失敗・ファイル不在は空リスト。
    """
    yaml_path = Path(git_root) / ".claude" / "rules" / "hardcoded-patterns.yaml"
    if not yaml_path.is_file():
        return []
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return []

    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        # コメント行・空行はスキップ
        if stripped.startswith("#") or not stripped:
            continue
        # 新しいエントリの開始
        if stripped.startswith("- id:"):
            if current:
                entries.append(current)
            current = {"id": stripped[len("- id:") :].strip()}
        elif ":" in stripped and current:
            key, _, val = stripped.partition(":")
            key = key.strip().lstrip("- ")
            val = val.strip().strip("'\"")
            if key and val:
                current[key] = val
    if current:
        entries.append(current)

    # 'regex' キーを持つエントリのみ返す（patterns: [] 等のダミー行を除外）
    return [e for e in entries if "regex" in e]


def _git(*args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=False, timeout=20
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout


def _compile_yaml_patterns(
    yaml_patterns: list[dict[str, str]],
) -> list[tuple[re.Pattern[str], str, str]]:
    """YAML パターンを事前コンパイルし、不正 regex は WARN + skip して返す."""
    compiled_yaml: list[tuple[re.Pattern[str], str, str]] = []
    for entry in yaml_patterns:
        raw_regex = entry.get("regex", "")
        message = entry.get(
            "message", f"個人値パターン '{raw_regex}' がハードコードされています"
        )
        try:
            compiled = re.compile(raw_regex)
        except re.error:
            sys.stderr.write(
                f"WARN: hardcoded-patterns.yaml の regex が不正です: {raw_regex}\n"
            )
            continue
        compiled_yaml.append((compiled, message, raw_regex))
    return compiled_yaml


def _check_content_lines(
    lines: list[str],
    file_path: str,
    compiled_yaml: list[tuple[re.Pattern[str], str, str]],
) -> tuple[bool, str]:
    """行リストをデフォルトパターン + YAML パターンで検査する.

    Args:
        lines: 検査対象の行リスト
        file_path: ファイルパス（除外判定に使用）
        compiled_yaml: コンパイル済み YAML パターンのリスト

    Returns:
        (found, found_message) のタプル。検出あり: (True, メッセージ)、なし: (False, "")
    """
    basename = os.path.basename(file_path)
    if is_excluded_basename(basename):
        return False, ""

    # 既存デフォルトパターン（文字列マッチ）
    for pattern in PATTERNS:
        for line in lines:
            if pattern not in line:
                continue
            if is_excluded_line(line):
                continue
            found_message = (
                "リポジトリ固有文字列 (being-gaia-plan, ai-dev-handbook) "
                "がハードコードされています。\n"
                "gh repo view --json nameWithOwner -q .nameWithOwner で動的取得に変更してください。\n"
                "詳細: Issue #336, #868 / docs/reference/hooks.md#ban-hardcoded-repopy\n"
            )
            sys.stderr.write(f"Blocked: {found_message}\n")
            return True, found_message

    # YAML 追加パターン（regex マッチ）
    for compiled, message, _raw_regex in compiled_yaml:
        for line in lines:
            if not compiled.search(line):
                continue
            if is_excluded_line(line):
                continue
            found_message = (
                f"{message}。\n"
                f"個人値・組織固有値のハードコードは避け、環境変数や設定ファイルで管理してください。\n"
                f"詳細: docs/reference/hooks.md#ban-hardcoded-repopy\n"
            )
            sys.stderr.write(f"Blocked: {message}\n")
            return True, found_message

    return False, ""


def _check_edit_write_tool(
    payload: dict,
    tool_name: str,
    git_root: str,
) -> int:
    """Edit / Write ツール呼び出し時のハードコード検査（Issue #1780）.

    Edit ツール: `new_string` フィールドを検査する
    Write ツール: `content` フィールドを検査する
    `file_path` で除外対象（basename・パスプレフィックス）を判定する。

    Returns:
        0 = 問題なし、2 = ブロック
    """
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    file_path = str(tool_input.get("file_path") or "")
    if not file_path:
        return 0

    # パスプレフィックスでスコープを絞る（scripts/ .claude/ agt/ 配下のみ）
    # 絶対パスの場合は git_root からの相対パスに変換を試みる
    rel_path = file_path
    if os.path.isabs(file_path) and git_root:
        try:
            rel_path = os.path.relpath(file_path, git_root)
        except ValueError:
            pass
    if not _EDIT_PATH_RE.match(rel_path):
        return 0

    # 検査テキストの取得
    if tool_name == "Edit":
        text = str(tool_input.get("new_string") or "")
    else:  # Write
        text = str(tool_input.get("content") or "")

    if not text:
        return 0

    # YAML パターンを読み込む
    yaml_patterns = _load_yaml_patterns(git_root)
    compiled_yaml = _compile_yaml_patterns(yaml_patterns)

    lines = text.splitlines()
    found, _msg = _check_content_lines(lines, file_path, compiled_yaml)
    return 2 if found else 0


_GIT_ADD_RE = re.compile(r"^\s*git\s+add\b")
# -A / --all / . → untracked を含む全体スキャン
_ADD_ALL_FLAGS = frozenset(["-A", "--all"])
# -u / --update → tracked 変更のみ（untracked は含まない）
_ADD_UPDATE_FLAGS = frozenset(["-u", "--update"])


_GIT_COMMIT_SEGMENT_RE = re.compile(r"^\s*git\s+commit\b")


def _iter_add_segments(command: str) -> list[tuple[list[str], bool, bool]]:
    """コマンド文字列から `git add` セグメントをパースして per-segment タプルリストを返す（Issue #1930）.

    チェーンセパレータ（&&, ;, ||, |）でセグメントに分割し、`git add` セグメントごとに
    (pathspecs, add_all, add_update) を生成する。グローバルマージではなくセグメント単位で
    保持することで、pathspec スコープを正確に反映できる（コードレビュー指摘修正）。

    - add_all=True: -A / --all フラグ、または 'A' を含む短フラグ束（例: -Av）→
      untracked を含む working tree 全体を走査する。
    - add_update=True: -u / --update、または 'u' を含む短フラグ束（-- で始まらないもの）→
      tracked 変更のみを走査する（untracked は含まない）。
    - pathspecs: 明示的な pathspec トークン。`.` は通常の pathspec として扱い、
      add_all は設定しない（`git add -u .` の untracked 誤ブロック修正）。
    フラグも pathspec も持たない `git add`（何もしない）は結果リストに含めない。

    Issue #1979: `git commit` セグメントより後に連結された `git add` は
    そのコミットの staging に影響しないため検査対象から除外する。

    Issue #1986: 同一コマンド文字列に commit が複数ある場合のバイパス修正。
    「最初の commit 以降すべて除外」では `add A && commit && add B_dirty && commit`
    の B_dirty が除外されホックをすり抜けられた。
    修正後: 各 add セグメントについて「その後ろに commit セグメントが存在するか」を
    チェックし、後続 commit がある add は検査対象に含める（後続 commit がない add のみ除外）。
    """
    segments = re.split(r"&&|;|\|\||\|", command)

    # パス 1: 各セグメントが add / commit / other のどれかを先に判定する
    seg_is_commit = [bool(_GIT_COMMIT_SEGMENT_RE.match(s)) for s in segments]
    seg_is_add = [bool(_GIT_ADD_RE.match(s)) for s in segments]

    result: list[tuple[list[str], bool, bool]] = []

    for i, segment in enumerate(segments):
        if not seg_is_add[i]:
            continue

        # パス 2: この add より後ろに commit セグメントが存在するか確認する（Issue #1986）
        # 後続 commit がなければこの add は前回の commit 以後の「次作業」であり
        # staging に影響しないため除外する（Issue #1979 の意図を維持）
        has_following_commit = any(
            seg_is_commit[j] for j in range(i + 1, len(segments))
        )
        if not has_following_commit:
            continue

        # "git add" 以降をトークン化
        rest = re.sub(r"^\s*git\s+add\b", "", segment)
        try:
            tokens = shlex.split(rest)
        except ValueError:
            tokens = rest.split()

        pathspecs: list[str] = []
        add_all = False
        add_update = False
        past_separator = False
        for tok in tokens:
            if past_separator:
                pathspecs.append(tok)
                continue
            if tok == "--":
                past_separator = True
                continue
            if tok.startswith("-"):
                if tok in _ADD_ALL_FLAGS or (
                    len(tok) > 1 and tok[1] != "-" and "A" in tok
                ):
                    add_all = True
                elif tok in _ADD_UPDATE_FLAGS or (
                    len(tok) > 1 and tok[1] != "-" and "u" in tok
                ):
                    add_update = True
                continue
            pathspecs.append(tok)

        if pathspecs or add_all or add_update:
            result.append((pathspecs, add_all, add_update))

    return result


def _parse_porcelain_paths(status_out: str) -> list[str]:
    """git status --porcelain 出力からパスリストを抽出する.

    - 4 文字未満の行はスキップ（ヘッダ等）
    - 純削除（XY から D と空白を除いた結果が空）はスキップ
    - path = line[3:]（XY + 空白の後）
    - リネーム（" -> " を含む）はターゲット側（右辺）を使う
    - 前後のダブルクォートを除去する
    """
    paths: list[str] = []
    seen: set[str] = set()
    for raw_line in status_out.splitlines():
        if len(raw_line) < 4:
            continue
        xy = raw_line[:2]
        if "D" in xy and xy.strip("D ") == "":
            continue
        path_part = raw_line[3:]
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        path_part = path_part.strip('"')
        if path_part not in seen:
            seen.add(path_part)
            paths.append(path_part)
    return paths


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")  # Issue #1364
    tool_name = get_tool_name(payload)

    # Issue #1780: Edit / Write ツール対応
    if tool_name in ("Edit", "Write"):
        rc, root_out = _git("rev-parse", "--show-toplevel")
        git_root = root_out.strip() if rc == 0 else ""
        return _check_edit_write_tool(payload, tool_name, git_root)

    # git commit パス（既存動作）
    command = get_command(payload)
    if not command:
        return 0
    if not _GIT_COMMIT_RE.search(command):
        return 0

    rc, root_out = _git("rev-parse", "--show-toplevel")
    if rc != 0:
        return 0
    git_root = root_out.strip()
    if not git_root:
        return 0

    rc, staged_out = _git(
        "-C", git_root, "diff", "--cached", "--name-only", "--diff-filter=AM"
    )
    if rc != 0:
        return 0

    target_files = [
        line for line in staged_out.splitlines() if line and _STAGED_PATH_RE.match(line)
    ]

    # Issue #1930: add+commit 連結時の working tree pathspec をセグメント単位で抽出する
    add_segments = _iter_add_segments(command)
    has_add_chain = bool(add_segments)

    if not target_files and not has_add_chain:
        return 0

    # YAML から追加パターンを読み込む（Issue #1642）
    yaml_patterns = _load_yaml_patterns(git_root)
    compiled_yaml = _compile_yaml_patterns(yaml_patterns)

    # ── staged ファイルの検査（既存動作） ──────────────────────────────────────
    for file in target_files:
        # Issue #1933: .md / /tests/ / /docs/ パスは除外する
        if is_excluded_path(file):
            continue

        rc, blob = _git("-C", git_root, "show", f":{file}")
        if rc != 0:
            continue

        lines = blob.splitlines()
        hit, msg = _check_content_lines(lines, file, compiled_yaml)
        if hit:
            sys.stderr.write("\n")
            sys.stderr.write(msg)
            return 2

    # ── Issue #1930: add チェーン working tree 検査（per-segment） ─────────────
    if has_add_chain:
        candidate_paths: list[str] = []
        seen_candidates: set[str] = set()

        for seg_pathspecs, seg_add_all, seg_add_update in add_segments:
            # untracked フラグ: add -u は tracked 変更のみなので -uno、それ以外は -u
            untracked_flag = "-uno" if (seg_add_update and not seg_add_all) else "-u"
            # pathspec スコープ: セグメント固有の pathspec を使用（空 = ツリー全体）
            rc, status_out = _git(
                "-C",
                git_root,
                "status",
                "--porcelain",
                untracked_flag,
                "--",
                *seg_pathspecs,
            )
            if rc == 0:
                for p in _parse_porcelain_paths(status_out):
                    if p not in seen_candidates:
                        seen_candidates.add(p)
                        candidate_paths.append(p)

        for path_part in candidate_paths:
            if not _STAGED_PATH_RE.match(path_part):
                continue
            if is_excluded_path(path_part):
                continue

            full_path = Path(git_root) / path_part
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            hit, msg = _check_content_lines(
                content.splitlines(), path_part, compiled_yaml
            )
            if hit:
                sys.stderr.write("\n")
                sys.stderr.write(msg)
                return 2

    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("ban-hardcoded-repo"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
