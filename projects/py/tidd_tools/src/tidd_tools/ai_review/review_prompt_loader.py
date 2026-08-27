"""`.claude/rules/review-prompt.yaml` を読み込んでプロンプト文字列を生成するローダ（Issue #1648）.

設計:
- `load_review_prompt(yaml_path)` — YAML をロードして schema 検証する。不正なら例外を raise。
- `render_common_header(yaml_path)` — load_review_prompt の結果を人間可読な文字列に変換して返す。

schema 要件:
  - `severity_rules` セクション（必須）
    - `severity_rules.definitions.CRITICAL` キー（必須）
    - `severity_rules.chain_of_thought` キー（必須・#3832: 敵対的検証 CoT）
    - `severity_rules.evidence_requirements` キー（必須・#3832: 反例提示義務）
  - `attempt_cycle_rules` セクション（必須）
    - `downgrade_to_medium` 内の `gherkin_cosmetic` / `doc_drift_unverified` /
      `speculative_exception` の各 rule は `exclusions` キー（除外条件・非空リスト）が
      必須（#4065: 実害のあるパターンの過剰ダウングレード防止）
  - `security_checklist` セクション（必須）
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

# Issue #4065: これらのカテゴリは実害のあるパターンと表面的に似ているため、
# 除外条件（exclusions）を必須フィールドとして schema で強制する。
_CATEGORIES_REQUIRING_EXCLUSIONS = frozenset({"gherkin_cosmetic", "doc_drift_unverified", "speculative_exception"})


def load_review_prompt(yaml_path: Path) -> dict[str, Any]:
    """`.claude/rules/review-prompt.yaml` を読み込み schema 検証した辞書を返す.

    Parameters
    ----------
    yaml_path:
        読み込む YAML ファイルのパス。

    Returns
    -------
    dict
        YAML をパースした辞書。

    Raises
    ------
    FileNotFoundError
        `yaml_path` が存在しない場合。メッセージに "review-prompt.yaml が見つかりません" を含む。
    ValueError
        必須セクション / キーが欠落している場合。
    """
    if not yaml_path.exists():
        msg = f"review-prompt.yaml が見つかりません: {yaml_path}"
        print(msg, file=sys.stderr)
        raise FileNotFoundError(msg)

    raw = yaml_path.read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw) or {}

    # --- schema 検証 ---

    for required_section in ("severity_rules", "attempt_cycle_rules", "security_checklist"):
        if required_section not in data:
            msg = f"{required_section} セクションが存在しません"
            print(msg, file=sys.stderr)
            raise ValueError(msg)

    # severity_rules.definitions.CRITICAL の存在確認
    severity_rules = data.get("severity_rules") or {}
    defs = severity_rules.get("definitions") or {}
    if "CRITICAL" not in defs:
        msg = "definitions.CRITICAL が存在しません"
        print(msg, file=sys.stderr)
        raise ValueError(msg)

    # severity_rules.chain_of_thought / evidence_requirements の存在確認（#3832）
    for required_key in ("chain_of_thought", "evidence_requirements"):
        if not severity_rules.get(required_key):
            msg = f"severity_rules.{required_key} セクションが存在しません"
            print(msg, file=sys.stderr)
            raise ValueError(msg)

    # attempt_cycle_rules.downgrade_to_medium の exclusions 存在確認（#4065）
    attempt_cycle_rules = data.get("attempt_cycle_rules") or {}
    downgrade_to_medium = attempt_cycle_rules.get("downgrade_to_medium") or []
    for rule in downgrade_to_medium:
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get("id")
        if rule_id in _CATEGORIES_REQUIRING_EXCLUSIONS and not rule.get("exclusions"):
            msg = f"attempt_cycle_rules.downgrade_to_medium[{rule_id}].exclusions が存在しません"
            print(msg, file=sys.stderr)
            raise ValueError(msg)

    return data


def _render_severity_rules_section(sr: dict[str, Any]) -> list[str]:
    """severity_rules セクションをプロンプト文字列の断片リストとして返す."""
    parts: list[str] = ["## Severity classification (Cloudflare-style, evidence-based)\n\n"]

    defs = sr.get("definitions", {})
    for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        desc = defs.get(level, "")
        if isinstance(desc, str):
            desc = desc.strip()
        parts.append(f"- {level}: {desc}\n")
    parts.append("\n")

    # chain_of_thought
    cot = sr.get("chain_of_thought", {})
    steps = cot.get("steps", [])
    if steps:
        parts.append("### Chain-of-thought (before assigning severity)\n\n")
        for i, step in enumerate(steps, 1):
            step_text = step.strip() if isinstance(step, str) else str(step)
            parts.append(f"{i}. {step_text}\n")
        parts.append("\n")

    # evidence_requirements
    evr = sr.get("evidence_requirements", {})
    hc = evr.get("high_critical", [])
    if hc:
        parts.append("### Evidence requirements (HIGH/CRITICAL)\n\n")
        for item in hc:
            item_text = item.strip() if isinstance(item, str) else str(item)
            parts.append(f"- {item_text}\n")
        parts.append("\n")

    # verdict_rules
    vr = sr.get("verdict_rules", {})
    if vr:
        parts.append("### Verdict rules\n\n")
        for key, val in vr.items():
            val_text = val.strip() if isinstance(val, str) else str(val)
            parts.append(f"- **{key}**: {val_text}\n")
        parts.append("\n")

    return parts


def _render_attempt_cycle_rules_section(acr: dict[str, Any]) -> list[str]:
    """attempt_cycle_rules セクションをプロンプト文字列の断片リストとして返す."""
    parts: list[str] = []
    dtm = acr.get("downgrade_to_medium", [])
    if not dtm:
        return parts

    parts.append("## Issue #1503: attempt-cycle reduction rules\n\n")
    parts.append("The following categories MUST be MEDIUM or LOW, never HIGH or CRITICAL:\n\n")
    for rule in dtm:
        if isinstance(rule, dict):
            rid = rule.get("id", "")
            rdesc = (rule.get("description") or "").strip()
            parts.append(f"- **{rid}**: {rdesc}\n")
            for exclusion in rule.get("exclusions", []):
                exclusion_text = exclusion.strip() if isinstance(exclusion, str) else str(exclusion)
                parts.append(f"  - Exclusion（対象外）: {exclusion_text}\n")
        else:
            parts.append(f"- {rule}\n")
    parts.append("\n")
    return parts


def _render_security_checklist_section(sc: dict[str, Any]) -> list[str]:
    """security_checklist セクションをプロンプト文字列の断片リストとして返す."""
    parts: list[str] = []
    items = sc.get("items", [])
    if not items:
        return parts

    parts.append("## Security checklist\n\n")
    for item in items:
        if isinstance(item, dict):
            sid = item.get("id", "")
            sdesc = (item.get("description") or "").strip()
            shint = item.get("severity_hint", "")
            parts.append(f"- **{sid}** [{shint}]: {sdesc}\n")
        else:
            parts.append(f"- {item}\n")
    parts.append("\n")
    return parts


def _render_bypass_markers_section(bm: dict[str, Any]) -> list[str]:
    """bypass_markers セクションをプロンプト文字列の断片リストとして返す（Issue #4147）.

    ``glossary`` が未定義（旧バージョンの review-prompt.yaml 等）の場合は空リストを返す
    （後方互換・任意セクション）。
    """
    parts: list[str] = []
    glossary = bm.get("glossary", [])
    if not glossary:
        return parts

    parts.append("## PR バイパスマーカー用語集（Issue #4147）\n\n")
    parts.append(
        "以下は本リポジトリの hook が認識する正規バイパスマーカーの一覧です。"
        "各マーカーは PR ボディの HTML コメント（`<!-- allow-xxx: <理由> -->` 等）として"
        "書かれますが、`sanitize_untrusted_text()` により HTML コメントとして本プロンプトの"
        "他セクションからは除去されています。このPRで実際にどのマーカーが存在するかは"
        "別セクション「## PR バイパスマーカーの検出結果」を確認してください"
        "（本セクションはマーカーの用語集であり、このPR個別の判定ではありません）。\n\n"
    )
    for item in glossary:
        if isinstance(item, dict):
            mid = item.get("id", "")
            role = (item.get("role") or "").strip()
            parts.append(f"- **{mid}**: {role}\n")
        else:
            parts.append(f"- {item}\n")
    parts.append("\n")
    return parts


def render_common_header(yaml_path: Path | None = None) -> str:
    """`.claude/rules/review-prompt.yaml` の内容をプロンプト文字列として組み立てて返す.

    Parameters
    ----------
    yaml_path:
        読み込む YAML のパス。`None` の場合は `.claude/rules/review-prompt.yaml` を
        カレントディレクトリ起点で自動解決する。

    Returns
    -------
    str
        severity_rules / attempt_cycle_rules / security_checklist の内容を含む
        プロンプト文字列。
    """
    if yaml_path is None:
        yaml_path = _find_default_yaml_path()

    data = load_review_prompt(yaml_path)

    parts: list[str] = []
    parts.extend(_render_severity_rules_section(data.get("severity_rules", {})))
    parts.extend(_render_attempt_cycle_rules_section(data.get("attempt_cycle_rules", {})))
    parts.extend(_render_security_checklist_section(data.get("security_checklist", {})))
    parts.extend(_render_bypass_markers_section(data.get("bypass_markers", {})))

    return "".join(parts)


def _find_default_yaml_path() -> Path:
    """`.claude/rules/review-prompt.yaml` をカレントディレクトリから親方向に検索する."""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        candidate = parent / ".claude" / "rules" / "review-prompt.yaml"
        if candidate.exists():
            return candidate
    return current / ".claude" / "rules" / "review-prompt.yaml"
