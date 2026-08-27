"""`tidd context-budget` サブコマンド (Issue #1883・親 #1872).

常時ロードコンテキスト（CLAUDE.md + `.claude/rules/*.md` + CLAUDE.md の
`@<path>` import を再帰解決した先・#3643）のファイル別 est tokens 内訳と合計を
stdout に表示する。`--check` で `.claude/rules/context-budget.yaml` の
HARD 閾値（total_hard / file_hard・ラチェット暫定値）超過を exit 1 でブロックする。

est_tokens = 0.923 * 非ASCII文字数 + 0.547 * ASCII文字数
（#3642 で `/context` 実測 9 ファイルへの最小二乗法フィットにより校正した
 実トークナイザ近似。誤差最大約 6% を含む・係数は暫定値）。
`.claude/hooks/detect-rule-bloat.py` の WARN 閾値と同じ算出式（WARN は編集時警告・
本コマンドは PR gate の機械強制）。運用手順: docs/reference/context-budget.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from tidd_tools.shared import git_client
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GitCommandError

CONTEXT_BUDGET_YAML = ".claude/rules/context-budget.yaml"

# 2026-08-10 の `/context` Memory files 内訳（9 ファイル）への最小二乗法フィットで導出。
# 旧式（非ASCII 1 = 1 token・ASCII 4 = 1 token）は実トークンを約 27% 過小評価していた。
NON_ASCII_TOKENS_PER_CHAR = 0.923
ASCII_TOKENS_PER_CHAR = 0.547


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "context-budget",
        help="常時ロードコンテキストの est tokens 内訳表示と HARD 閾値チェック（Issue #1883）",
        description=(
            "CLAUDE.md + .claude/rules/*.md のファイル別 est tokens 内訳と合計を表示する。"
            "--check で context-budget.yaml の HARD 閾値（total_hard / file_hard）超過を"
            " exit 1 でブロックする（ラチェット式 PR gate）。"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="HARD 閾値（total_hard / file_hard）超過で exit 1",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="リポジトリルート（省略時は git rev-parse --show-toplevel）",
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def estimate_tokens(text: str) -> float:
    """est tokens を実トークナイザ近似で見積もる（#3642 で校正）.

    非ASCII 1 文字 ≈ 0.923 token・ASCII 1 文字 ≈ 0.547 token（独立係数）。
    実トークナイザの近似であり、2026-08-10 の `/context` 実測との突き合わせで
    最大約 6% の誤差を含む。係数は暫定値なので、再校正は `docs/reference/context-budget.md`
    の手順に従い `/context` の Memory files 内訳を再取得して最小二乗法で確定する。
    """
    non_ascii = sum(1 for c in text if ord(c) >= 128)
    ascii_count = len(text) - non_ascii
    return NON_ASCII_TOKENS_PER_CHAR * non_ascii + ASCII_TOKENS_PER_CHAR * ascii_count


def resolve_import_targets(repo_root: Path) -> set[str]:
    """CLAUDE.md の `@<path>` import を再帰的に解決し、repo 相対パスの集合を返す (#3643).

    - 相対パス（親ディレクトリ参照 `..` を含む）を repo ルート基準の相対パスへ正規化する
    - 循環 import・存在しないパス・リポジトリ外を指す import は stderr に警告を出して
      スキップする（計測を中断しない）
    """
    result: set[str] = set()
    claude_md = repo_root / "CLAUDE.md"
    if not claude_md.is_file():
        return result
    visited: set[Path] = {claude_md.resolve()}
    _resolve_imports_from_file(claude_md, repo_root, visited, result)
    return result


def _resolve_imports_from_file(
    source: Path,
    repo_root: Path,
    visited: set[Path],
    result: set[str],
) -> None:
    """source 内の `@<path>` import を解決し、再帰的に辿る（#3643）."""
    repo_root_r = repo_root.resolve()
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("@"):
            continue
        rest = stripped[1:].strip()
        if not rest:
            continue
        import_path = rest.split()[0]
        resolved = (source.parent / import_path).resolve()
        try:
            rel = resolved.relative_to(repo_root_r)
        except ValueError:
            print(
                f"context-budget: import がリポジトリ外を指しています: {import_path}"
                f"（{source.relative_to(repo_root)} から）",
                file=sys.stderr,
            )
            continue
        rel_str = rel.as_posix()
        if resolved in visited:
            print(
                f"context-budget: 循環 import を検出しました: {rel_str}（{source.relative_to(repo_root)} から）",
                file=sys.stderr,
            )
            continue
        if not resolved.is_file():
            print(
                f"context-budget: import 先が存在しません: {rel_str}（{source.relative_to(repo_root)} から）",
                file=sys.stderr,
            )
            continue
        visited.add(resolved)
        result.add(rel_str)
        _resolve_imports_from_file(resolved, repo_root, visited, result)


def is_gate_trigger(path: str, repo_root: Path | None = None) -> bool:
    """PR の変更ファイルが予算ゲートのトリガー対象か.

    CLAUDE.md / `.claude/rules/` 配下に加え、repo_root 指定時は CLAUDE.md の
    `@import` 再帰解決で得た import 先もトリガー対象とする（#3643）。
    """
    if path == "CLAUDE.md" or path.startswith(".claude/rules/"):
        return True
    if repo_root is not None:
        return path in resolve_import_targets(repo_root)
    return False


def collect_breakdown(repo_root: Path) -> list[tuple[str, float]]:
    """常時ロードファイルの (repo 相対パス, est tokens) を CLAUDE.md → rules 名前順で返す.

    CLAUDE.md / `.claude/rules/*.md` に加え、CLAUDE.md の `@import` を再帰解決した
    先のファイルも内訳・合計に含める（#3643）。
    """
    breakdown: list[tuple[str, float]] = []
    claude_md = repo_root / "CLAUDE.md"
    if claude_md.is_file():
        breakdown.append(("CLAUDE.md", estimate_tokens(claude_md.read_text(encoding="utf-8"))))
    rules_dir = repo_root / ".claude" / "rules"
    if rules_dir.is_dir():
        for f in sorted(rules_dir.glob("*.md")):
            breakdown.append((f".claude/rules/{f.name}", estimate_tokens(f.read_text(encoding="utf-8"))))
    # import 先（上記で計上済みの CLAUDE.md / .claude/rules は除外・二重計上防止）
    for rel in sorted(resolve_import_targets(repo_root)):
        if rel == "CLAUDE.md" or rel.startswith(".claude/rules/"):
            continue
        target = repo_root / rel
        if target.is_file():
            breakdown.append((rel, estimate_tokens(target.read_text(encoding="utf-8"))))
    return breakdown


def check_breakdown(repo_root: Path, breakdown: list[tuple[str, float]]) -> int:
    """HARD 閾値チェック。超過・設定エラーは stderr に出力して 1 を返す."""
    yaml_path = repo_root / CONTEXT_BUDGET_YAML
    if not yaml_path.is_file():
        print(f"ERROR: context-budget.yaml not found: {yaml_path}", file=sys.stderr)
        return 1
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    total_hard = data.get("total_hard")
    file_hard = data.get("file_hard")
    if not isinstance(total_hard, (int, float)) or not isinstance(file_hard, (int, float)):
        print(
            f"ERROR: {yaml_path} に total_hard / file_hard が定義されていません（ラチェット閾値の設定が必要）",
            file=sys.stderr,
        )
        return 1

    exceeded = False
    for path, est in breakdown:
        if est > file_hard:
            print(
                f"ERROR: context budget exceeded: {path} = {est:.2f} est tokens"
                f"（file_hard {file_hard} を {est - file_hard:.2f} 超過）",
                file=sys.stderr,
            )
            exceeded = True
    total = sum(est for _, est in breakdown)
    if total > total_hard:
        print(
            f"ERROR: context budget exceeded: 合計 {total:.2f} est tokens"
            f"（total_hard {total_hard} を {total - total_hard:.2f} 超過）",
            file=sys.stderr,
        )
        exceeded = True

    if exceeded:
        print(
            "=> docs/reference/context-budget.md のラチェット手順に従い、"
            "本文を docs/reference/ へ退避して削減してください（閾値引き上げは原則禁止）。",
            file=sys.stderr,
        )
        return 1
    return 0


def run_gate(repo_root: Path) -> int:
    """test-plan の PR gate から呼ぶ in-process チェック（内訳出力なし・エラーのみ stderr）."""
    return check_breakdown(repo_root, collect_breakdown(repo_root))


def run_cli(args: argparse.Namespace) -> int:
    if args.repo_root is not None:
        repo_root = args.repo_root
    else:
        try:
            repo_root = git_client.rev_parse_show_toplevel()
        except GitCommandError as exc:
            print(f"ERROR: git rev-parse に失敗しました: {exc}", file=sys.stderr)
            return 2

    breakdown = collect_breakdown(repo_root)
    total = sum(est for _, est in breakdown)

    if args.json_output:
        print(
            json.dumps(
                {
                    "files": [{"path": p, "est_tokens": round(e, 2)} for p, e in breakdown],
                    "total_est_tokens": round(total, 2),
                },
                ensure_ascii=False,
            )
        )
    else:
        width = max((len(p) for p, _ in breakdown), default=10)
        for p, e in breakdown:
            print(f"{p:<{width}}  {e:>10.2f}")
        print(f"{'-' * width}  {'-' * 10}")
        print(f"{'合計':<{width}}  {total:>10.2f}")

    if not args.check:
        return 0
    return check_breakdown(repo_root, breakdown)
