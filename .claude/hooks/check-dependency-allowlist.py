#!/usr/bin/env python3
"""PreToolUse hook: pyproject.toml への許可リスト外依存追加をブロックする（Issue #2561）.

pyproject.toml の dependencies / optional-dependencies の変更を検知し、
`.claude/rules/dependency-allowlist.yaml` に含まれていないパッケージ名を検出した場合に
exit 2 でブロックして人間承認（allowlist への追加 PR）へ誘導する。

対象:
  - Edit / Write tool で pyproject.toml を変更する場合:
    [project] dependencies / [project.optional-dependencies] に
    許可リスト外のパッケージが含まれるとき
  - Bash tool で `uv add <pkg>` を実行する場合:
    <pkg> が許可リスト外のとき

対象外:
  - pyproject.toml の dependencies 以外の変更（version・scripts 等）
  - pyproject.toml 以外のファイル（requirements.txt 等）
  - git+ URL 依存（パッケージ名部分を抽出して照合する）

パッケージ名照合: PEP 503 正規化（小文字・ハイフン/アンダースコア/ドット 同一視）

stdlib のみ使用（hook は stdlib のみ前提）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (
    get_command,
    get_file_path,
    get_new_content,
    get_tool_name,
    is_file_edit_tool,
    is_hook_enabled,
    read_hook_input,
)

# ── allowlist YAML パス ────────────────────────────────────────────────────────

_ALLOWLIST_PATH = (
    Path(__file__).resolve().parent.parent / "rules" / "dependency-allowlist.yaml"
)

# ── パッケージ名抽出パターン ──────────────────────────────────────────────────

# pyproject.toml の dependencies / optional-dependencies 内のパッケージ名を抽出する。
# 各エントリは以下の形式:
#   "pkg"
#   "pkg>=1.0"
#   "pkg[extras]"
#   "pkg[extras]>=1.0"
#   "pkg @ git+https://..."
# パッケージ名はクォート直後、または @ 前後の空白を除いた部分。
# 複数行にまたがる場合も対応する。
_DEP_ENTRY_RE = re.compile(
    r"""['"]([\w][\w.-]*(?:\[[\w,.-]+\])?)\s*(?:@\s*git\+[^'"]+)?(?:[<>=!~;][^'"]*)?['"]""",
    re.MULTILINE,
)

# [project] dependencies / [project.optional-dependencies] セクションを抽出する正規表現。
# TOML のセクション境界は次の [section] 開始まで。
_PROJECT_DEPS_SECTION_RE = re.compile(
    r"""
    # [project] dependencies または [project.optional-dependencies.*] セクション開始
    ^\[project(?:\.optional-dependencies)?\][ \t]*\n
    # セクション内容（次の [section] まで）
    ((?:(?!\[).*\n)*)
    """,
    re.MULTILINE | re.VERBOSE,
)

# pyproject.toml の inline "dependencies" キー行を含む配列全体を抽出する。
# [project] dependencies = [...] の値部分を抽出する。
_DEPS_VALUE_RE = re.compile(
    r"""^\s*(?:dependencies)\s*=\s*(\[.*?\])""",
    re.MULTILINE | re.DOTALL,
)

# git+ URL 形式の依存: "pkg-name @ git+..." → パッケージ名を抽出
_GIT_URL_PKG_RE = re.compile(
    r"""['"]([\w][\w.-]*)\s*@\s*git\+""",
    re.MULTILINE,
)

# uv add コマンドのパッケージ名抽出
# "uv add pkg" / "uv add 'pkg>=1.0'" / "uv add 'pkg @ git+...'"
_UV_ADD_RE = re.compile(r"\buv\s+add\s+")
_UV_PKG_RE = re.compile(
    r"""['"]?([\w][\w.-]*(?:\[[\w,.-]+\])?)\s*(?:@\s*git\+[^\s'"]*)?(?:[<>=!~;][^'"\s]*)?['"]?"""
)


def _normalize_pkg_name(name: str) -> str:
    """PEP 503 正規化: 小文字化・ハイフン/アンダースコア/ドットを同一視する."""
    return re.sub(r"[-_.]", "-", name.lower())


def _load_allowlist() -> frozenset[str]:
    """allowlist YAML を読み込んで正規化済みパッケージ名セットを返す.

    stdlib のみで YAML を簡易パースする（PyYAML は hook 環境で利用可能だが
    hook の stdlib 制約の可能性を考慮してまず stdlib で試みる）。
    hook の同一プロセス内では PyYAML が利用可能なため PyYAML を試み、
    import 失敗時は簡易パーサーにフォールバックする。
    """
    if not _ALLOWLIST_PATH.is_file():
        # allowlist ファイルが存在しない場合はブロックしない（設定不備を見逃さない安全側）
        sys.stderr.write(
            f"WARN: dependency-allowlist.yaml が見つかりません: {_ALLOWLIST_PATH}\n"
            "hook を no-op として扱います。allowlist ファイルを作成してください。\n"
        )
        return frozenset()

    content = _ALLOWLIST_PATH.read_text(encoding="utf-8")

    # PyYAML が利用可能なら使う
    try:
        import yaml  # type: ignore[import-not-found]

        data = yaml.safe_load(content)
        if isinstance(data, dict) and "packages" in data:
            packages = data["packages"]
            if isinstance(packages, list):
                return frozenset(_normalize_pkg_name(str(p)) for p in packages if p)
    except ImportError:
        pass

    # PyYAML 不使用時の簡易パーサー（stdlib のみ）
    # "  - package-name" 行を抽出する
    packages_std: list[str] = []
    in_packages = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("packages:"):
            in_packages = True
            continue
        if in_packages:
            if stripped.startswith("- "):
                pkg = stripped[2:].strip()
                # コメントを除去
                if "#" in pkg:
                    pkg = pkg[: pkg.index("#")].strip()
                if pkg:
                    packages_std.append(pkg)
            elif stripped and not stripped.startswith("#"):
                # 新しいセクションが始まった
                in_packages = False

    return frozenset(_normalize_pkg_name(p) for p in packages_std if p)


def _extract_packages_from_toml_content(content: str) -> list[str]:
    """pyproject.toml のコンテンツから dependencies 内のパッケージ名を抽出する.

    [project] dependencies と [project.optional-dependencies] の両方を対象とする。
    git+ URL 依存のパッケージ名も抽出する。
    """
    packages: list[str] = []

    # git+ URL 形式のパッケージ名を抽出（先に処理）
    git_url_names = {m.group(1) for m in _GIT_URL_PKG_RE.finditer(content)}

    # dependencies が含まれる行を見つける
    # [project] または [project.optional-dependencies] セクション内の "dependencies" または "dev" etc.
    # より幅広く: dependencies/dev/test/extras などの配列値を抽出
    dep_sections = _PROJECT_DEPS_SECTION_RE.findall(content)
    if not dep_sections:
        # セクション境界が取れなかった場合、全体から依存配列を探す
        dep_sections = [content]

    for section in dep_sections:
        # dependencies = [...] の値部分を抽出
        for match in _DEPS_VALUE_RE.finditer(section):
            array_text = match.group(1)
            # 配列内のパッケージ名を抽出
            for pkg_match in _DEP_ENTRY_RE.finditer(array_text):
                pkg_name = pkg_match.group(1)
                # [extras] 部分を除去
                pkg_name = re.sub(r"\[.*?\]", "", pkg_name).strip()
                if pkg_name:
                    packages.append(pkg_name)

        # optional-dependencies の値も抽出（キー名を問わず = [...] 全部）
        for m in re.finditer(
            r"""^\s*\w[\w-]*\s*=\s*(\[.*?\])""",
            section,
            re.MULTILINE | re.DOTALL,
        ):
            # "dependencies" 以外のキー（dev, test, embeddings etc.）も対象
            array_text = m.group(1)
            for pkg_match in _DEP_ENTRY_RE.finditer(array_text):
                pkg_name = pkg_match.group(1)
                pkg_name = re.sub(r"\[.*?\]", "", pkg_name).strip()
                if pkg_name and pkg_name not in packages:
                    packages.append(pkg_name)

    # git+ URL パッケージを追加（既存リストになければ）
    for gp in git_url_names:
        if gp not in packages:
            packages.append(gp)

    # 重複除去（順序保持）
    seen: set[str] = set()
    unique: list[str] = []
    for p in packages:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _get_new_content(payload: dict) -> str | None:
    """tool_input から Edit/Write の新コンテンツを取り出す.

    Write の場合は "content" キーの全文を返す。
    Edit の場合は file_path のディスク上ファイルを読み、
    old_string → new_string を適用した編集後全文を返す。
    ファイルが読めない・old_string が見つからない場合は new_string 単体にフォールバックする。
    Codex の apply_patch は共通ヘルパー `hook_io.get_new_content()` が
    patch の追加行（+ 行）を返す（Issue #3221）。
    """
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None

    # Write: content キーの全文を返す（従来どおり）
    content_val = tool_input.get("content")
    if isinstance(content_val, str):
        return content_val

    # Edit: ディスク上のファイル全文に old_string → new_string を適用した全文を返す
    new_string = (
        tool_input.get("new_string")
        or tool_input.get("new_str")
        or tool_input.get("replacement")
    )
    if not isinstance(new_string, str):
        # Codex apply_patch: 共通ヘルパーへ委譲（content / new_string が存在しない場合）
        return get_new_content(payload)

    old_string = tool_input.get("old_string") or tool_input.get("old_str")
    file_path_str = get_file_path(payload)

    if isinstance(old_string, str) and isinstance(file_path_str, str):
        # ディスク上のファイルを読み、編集後全文をシミュレートする
        try:
            disk_content = Path(file_path_str).read_text(encoding="utf-8")
            if old_string in disk_content:
                return disk_content.replace(old_string, new_string, 1)
        except OSError:
            # ファイルが読めない場合は new_string 単体にフォールバック
            pass

    # フォールバック: old_string が見つからない・file_path 不明の場合は new_string 単体
    return new_string


def _check_pyproject_content(content: str, allowlist: frozenset[str]) -> list[str]:
    """pyproject.toml のコンテンツを検査し、許可リスト外のパッケージ名を返す."""
    # dependencies セクションが含まれているか確認
    if "dependencies" not in content:
        return []

    packages = _extract_packages_from_toml_content(content)
    blocked: list[str] = []
    for pkg in packages:
        normalized = _normalize_pkg_name(pkg)
        if normalized not in allowlist:
            blocked.append(pkg)
    return blocked


def _extract_uv_add_packages(command: str) -> list[str]:
    """uv add コマンドからパッケージ名リストを抽出する.

    例:
      "uv add pyyaml" → ["pyyaml"]
      "uv add 'pkg>=1.0'" → ["pkg"]
      "uv add 'tidd-tools @ git+...'" → ["tidd-tools"]
    """
    if not _UV_ADD_RE.search(command):
        return []

    # uv add の後ろの引数部分を抽出（複数パッケージ指定可のため全トークンを検査）
    after_uv_add = _UV_ADD_RE.split(command, maxsplit=1)[-1]
    packages: list[str] = []

    # クォートで囲まれたトークンまたはスペース区切りトークンを抽出
    token_re = re.compile(r"""'([^']*)'|"([^"]*)"|(\S+)""")
    for m in token_re.finditer(after_uv_add):
        raw = m.group(1) or m.group(2) or m.group(3) or ""
        if not raw:
            continue
        # オプションフラグを除外
        if raw.startswith("-"):
            continue
        # git+ URL の場合はパッケージ名を抽出
        git_match = re.match(r"([\w][\w.-]*)\s*@\s*git\+", raw)
        if git_match:
            packages.append(git_match.group(1))
            continue
        # version specifier が含まれる場合はパッケージ名のみ抽出
        pkg_m = re.match(r"([\w][\w.-]*(?:\[[\w,.-]+\])?)", raw)
        if pkg_m:
            pkg_name = pkg_m.group(1)
            # [extras] 除去
            pkg_name = re.sub(r"\[.*?\]", "", pkg_name).strip()
            if pkg_name:
                packages.append(pkg_name)

    return packages


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")
    tool_name = get_tool_name(payload)

    allowlist = _load_allowlist()
    # allowlist が空（ファイル不存在等）の場合はブロックしない
    if not allowlist:
        return 0

    if is_file_edit_tool(tool_name):
        file_path = get_file_path(payload)
        if not file_path:
            return 0
        # pyproject.toml のみを対象とする
        if Path(file_path).name != "pyproject.toml":
            return 0

        content = _get_new_content(payload)
        if not content:
            return 0

        blocked = _check_pyproject_content(content, allowlist)
        if not blocked:
            return 0

        sys.stderr.write(
            "Blocked: 許可リスト外のパッケージが pyproject.toml の dependencies に追加されようとしています。\n"
            "\n"
            f"ブロック対象: {', '.join(blocked)}\n"
            f"対象ファイル: {file_path}\n"
            "\n"
            "新しいパッケージを採用する場合は、以下の手順を踏んでください:\n"
            "  1. Issue を作成してパッケージの採用理由を記録する\n"
            "  2. `.claude/rules/dependency-allowlist.yaml` にパッケージ名を追加する PR を作成する\n"
            "  3. allowlist への追加 PR のレビュー後、依存追加 PR を作成する\n"
            "\n"
            "詳細: docs/reference/hooks.md#check-dependency-allowlistpy\n"
        )
        return 2

    if tool_name == "Bash":
        command = get_command(payload)
        if not command:
            return 0

        # uv add コマンドのみを対象とする
        if not _UV_ADD_RE.search(command):
            return 0

        pkgs = _extract_uv_add_packages(command)
        blocked = [p for p in pkgs if _normalize_pkg_name(p) not in allowlist]
        if not blocked:
            return 0

        sys.stderr.write(
            "Blocked: 許可リスト外のパッケージが uv add で追加されようとしています。\n"
            "\n"
            f"ブロック対象: {', '.join(blocked)}\n"
            f"コマンド: {command}\n"
            "\n"
            "新しいパッケージを採用する場合は、以下の手順を踏んでください:\n"
            "  1. Issue を作成してパッケージの採用理由を記録する\n"
            "  2. `.claude/rules/dependency-allowlist.yaml` にパッケージ名を追加する PR を作成する\n"
            "  3. allowlist への追加 PR のレビュー後、uv add を実行する\n"
            "\n"
            "詳細: docs/reference/hooks.md#check-dependency-allowlistpy\n"
        )
        return 2

    return 0


def main() -> int:
    # is_hook_enabled ゲート（default OFF・opt-in 設計）
    if not is_hook_enabled("check-dependency-allowlist"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
