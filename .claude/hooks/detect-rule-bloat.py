#!/usr/bin/env python3
"""PostToolUse hook: .claude/rules/*.md と CLAUDE.md の肥大化パターンを静的検出して warning を出す.

Issue #1772・#1882。`.claude/rules/*.md` と `CLAUDE.md` は毎セッション全ロードされるため、
肥大化するとper-session トークン消費量が増大する。以下の 3 種類のパターンを検出して警告する:

1. 行数上限超過: ファイル行数が LINE_LIMIT を超えると垂直分割を促す（`.claude/rules/*.md` のみ）
2. 敗戦記録キーワード: `.claude/rules/rule-bloat-keywords.yaml` に定義したキーワードが
   本文に含まれていると docs/decisions/ / docs/research/ への退避を促す（`.claude/rules/*.md` のみ）
3. コンテキスト予算チェック: Issue #1882。CLAUDE.md + .claude/rules/*.md の est tokens 合計、
   および単体ファイルの est が `.claude/rules/context-budget.yaml` の閾値を超えるか監視する

どちらも **exit 0（非 blocking・警告のみ）**。開発フローを止めない設計。

stdlib のみ使用（uv run オーバーヘッドを避ける）。

対象: Edit / Write ツールで `.claude/rules/*.md` または `CLAUDE.md` を保存した直後
対象外: `.claude/rules/` 以外のファイル（即 exit 0）
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.hook_io import (  # noqa: E402
    get_file_path,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)

_HOOK_NAME = "detect-rule-bloat"

# 行数上限（これを超えると垂直分割を促す）
LINE_LIMIT = 500

# キーワード辞書 YAML のデフォルトパス（RULE_BLOAT_KEYWORDS_YAML 環境変数でテスト時にオーバーライド可能）
_KEYWORDS_YAML_DEFAULT = (
    Path(__file__).resolve().parent.parent / "rules" / "rule-bloat-keywords.yaml"
)

# コンテキスト予算 YAML のデフォルトパス（CONTEXT_BUDGET_YAML 環境変数でテスト時にオーバーライド可能）
_CONTEXT_BUDGET_YAML_DEFAULT = (
    Path(__file__).resolve().parent.parent / "rules" / "context-budget.yaml"
)


def _get_keywords_yaml() -> Path:
    override = os.environ.get("RULE_BLOAT_KEYWORDS_YAML")
    if override:
        return Path(override)
    return _KEYWORDS_YAML_DEFAULT


def _is_rules_md(file_path: str) -> bool:
    """.claude/rules/*.md のパスか判定する（サブディレクトリは対象外）.

    パス末尾が `.claude/rules/<file>.md` の構造であることを確認する。
    複数の `.claude` セグメントが含まれるパスでも末尾から逆算することで正確に判定する。
    """
    p = Path(file_path)
    # .md ファイルのみ対象
    if p.suffix != ".md":
        return False
    # 末尾から「ファイル名 / rules / .claude」の順序であることを確認する
    # parts[-1] = <file>.md, parts[-2] = rules, parts[-3] = .claude
    parts = p.parts
    if len(parts) < 3:
        return False
    return parts[-2] == "rules" and parts[-3] == ".claude"


def _is_repo_claude_md(file_path: str) -> bool:
    """リポジトリルート直下の CLAUDE.md か判定する（同じディレクトリに .claude がある）."""
    p = Path(file_path)
    return p.name == "CLAUDE.md" and (p.parent / ".claude").is_dir()


def _load_keywords() -> list[dict[str, str]]:
    """rule-bloat-keywords.yaml からキーワードリストを読み込む（stdlib のみ）."""
    yaml_path = _get_keywords_yaml()
    if not yaml_path.is_file():
        return []
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return []
    # stdlib のみで YAML をパース（キーワードリストのみ対応する簡易パーサー）
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- id:"):
            if current:
                entries.append(current)
            current = {"id": stripped[len("- id:") :].strip()}
        elif stripped.startswith("id:") and not current:
            current = {"id": stripped[len("id:") :].strip()}
        elif ":" in stripped and current:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip("'\"")
            if key in ("category", "regex", "reason", "recommend"):
                current[key] = val
    if current:
        entries.append(current)
    return [e for e in entries if "id" in e and "regex" in e]


def _check_line_count(file_path: str) -> str | None:
    """ファイル行数が LINE_LIMIT を超えていれば warning メッセージを返す."""
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except OSError:
        return None
    line_count = len(text.splitlines())
    if line_count > LINE_LIMIT:
        name = Path(file_path).name
        return (
            f"WARN: rules/{name} が {LINE_LIMIT} 行を超えています。垂直分割を検討してください。\n"
            f"（現在: {line_count} 行 / 上限: {LINE_LIMIT} 行）\n"
            "詳細: docs/reference/pr-splitting-guide.md の「Phase 内垂直分割の判断基準」を参照"
        )
    return None


def _check_keywords(file_path: str, keywords: list[dict[str, str]]) -> list[str]:
    """敗戦記録キーワードが本文に含まれていれば warning メッセージのリストを返す."""
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except OSError:
        return []
    name = Path(file_path).name
    warnings: list[str] = []
    seen_ids: set[str] = set()
    for kw in keywords:
        kw_id = kw.get("id", "")
        if kw_id in seen_ids:
            continue
        regex = kw.get("regex", "")
        if not regex:
            continue
        try:
            pattern = re.compile(regex, re.IGNORECASE)
        except re.error:
            continue
        m = pattern.search(text)
        if m:
            seen_ids.add(kw_id)
            matched_word = m.group(0)
            recommend = kw.get("recommend", "")
            reason = kw.get("reason", "")
            msg = (
                f'WARN: rules/{name} に decision-journal 候補記述（"{matched_word}"）が含まれます。\n'
                f"      {reason}\n"
            )
            if recommend:
                msg += f"      推奨退避先: {recommend}\n"
            warnings.append(msg)
    return warnings


def _estimate_tokens(text: str) -> float:
    """est_tokens = 非ASCII文字数 + ASCII文字数/4（保守的方式・決定的）."""
    non_ascii = sum(1 for c in text if ord(c) >= 128)
    return non_ascii + (len(text) - non_ascii) / 4


def _get_context_budget_yaml() -> Path:
    """コンテキスト予算 YAML のパスを取得（環境変数でオーバーライド可能）."""
    override = os.environ.get("CONTEXT_BUDGET_YAML")
    if override:
        return Path(override)
    return _CONTEXT_BUDGET_YAML_DEFAULT


def _parse_context_budget_yaml(yaml_path: Path) -> dict[str, int]:
    """context-budget.yaml をパースして {total_warn, file_warn} を返す（stdlib のみ）."""
    if not yaml_path.is_file():
        return {}
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    result: dict[str, int] = {}
    for line in text.splitlines():
        # コメント除去
        line = line.split("#")[0].strip()
        if not line:
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if key in ("total_warn", "file_warn"):
                try:
                    result[key] = int(val)
                except ValueError:
                    pass
    return result


def _get_repo_root(file_path: str) -> Path:
    """ファイルパスからリポジトリルートを導出する.

    .claude/rules/*.md なら `.claude` の親、CLAUDE.md なら親ディレクトリ。
    """
    p = Path(file_path)
    if _is_rules_md(file_path):
        # .claude/rules/ -> .claude -> repo_root
        return p.parents[2]
    elif _is_repo_claude_md(file_path):
        # CLAUDE.md -> repo_root
        return p.parent
    return p.parent


def _check_context_budget(file_path: str) -> list[str]:
    """コンテキスト予算チェック（総量 + 単体）."""
    warnings: list[str] = []
    yaml_path = _get_context_budget_yaml()

    # YAML が存在しない場合
    if not yaml_path.is_file():
        msg = "WARN: context-budget.yaml not found（予算チェックをスキップします）"
        warnings.append(msg)
        return warnings

    budget = _parse_context_budget_yaml(yaml_path)
    if not budget or "total_warn" not in budget or "file_warn" not in budget:
        return warnings

    total_warn = budget["total_warn"]
    file_warn = budget["file_warn"]
    repo_root = _get_repo_root(file_path)

    # 単体ファイルチェック（編集対象ファイル）
    try:
        file_text = Path(file_path).read_text(encoding="utf-8")
        file_est = _estimate_tokens(file_text)
        if file_est > file_warn:
            rel_path = Path(file_path).name
            msg = (
                f"WARN: context budget 超過 — {rel_path} 単体 {file_est:.2f} est tokens"
                f"（WARN 閾値: {file_warn}）。"
            )
            warnings.append(msg)
    except OSError:
        pass

    # 総量チェック（CLAUDE.md + .claude/rules/*.md）
    try:
        total_est = 0.0

        # CLAUDE.md
        claude_md = repo_root / "CLAUDE.md"
        if claude_md.is_file():
            claude_text = claude_md.read_text(encoding="utf-8")
            total_est += _estimate_tokens(claude_text)

        # .claude/rules/*.md（サブディレクトリ除外）
        rules_dir = repo_root / ".claude" / "rules"
        if rules_dir.is_dir():
            for rule_file in rules_dir.glob("*.md"):
                if rule_file.is_file():
                    rule_text = rule_file.read_text(encoding="utf-8")
                    total_est += _estimate_tokens(rule_text)

        if total_est > total_warn:
            msg = (
                f"WARN: context budget 超過 — 常時ロード合計 {total_est:.2f} est tokens"
                f"（WARN 閾値: {total_warn}）。docs/reference/ への退避を検討してください。"
            )
            warnings.append(msg)
    except OSError:
        pass

    return warnings


def _main() -> int:
    payload = read_hook_input(hook_name="PostToolUse")

    tool_name = get_tool_name(payload)
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return 0

    file_path = get_file_path(payload)
    if not file_path:
        return 0

    # .claude/rules/*.md または CLAUDE.md 以外は即 exit 0（対象外）
    is_rules = _is_rules_md(file_path)
    is_claude_md = _is_repo_claude_md(file_path)
    if not (is_rules or is_claude_md):
        return 0

    # ファイルが実際に存在しない場合はスキップ（削除操作など）
    if not Path(file_path).exists():
        return 0

    warnings: list[str] = []

    # チェック 1 & 2: 行数上限と敗戦記録キーワード（.claude/rules/*.md のみ）
    if is_rules:
        line_warn = _check_line_count(file_path)
        if line_warn:
            warnings.append(line_warn)

        keywords = _load_keywords()
        keyword_warns = _check_keywords(file_path, keywords)
        warnings.extend(keyword_warns)

    # チェック 3: コンテキスト予算（両方の場合に実行）
    budget_warns = _check_context_budget(file_path)
    warnings.extend(budget_warns)

    for w in warnings:
        print(w, file=sys.stderr)

    # 非 blocking: warning のみ出して exit 0
    return 0


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled(_HOOK_NAME):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
