"""skill_lint - SKILL.md 品質検証ロジックのvendored コピー（stdlib のみ）.

`.claude/hooks/validate-skill.py` の PreToolUse hook が consumer リポジトリ
（copier 配布先）でも機能するように、`tidd_tools.skill_checker` の判定ロジックを
stdlib のみで再実装したファイル。

`projects/py/tidd_tools/src/tidd_tools/skill_checker.py` と同一の判定ロジックを持ち、
tidd_tools パッケージが利用できない環境でも import できる。

Issue #2151
"""

from __future__ import annotations

import re
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


def split_frontmatter(content: str) -> tuple[str, str]:
    """YAML frontmatter と body を分割する.

    Returns:
        (frontmatter, body) のタプル。frontmatter がない場合は ("", content)。
    """
    if not content.startswith("---"):
        return "", content
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
        if url.endswith(".md") and not url.startswith(("http://", "https://", "/")):
            links.append(url)
    return links


def check_reference_depth(content: str, file_path: str) -> list[str]:
    """SKILL.md が参照するファイルが 1 階層以内かどうかをチェックする.

    SKILL.md → a.md（OK）
    SKILL.md → a.md → b.md（NG: 2 階層）
    """
    errors: list[str] = []

    skill_refs = extract_md_links(content)
    if not skill_refs:
        return errors

    skill_path = Path(file_path)
    if not skill_path.is_absolute():
        skill_path = Path.cwd() / skill_path
    skill_dir = skill_path.parent

    for ref_link in skill_refs:
        ref_path = skill_dir / ref_link
        if not ref_path.is_file():
            continue

        try:
            ref_content = ref_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        nested_refs = extract_md_links(ref_content)
        if nested_refs:
            errors.append(
                f"参照は 1 階層以内に保ってください。\n"
                f"SKILL.md → {ref_link} → {nested_refs[0]} は 2 階層の参照になります。\n"
                f"詳細: docs/reference/skill-authoring-rules.md"
            )
            break

    return errors


def check_skill_md(content: str, file_path: str) -> list[str]:
    """SKILL.md のチェックを実行してエラーメッセージのリストを返す.

    エラーがない場合は空リストを返す。
    """
    errors: list[str] = []

    frontmatter_text, body = split_frontmatter(content)
    fm = parse_frontmatter(frontmatter_text)

    body_line_count = len(body.strip().splitlines())
    if body_line_count >= SKILL_BODY_MAX_LINES:
        errors.append(
            f"SKILL.md body が 500 行を超えています（現在: {body_line_count} 行）。\n"
            f"Progressive Disclosure パターンで分割してください。\n"
            f"詳細: docs/reference/skill-authoring-rules.md"
        )

    name = fm.get("name", "")
    if not name:
        errors.append("SKILL.md の YAML frontmatter に name フィールドが必要です。\n例: name: my-skill")
    else:
        if len(name) > NAME_MAX_CHARS:
            errors.append(f"name が {NAME_MAX_CHARS} 文字を超えています（現在: {len(name)} 文字）。")
        if not _NAME_RE.match(name):
            errors.append(f"name は小文字英字・数字・ハイフンのみ使用できます（現在: {name!r}）。")
        for reserved in RESERVED_NAME_WORDS:
            if reserved in name:
                errors.append(
                    f"reserved word {reserved!r} を name に含めることはできません（現在: {name!r}）。\n"
                    f"Anthropic 公式ベストプラクティスの禁止事項です。"
                )

    description = fm.get("description", "")
    if not description:
        errors.append(
            "SKILL.md の YAML frontmatter に description フィールドが必要です。\n"
            "例: description: Does something when needed"
        )
    elif len(description) > DESCRIPTION_MAX_CHARS:
        errors.append(f"description が {DESCRIPTION_MAX_CHARS} 文字を超えています（現在: {len(description)} 文字）。")

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
