"""`tidd win-compat-scan` サブコマンド.

Windows ネイティブで動かない可能性のあるコード・設定を grep ベースで検出する（Issue #1092）。

検出パターン（旧 Issue 設計より）:
- `#!/usr/bin/env bash` / `#!/bin/bash` shebang
- WSL マウントパス `/mnt/c/...` のハードコード
- bash プロセス置換 `<(...)`
- `mapfile` / `readarray`（bash 4+）
- GNU coreutils 限定オプション（`date -d` / `stat --format` 等）
- `~/.bashrc` 等のシェル前提設定への直接参照

Issue #3898 で追加した 3 パターン（`--strict` 指定時のみ exit 1 の gate 対象）:
- `subprocess_text_without_encoding`（`subprocess.run(..., text=True)` の `encoding=` 未指定・AST 判定）
- `posix_path_assumption`（payload パスに対する `/` 前提の正規表現・部分一致）
- `venv_bin_hardcode`（`.venv/bin` 決め打ち。Windows は `.venv/Scripts` + `.exe`。
  `Path(".venv") / "bin"` のように `Path()` を挟む形式も検出・Issue #3936）

この 3 パターンは Python コード構文の検出のため `.py` ファイルのみを対象とする
（ドキュメント内のコード例で `--strict` が失敗しないようにするため）。

除外:
- `.git/` `.venv/` `node_modules/` `__pycache__/`
- `.bats` ファイル（Phase 2-C / Phase 3 / Phase 4 で削除済み）
- `projects/gas/` 配下（GAS は本プロジェクトの Windows 対応スコープ外）
- 本ファイル自身（パターン文字列の誤検知防止）

終了コードは既定では常に 0（情報提供のみ・CI を fail させない）。
`--strict` 指定時のみ、上記 3 パターン（`Pattern.strict=True`）の検出があれば exit 1 になる。
"""

from __future__ import annotations

import argparse
import ast
import logging
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tidd_tools.shared import git_client
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GitCommandError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Pattern:
    name: str
    regex: re.Pattern[str] | None
    description: str
    strict: bool = False


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        name="bash_shebang",
        regex=re.compile(r"^#!.*/bin/bash|^#!/usr/bin/env bash"),
        description="bash shebang（Windows ネイティブでは bash がないため動かない）",
    ),
    Pattern(
        name="wsl_mount_path",
        regex=re.compile(r"/mnt/c(?:/|$)"),
        description="WSL マウントパスのハードコード（Windows ネイティブからアクセス不能）",
    ),
    Pattern(
        name="bash_process_substitution",
        regex=re.compile(r"<\([^)]+\)|>\([^)]+\)"),
        description="bash プロセス置換 <(...) / >(...)（POSIX sh では動かない）",
    ),
    Pattern(
        name="bash_mapfile",
        regex=re.compile(r"\bmapfile\b|\breadarray\b"),
        description="bash 4+ 固有の mapfile/readarray（Windows の Git Bash でも環境依存）",
    ),
    Pattern(
        name="gnu_date_d",
        regex=re.compile(r"\bdate\s+-d\b"),
        description="GNU coreutils 限定 `date -d`（macOS BSD date では動かない・Windows 不可）",
    ),
    Pattern(
        name="gnu_stat_format",
        regex=re.compile(r"\bstat\s+--format=|\bstat\s+-c\s"),
        description="GNU coreutils 限定 `stat --format` / `stat -c`（BSD/Windows で挙動異なる）",
    ),
    Pattern(
        name="bashrc_direct_reference",
        regex=re.compile(r"~/\.bashrc\b|\$HOME/\.bashrc\b"),
        description="`~/.bashrc` への直接参照（Windows では `Microsoft.PowerShell_profile.ps1` 等）",
    ),
    Pattern(
        name="import_fcntl",
        regex=re.compile(r"^\s*import\s+fcntl\b|^\s*from\s+fcntl\s+import"),
        description="Unix 専用 stdlib `fcntl` の import（Windows で ImportError。`filelock` パッケージへ置換推奨）",
    ),
    Pattern(
        name="tempdir_tmp_hardcode",
        regex=re.compile(r'dir\s*=\s*["\']\/tmp["\']|Path\(["\']\/tmp["\']'),
        description="`/tmp` の dir 指定ハードコード（Windows で無効。`tempfile.gettempdir()` に置換）",
    ),
    Pattern(
        name="apt_dpkg_subprocess",
        regex=re.compile(r'["\'](apt-get|apt|dpkg)["\']'),
        description="Debian/Ubuntu 専用 `apt-get` / `apt` / `dpkg`（Windows は winget/scoop 等）",
    ),
    Pattern(
        name="systemctl_subprocess",
        regex=re.compile(r'["\'](systemctl|journalctl)["\']'),
        description="Linux 専用 systemd（Windows は Task Scheduler / Services 相当）",
    ),
    Pattern(
        name="ps_pstree_subprocess",
        regex=re.compile(r'["\']pstree["\']|\bsubprocess\.run\(\s*\[["\']ps["\']'),
        description="Linux 系 `ps` / `pstree`（Windows は `tasklist` / `wmic`）",
    ),
    Pattern(
        name="proc_stat_reference",
        regex=re.compile(r'["\']?/proc/(?:stat|meminfo|cpuinfo)'),
        description="Linux 専用 `/proc/*`（Windows 非対応。`psutil` 等でクロス化推奨）",
    ),
    # Issue #3898 で追加（--strict gate 対象）
    Pattern(
        name="subprocess_text_without_encoding",
        regex=None,  # AST 判定（_scan_subprocess_text_without_encoding）で検出する
        description=(
            "`subprocess.run(..., text=True)` の `encoding=` 未指定"
            "（Windows 既定ロケール依存でデコード事故・Issue #3898）"
        ),
        strict=True,
    ),
    Pattern(
        name="posix_path_assumption",
        regex=None,  # 誤検知抑制のためカスタム判定（_scan_posix_path_assumption）で検出する
        description=(
            "payload パスに対する `/` 前提の正規表現・部分一致"
            "（Windows のバックスラッシュ区切りで不一致・`as_posix()` 正規化推奨・Issue #3898）"
        ),
        strict=True,
    ),
    Pattern(
        name="venv_bin_hardcode",
        # `".venv/bin"` 単体リテラル（末尾スラッシュなし）も後続で連結されるため検出する。
        # `.venv/binary` 等の別ディレクトリを拾わないよう `/` かクォートで終端を確認する。
        # `Path(".venv") / "bin"` のように `Path()` コンストラクタで閉じ括弧を挟む書き方も
        # 検出できるよう `\)*` でクォート直後の任意個の閉じ括弧を許容する（Issue #3936）。
        regex=re.compile(r"""["']\.venv["']\)*\s*/\s*["']bin["']|["']\.venv/bin(?=/|["'])"""),
        description=("`.venv/bin` 決め打ち（Windows は `.venv/Scripts` + `.exe`・Issue #3898）"),
        strict=True,
    ),
)

_PATTERNS_BY_NAME: dict[str, Pattern] = {p.name: p for p in PATTERNS}


EXCLUDE_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)

EXCLUDE_PATH_FRAGMENTS = ("projects/gas/",)

# 既存 Issue でカバー済みのファイル → 検出から除外する
ISSUE_COVERAGE: dict[str, str] = {
    "scripts/test-plan.sh": "#1050",  # 既に削除済み
}

SELF_BASENAME = "win_compat_scan.py"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "win-compat-scan",
        help="Windows ネイティブで動かない可能性のあるコードを検出する（#1092）",
        description=__doc__,
    )
    parser.add_argument(
        "--report",
        dest="report_path",
        type=Path,
        default=None,
        help="Markdown レポートの出力先（省略時は stdout のみ）",
    )
    parser.add_argument(
        "--root",
        dest="root",
        type=Path,
        default=None,
        help="検索ルート（省略時は git toplevel）",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Issue #3898 で追加した 3 パターン（subprocess_text_without_encoding /"
            " posix_path_assumption / venv_bin_hardcode）の検出時に exit 1 で失敗させる"
            "（未指定時は従来どおり常に exit 0 の情報提供モード）"
        ),
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    root = args.root.resolve() if args.root else _git_root()
    if root is None:
        print("ERROR: git toplevel を取得できませんでした。--root を指定してください。", file=sys.stderr)
        return 2
    findings = scan(root)
    summary = _summarize(findings)

    # stdout に出力する（Gherkin 受け入れ基準: "stdout に検出パターンごとの一覧が出力される"）
    print(_format_stdout(findings, summary))

    if args.report_path:
        report_md = _format_report_markdown(findings, summary, root)
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(report_md, encoding="utf-8")
        print(f"==> レポートを書き出しました: {args.report_path}", file=sys.stderr)

    if getattr(args, "strict", False):
        strict_findings = [f for f in findings if f[3].strict]
        if strict_findings:
            print(
                f"ERROR: --strict 検出: {len(strict_findings)} 件の Windows 非互換パターン"
                "（Issue #3898）が見つかりました。",
                file=sys.stderr,
            )
            return 1

    return 0


def scan(root: Path) -> list[tuple[str, int, str, Pattern]]:
    """ディレクトリを走査して非互換パターンを検出する."""
    findings: list[tuple[str, int, str, Pattern]] = []
    for path in _iter_target_files(root):
        rel = str(path.relative_to(root))
        if rel in ISSUE_COVERAGE:
            continue
        if path.name == SELF_BASENAME:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.splitlines()
        # strict パターンは Python コード構文の検出なので .py 以外（ドキュメントのコード例等）は
        # gate 対象にしない（PR #3916 codex レビュー指摘）
        is_python = path.suffix == ".py"
        for lineno, line in enumerate(lines, start=1):
            for pattern in PATTERNS:
                if pattern.strict and not is_python:
                    continue
                if pattern.regex is not None and pattern.regex.search(line):
                    findings.append((rel, lineno, line.rstrip(), pattern))
        if is_python:
            findings.extend(_scan_posix_path_assumption(rel, lines))
            findings.extend(_scan_subprocess_text_without_encoding(rel, text, lines))
    return findings


def _scan_subprocess_text_without_encoding(
    rel: str, text: str, lines: list[str]
) -> list[tuple[str, int, str, Pattern]]:
    """`subprocess.run(..., text=<truthy>)` の `encoding=` 未指定を AST で検出する（Issue #3898）.

    `text=<変数>` のような正規表現では拾えないケースも確実に検出するため、
    grep ではなく AST 判定を用いる。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    pattern = _PATTERNS_BY_NAME["subprocess_text_without_encoding"]
    findings: list[tuple[str, int, str, Pattern]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            continue
        has_text = False
        has_encoding = False
        for kw in node.keywords:
            if kw.arg == "text":
                is_false_literal = isinstance(kw.value, ast.Constant) and kw.value.value is False
                has_text = not is_false_literal
            elif kw.arg == "encoding":
                has_encoding = True
        if has_text and not has_encoding:
            lineno = node.lineno
            line_text = lines[lineno - 1].rstrip() if 0 < lineno <= len(lines) else ""
            findings.append((rel, lineno, line_text, pattern))
    return findings


_POSIX_PREFIX_REGEX_RE = re.compile(r"""re\.compile\(\s*r?["']\^(?P<prefix>[^"']*?)/""")
_POSIX_SUBSTRING_RE = re.compile(r"""["'](?:/[\w.\-]+)+/["']\s*in\s+(\w+)""")
_ASSIGN_RE = re.compile(r"""^\s*(\w+)\s*(?::[^=]+)?=(?!=)\s*(.*)$""")
# `<変数>.match(...)` 形式（適用先の実引数は `_first_call_arg()` で切り出す）
_REGEX_USE_RE = re.compile(r"""\b(\w+)\.(?:match|search|fullmatch)\(""")
# 変数へ束縛せずその場で適用する `re.compile(...).match(...)` 形式
_INLINE_REGEX_USE_RE = re.compile(r"""re\.compile\(.*?\)\s*\.\s*(?:match|search|fullmatch)\(""")


def _first_call_arg(text: str) -> str:
    """呼び出しの `(` 直後のテキストから最初の実引数を括弧の対応を見て切り出す."""
    depth = 0
    for index, char in enumerate(text):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return text[:index]
            depth -= 1
        elif char == "," and depth == 0:
            return text[:index]
    return text


def _is_normalized_arg(arg: str, normalized_vars: set[str]) -> bool:
    """正規表現の適用先が `as_posix()` 正規化済みかを判定する.

    単純変数（`rel`）は行順で追跡した `normalized_vars` を、
    式（`Path(file_path).as_posix()`）はその場の `.as_posix()` 呼び出しを見る。
    """
    arg = arg.strip()
    return arg in normalized_vars or ".as_posix()" in arg


def _scan_posix_path_assumption(rel: str, lines: list[str]) -> list[tuple[str, int, str, Pattern]]:
    """`/` 前提の正規表現・部分一致を検出する（Issue #3898・誤検知抑制は #3916 レビュー対応）.

    `rel = Path(...).as_posix()` のように正規化済みの変数に対する
    `"/foo/" in rel` は Windows でも一致するため誤検知にしない
    （PR #3916 codex レビュー指摘: as_posix() 正規化済みなら 0 件）。

    正規化状態は**行順**（使用行より前にある直近の代入が勝つ）で判定する。
    ファイル全体を先読みして判定すると、使用行より後ろの `.as_posix()` 代入や
    未正規化な再代入で検出が抑制され gate をすり抜ける（PR #3916 codex レビュー指摘）。

    `re.compile(r"^https://...")` のように `^` から最初の `/` までがスキーム区切り `:`
    で終わる正規表現は URL 用途でパス区切りではないため検出しない
    （PR #3916 codex レビュー指摘: URL 正規表現で strict gate が誤ブロックする）。

    `TARGET_RE = re.compile(r"^projects/py/")` のように変数へ束縛した prefix 正規表現は、
    同一ファイル内の `TARGET_RE.match(<変数>)` 等の適用先が**すべて**正規化済み変数なら
    検出しない（PR #3916 codex レビュー指摘: 受け入れ基準「as_posix() 正規化済みなら 0 件」）。
    適用箇所が 1 つも無い・未正規化の変数や式に適用している場合は用途を確認できないため
    従来どおり検出する（抑制のやりすぎ防止）。

    `re.compile(r"^projects/py/").match(rel)` のように変数へ束縛せずその場で適用する
    prefix 正規表現も、適用先が正規化済み変数なら同様に検出しない
    （PR #3916 codex レビュー指摘: inline 形式で適用先を確認せず即 finding 化していた）。

    適用先は単純変数に限らず `.match(Path(file_path).as_posix())` のような**式**でも
    正規化済みとみなす（PR #3916 codex レビュー指摘: 正規化式を直接渡すと誤検出していた）。
    """
    pattern = _PATTERNS_BY_NAME["posix_path_assumption"]
    normalized_vars: set[str] = set()
    findings: list[tuple[str, int, str, Pattern]] = []
    # 変数へ束縛した prefix 正規表現の検出候補（変数名 -> finding）と、その適用先の正規化状況
    pending_prefix: dict[str, tuple[str, int, str, Pattern]] = {}
    regex_uses: dict[str, list[bool]] = defaultdict(list)
    for lineno, line in enumerate(lines, start=1):
        assign = _ASSIGN_RE.match(line)
        if assign:
            var, rhs = assign.group(1), assign.group(2)
            if ".as_posix()" in rhs:
                normalized_vars.add(var)
            else:
                normalized_vars.discard(var)
        for use in _REGEX_USE_RE.finditer(line):
            arg = _first_call_arg(line[use.end() :])
            regex_uses[use.group(1)].append(_is_normalized_arg(arg, normalized_vars))
        prefix_match = _POSIX_PREFIX_REGEX_RE.search(line)
        if prefix_match and not prefix_match.group("prefix").endswith(":"):
            inline_use = _INLINE_REGEX_USE_RE.search(line)
            if inline_use and _is_normalized_arg(_first_call_arg(line[inline_use.end() :]), normalized_vars):
                # 正規化済みの適用先なので Windows でも一致する
                continue
            finding = (rel, lineno, line.rstrip(), pattern)
            if assign:
                pending_prefix[assign.group(1)] = finding
            else:
                findings.append(finding)
            continue
        match = _POSIX_SUBSTRING_RE.search(line)
        if match and match.group(1) not in normalized_vars:
            findings.append((rel, lineno, line.rstrip(), pattern))
    for var, finding in pending_prefix.items():
        uses = regex_uses.get(var, [])
        if uses and all(uses):
            continue
        findings.append(finding)
    findings.sort(key=lambda f: f[1])
    return findings


def _iter_target_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in EXCLUDE_DIR_NAMES for part in rel_parts):
            continue
        rel_str = "/".join(rel_parts)
        if any(rel_str.startswith(frag) for frag in EXCLUDE_PATH_FRAGMENTS):
            continue
        yield path


def _summarize(findings: list[tuple[str, int, str, Pattern]]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for _, _, _, pattern in findings:
        counter[pattern.name] += 1
    return counter


def _format_stdout(
    findings: list[tuple[str, int, str, Pattern]],
    summary: Counter[str],
) -> str:
    lines = ["==> Windows 非互換コード スキャン結果", ""]
    by_pattern: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for rel, lineno, line, pattern in findings:
        by_pattern[pattern.name].append((rel, lineno, line))
    for pattern in PATTERNS:
        entries = by_pattern.get(pattern.name, [])
        if not entries:
            continue
        lines.append(f"## {pattern.name}: {pattern.description}")
        for rel, lineno, line in entries[:20]:
            lines.append(f"  {rel}:{lineno}:{line[:140]}")
        if len(entries) > 20:
            lines.append(f"  ...（残り {len(entries) - 20} 件）")
        lines.append("")
    lines.append("---")
    lines.append(f"検出合計: {sum(summary.values())} 件 / パターン数: {len(summary)}")
    for name, count in summary.most_common():
        lines.append(f"  - {name}: {count}")
    return "\n".join(lines)


def _format_report_markdown(
    findings: list[tuple[str, int, str, Pattern]],
    summary: Counter[str],
    root: Path,
) -> str:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# Windows 非互換コード 棚卸しレポート",
        "",
        f"> 生成日時: {ts}",
        f"> 検索ルート: `{root}`",
        f"> 検出パターン数: {len(PATTERNS)}",
        f"> 検出合計: {sum(summary.values())} 件",
        "",
        "## 概要（パターン別件数）",
        "",
        "| パターン | 件数 | 説明 |",
        "|---------|------:|------|",
    ]
    for pattern in PATTERNS:
        count = summary.get(pattern.name, 0)
        lines.append(f"| `{pattern.name}` | {count} | {pattern.description} |")
    lines.append("")

    by_pattern: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for rel, lineno, line, pattern in findings:
        by_pattern[pattern.name].append((rel, lineno, line))

    for pattern in PATTERNS:
        entries = by_pattern.get(pattern.name, [])
        if not entries:
            continue
        lines.append(f"## `{pattern.name}`")
        lines.append("")
        lines.append(pattern.description)
        lines.append("")
        for rel, lineno, line in entries:
            lines.append(f"- `{rel}:{lineno}` `{line[:200]}`")
        lines.append("")

    lines.append("## 対応方針")
    lines.append("")
    lines.append("各検出について以下のいずれかを判定する:")
    lines.append("")
    lines.append("1. **Issue 化** — Windows 対応が必要な実利用シナリオがある")
    lines.append("2. **受容（WSL 限定継続）** — WSL でのみ動けば十分な機能")
    lines.append("3. **既にカバー済み** — 親 #1090 配下の他 Issue で対応中")
    lines.append("")
    lines.append("子 Issue 起票は本 Issue 完了後に別途実施する。")
    return "\n".join(lines)


def _git_root() -> Path | None:
    try:
        return git_client.rev_parse_show_toplevel()
    except GitCommandError:
        return None
