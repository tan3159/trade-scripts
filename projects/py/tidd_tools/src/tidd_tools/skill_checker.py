"""`skill_checker` - SKILL.md 品質検証の共通モジュール（Issue #2112）.

`.claude/hooks/validate-skill.py`（PreToolUse hook）と `test_plan.py`（PR ゲート）の
両方から共通化された検証ロジックを参照できるようにする。

`context_budget.py` が `estimate_tokens`/`is_gate_trigger`/`run_gate` を公開している
パターンを踏襲する。

判定対象の定数・関数:
  - SKILL_BODY_MAX_LINES: SKILL.md body の最大行数
  - is_gate_trigger(path): PR 変更ファイルが skill ゲートのトリガー対象か
  - check_skill_md(content, file_path): SKILL.md のチェックを実行してエラーリストを返す
  - check_subfile(content): サブファイルのチェックを実行してエラーリストを返す
  - run_gate(repo_root, changed_files): PR ゲートを実行して exit code を返す
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# SKILL.md のボディ最大行数（これ未満が要件）
SKILL_BODY_MAX_LINES = 500

# サブファイルで TOC が必須になる行数閾値（超えると TOC が必要）
SUBFILE_TOC_REQUIRED_LINES = 100

# name の最大文字数
NAME_MAX_CHARS = 64

# description の最大文字数
DESCRIPTION_MAX_CHARS = 1024

# name に使えない予約語
RESERVED_NAME_WORDS = ("anthropic", "claude")

# name の正規表現（lowercase + digits + hyphen only）
_NAME_RE = re.compile(r"^[a-z0-9-]+$")

# Markdown リンクを抽出する正規表現（[text](url)）
_MD_LINK_RE = re.compile(r"\[(?:[^\[\]]*)\]\(([^)]+)\)")

# TOC 見出しパターン（## Contents / ## 目次 / ## Table of Contents / ## TOC 等）
_TOC_HEADING_RE = re.compile(
    r"^##\s+(?:Contents|目次|Table\s+of\s+Contents|TOC)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def is_gate_trigger(path: str) -> bool:
    """PR の変更ファイルが skill ゲートのトリガー対象か（.claude/skills/ 配下の .md）."""
    return path.startswith(".claude/skills/") and path.endswith(".md")


def split_frontmatter(content: str) -> tuple[str, str]:
    """YAML frontmatter と body を分割する.

    Returns:
        (frontmatter, body) のタプル。frontmatter がない場合は ("", content)。
    """
    if not content.startswith("---"):
        return "", content
    # 2 番目の --- を探す
    rest = content[3:]
    end_idx = rest.find("\n---")
    if end_idx == -1:
        return "", content
    frontmatter = rest[: end_idx + 1]
    body = rest[end_idx + 4 :]  # "\n---" の後
    return frontmatter, body


def parse_frontmatter(frontmatter: str) -> dict[str, str]:
    """YAML frontmatter から key: value ペアを抽出する（stdlib 実装）.

    完全な YAML パースではなく、シンプルな key: value 抽出のみ対応。
    """
    result: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if key:
                result[key] = value
    return result


def extract_md_links(content: str) -> list[str]:
    """Markdown テキストから相対 .md リンクを抽出する."""
    links = []
    for m in _MD_LINK_RE.finditer(content):
        url = m.group(1)
        # 相対パスの .md リンクのみ対象（外部 URL・アンカーは除く）
        if url.endswith(".md") and not url.startswith(("http://", "https://", "/")):
            links.append(url)
    return links


def check_reference_depth(content: str, file_path: str) -> list[str]:
    """SKILL.md が参照するファイルが 1 階層以内かどうかをチェックする.

    SKILL.md → a.md（OK）
    SKILL.md → a.md → b.md（NG: 2 階層）
    """
    errors: list[str] = []

    # SKILL.md が参照する .md リンクを収集
    skill_refs = extract_md_links(content)
    if not skill_refs:
        return errors

    # SKILL.md の親ディレクトリを解決
    skill_path = Path(file_path)
    if not skill_path.is_absolute():
        skill_path = Path.cwd() / skill_path
    skill_dir = skill_path.parent

    for ref_link in skill_refs:
        ref_path = skill_dir / ref_link
        if not ref_path.is_file():
            # 参照先が存在しない場合はチェックをスキップ（まだ作成中の可能性）
            continue

        try:
            ref_content = ref_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        # 参照先ファイルがさらに .md リンクを持つ場合は 2 階層として NG
        nested_refs = extract_md_links(ref_content)
        if nested_refs:
            errors.append(
                f"参照は 1 階層以内に保ってください。\n"
                f"SKILL.md → {ref_link} → {nested_refs[0]} は 2 階層の参照になります。\n"
                f"詳細: docs/reference/skill-authoring-rules.md"
            )
            break  # 1 件見つかれば十分

    return errors


def check_skill_md(content: str, file_path: str) -> list[str]:
    """SKILL.md のチェックを実行してエラーメッセージのリストを返す.

    エラーがない場合は空リストを返す。
    """
    errors: list[str] = []

    frontmatter_text, body = split_frontmatter(content)
    fm = parse_frontmatter(frontmatter_text)

    # チェック 1: body が 500 行未満（先頭・末尾の空行を除いてカウント）
    body_line_count = len(body.strip().splitlines())
    if body_line_count >= SKILL_BODY_MAX_LINES:
        errors.append(
            f"SKILL.md body が 500 行を超えています（現在: {body_line_count} 行）。\n"
            f"Progressive Disclosure パターンで分割してください。\n"
            f"詳細: docs/reference/skill-authoring-rules.md"
        )

    # チェック 2: name が存在する
    name = fm.get("name", "")
    if not name:
        errors.append("SKILL.md の YAML frontmatter に name フィールドが必要です。\n例: name: my-skill")
    else:
        # name の文字数チェック
        if len(name) > NAME_MAX_CHARS:
            errors.append(f"name が {NAME_MAX_CHARS} 文字を超えています（現在: {len(name)} 文字）。")
        # name の文字種チェック（lowercase + digits + hyphen only）
        if not _NAME_RE.match(name):
            errors.append(f"name は小文字英字・数字・ハイフンのみ使用できます（現在: {name!r}）。")
        # reserved word チェック
        for reserved in RESERVED_NAME_WORDS:
            if reserved in name:
                errors.append(
                    f"reserved word {reserved!r} を name に含めることはできません（現在: {name!r}）。\n"
                    f"Anthropic 公式ベストプラクティスの禁止事項です。"
                )

    # チェック 3: description が存在する
    description = fm.get("description", "")
    if not description:
        errors.append(
            "SKILL.md の YAML frontmatter に description フィールドが必要です。\n"
            "例: description: Does something when needed"
        )
    elif len(description) > DESCRIPTION_MAX_CHARS:
        errors.append(f"description が {DESCRIPTION_MAX_CHARS} 文字を超えています（現在: {len(description)} 文字）。")

    # チェック 4: 参照が 1 階層以内
    depth_errors = check_reference_depth(content, file_path)
    errors.extend(depth_errors)

    return errors


def check_subfile(content: str) -> list[str]:
    """スキルサブファイルのチェックを実行してエラーメッセージのリストを返す."""
    errors: list[str] = []

    line_count = len(content.splitlines())
    if line_count > SUBFILE_TOC_REQUIRED_LINES and not _TOC_HEADING_RE.search(content):
        errors.append(
            f"100 行を超えるサブファイルには TOC（目次）が必要です（現在: {line_count} 行）。\n"
            "先頭近くに `## Contents` または `## 目次` 見出しを追加してください。\n"
            "詳細: docs/reference/skill-authoring-rules.md"
        )

    return errors


def run_gate(repo_root: Path, changed_files: list[str]) -> int:
    """PR ゲートを実行して exit code を返す.

    PR 変更ファイルに .claude/skills/**/*.md が含まれる場合、
    それぞれの SKILL.md と サブファイルを検証する。
    違反があれば stderr にエラーを出力して 1 を返す。
    問題がなければ 0 を返す。

    Args:
        repo_root: リポジトリルートの Path
        changed_files: PR 変更ファイルのリスト（repo 相対パス）
    """
    skill_files = [f for f in changed_files if is_gate_trigger(f)]
    if not skill_files:
        return 0

    all_errors: list[tuple[str, list[str]]] = []
    for rel_path in skill_files:
        abs_path = repo_root / rel_path
        if not abs_path.is_file():
            continue
        try:
            content = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        path_parts = Path(rel_path).parts
        # .claude/skills/<name>/SKILL.md かどうか判定
        is_skill_md = (
            len(path_parts) >= 4
            and path_parts[0] == ".claude"
            and path_parts[1] == "skills"
            and path_parts[-1] == "SKILL.md"
        )

        errors = check_skill_md(content, str(abs_path)) if is_skill_md else check_subfile(content)

        if errors:
            all_errors.append((rel_path, errors))

    if not all_errors:
        return 0

    print("validate-skill: SKILL.md の品質チェックに失敗しました（PR ゲート）。", file=sys.stderr)
    for file_path, errors in all_errors:
        print(f"対象: {file_path}", file=sys.stderr)
        for i, err in enumerate(errors, 1):
            print(f"[{i}] {err}", file=sys.stderr)
        print(file=sys.stderr)
    print(
        "詳細: docs/reference/skill-authoring-rules.md\n     docs/reference/hooks.md#validate-skillpy",
        file=sys.stderr,
    )
    return 1
