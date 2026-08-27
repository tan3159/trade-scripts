"""Issue #2026: PR 作成時に Issue やること内テスト項目を PR Test plan に自動転記する.

**設計:**
- Issue body の ``## やること`` から ``[手動]`` / ``[AI確認]`` prefix 付き項目を抽出
- Issue body に直接記載された ``[AI確認-post-merge]`` 未 tick 項目も対象に含める
  （#2082: 自己修復。本来 Issue には書かない prefix だが、直接記載されても
  PR body 未転記なら救済する）
- PR Test plan の末尾に元の prefix を維持したまま追記する。PR body に同内容が
  既に存在する場合は重複追加しない（#2082）
- Issue body の対象項目を ``- [x]`` に更新し転記済みコメントを Issue に投稿

**Issue #4214: `[手動]` の無条件アップグレードを廃止（設計選択肢 A）:**
    かつては転記時に ``[手動]`` を無条件に ``[AI確認-post-merge]`` へ変換していたが、
    ``docs/reference/post-merge-verify-workflow.md`` の判断ツリー上「人間固有の判断が
    必要」と分類されるべき項目（ブラウザ目視・実機確認等）まで機械的にアップグレード
    され、``## やること`` 全消化 gate をすり抜けて自動マージされる実害が consumer
    実測で複数件発生した（tan3159/mn-scripts#1453）。安全側の対応として、転記時は
    prefix を一切変換せず元のまま維持する。

**フロー（第 1 段: PR 作成時）:**
    1. PR body から ``closes #N`` を辿り Issue body を取得
    2. Issue ``## やること`` の ``[手動]`` / ``[AI確認]`` / ``[AI確認-post-merge]`` 未 tick 項目を抽出
    3. PR Test plan に元の prefix のまま追記（既存内容と重複時はスキップ）
    4. Issue body の対象項目を ``- [x]`` に更新
    5. Issue に転記済みコメントを投稿

**フロー（第 2 段: merge gate）:**
    ``ai_review/core.py`` の ``_rescue_transfer_manual_items`` が転記漏れを検出し、
    同様の転記ロジックを実行する。転記後の PR body で ``ai_review/approve_flow.py`` の
    ``run_approve_flow`` が再分類し、未完了項目（``[手動]`` を含む）が残っていれば
    auto-merge せず exit 4（人間マージ待ち）に倒す（Issue #4214）。

**関連:**
    - Issue #2026 — 本モジュールの設計（2 段構え自動転記）
    - Issue #2082 — Issue に直接記載された ``[AI確認-post-merge]`` の自己修復対応
    - Issue #4214 — ``[手動]`` 無条件アップグレードの廃止（本節）
    - ``ai_review/core.py`` — ``_rescue_transfer_manual_items`` 内で呼び出す（merge gate）
    - ``ai_review/yaru_merge_gate.py`` — Issue やること 全消化 gate
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass

from tidd_tools.shared.issue_body import extract_closes_issues, extract_section, iter_unchecked_items

# ── 定数（Issue #2943: closes/セクション/未チェック項目抽出は shared/issue_body.py へ集約） ──

# [手動] / [AI確認] / [AI確認-post-merge]（Issue 直接記載の自己修復・#2082）prefix。
# 長さ順チェック不要（startswith による完全一致判定のため）。
_TRANSFER_TARGET_PREFIXES = ("[手動]", "[AI確認-post-merge]", "[AI確認]")
# PR body 中の既存転記項目の内容抽出（重複転記防止・#2082・#4098）
_EXISTING_TRANSFER_RE = re.compile(
    r"^\s*-\s*\[[ xX]\]\s+(\[(?:AI確認-post-merge|AI確認|手動)\][^\n]+?)\s*$",
    re.MULTILINE,
)
_CHECKED_ITEM_RE = re.compile(r"^\s*-\s*\[x\]\s+", re.IGNORECASE)
_TEST_PLAN_HEADER_RE = re.compile(r"^##\s+Test plan\b", re.IGNORECASE)
_H2_RE = re.compile(r"^##\s+")

# 転記済みコメントのテンプレート
# Issue #4214: 元の prefix をそのまま維持して転記するため「[AI確認-post-merge] として」
# という固定文言は使わない（[手動] は人間確認、[AI確認-post-merge] のみ cron 検証対象）。
_TRANSFER_COMMENT_TEMPLATE = """\
## テスト項目転記通知

以下の項目を PR のテスト計画に元の prefix を維持したまま転記しました。
`[AI確認-post-merge]` はマージ後に `verify-post-merge` cron が検証します。
`[手動]` は人間による確認が必要です。

{items}

関連 PR: #{pr_num}
"""


# ── データクラス ──────────────────────────────────────────────────────────────


@dataclass
class TransferItem:
    """転記対象の1項目."""

    raw_text: str
    """Issue body の行テキスト（- [ ] [手動] ... の形式）."""

    item_text: str
    """prefix 部分を含む内容テキスト（例: `[手動] ブラウザで確認する`）."""


# ── Issue body パース ─────────────────────────────────────────────────────────


def extract_transfer_targets(issue_body: str) -> list[TransferItem]:
    """Issue body の ``## やること`` から転記対象項目を抽出する.

    転記対象: ``[手動]`` / ``[AI確認]`` / ``[AI確認-post-merge]``（Issue に直接記載された
    場合の自己修復・#2082）prefix 付き未 tick 項目。
    ``## やること`` セクションが存在しない場合は空リストを返す。
    """
    if not issue_body:
        return []
    section = extract_section(issue_body, "やること")
    if section is None:
        return []

    items: list[TransferItem] = []
    for unchecked in iter_unchecked_items(section):
        text = unchecked.text
        for prefix in _TRANSFER_TARGET_PREFIXES:
            if text.startswith(prefix) and len(text) > len(prefix):
                items.append(TransferItem(raw_text=unchecked.raw_line, item_text=text))
                break
    return items


# ── PR body 更新 ──────────────────────────────────────────────────────────────


def append_to_test_plan(pr_body: str, items: list[TransferItem]) -> str:
    """PR body の ``## Test plan`` セクションに転記項目を追記する.

    - ``## Test plan`` セクションが存在する場合: そのセクションの末尾（次の H2 見出し前）に追記
    - ``## Test plan`` セクションが存在しない場合: body 末尾に新規セクションを追加
    - PR body に同内容の既存転記行がある場合は重複追加しない（#2082・#4098）

    転記する形式: Issue #4214 により、``[手動]`` / ``[AI確認]`` /
    ``[AI確認-post-merge]`` いずれの prefix も変換せずそのまま維持する。
    """
    if not items:
        return pr_body

    existing_contents = {_normalize_transfer_content(m.group(1)) for m in _EXISTING_TRANSFER_RE.finditer(pr_body)}
    new_lines: list[str] = []
    for item in items:
        line = _to_post_merge_line(item)
        content = _normalize_transfer_content(item.item_text)
        if content in existing_contents:
            continue
        existing_contents.add(content)
        new_lines.append(line)
    if not new_lines:
        return pr_body

    lines = pr_body.splitlines(keepends=True)
    in_test_plan = False
    insert_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if _TEST_PLAN_HEADER_RE.match(stripped):
            in_test_plan = True
            continue
        if in_test_plan and _H2_RE.match(stripped):
            # 次の H2 見出し前に挿入
            insert_idx = i
            break

    if in_test_plan and insert_idx is None:
        # Test plan が最後のセクションの場合: body 末尾に追記
        insert_idx = len(lines)

    if insert_idx is not None:
        # Test plan セクション内に追記
        suffix = "\n".join(new_lines) + "\n"
        lines.insert(insert_idx, suffix)
        result = "".join(lines)
    else:
        # Test plan セクションが存在しない: 末尾に新規追加
        section_header = "\n## Test plan\n\n"
        section_body = "\n".join(new_lines) + "\n"
        result = pr_body.rstrip("\n") + "\n" + section_header + section_body

    return result


def _to_post_merge_line(item: TransferItem) -> str:
    """TransferItem を Test plan の checkbox 行に変換する.

    Issue #4214（設計選択肢 A）: ``[手動]`` を無条件に ``[AI確認-post-merge]`` へ
    アップグレードすると、``docs/reference/post-merge-verify-workflow.md`` の
    判断ツリー上「人間固有の判断が必要」（構造的に検証不能）と分類されるべき項目
    まで機械変換され、``## やること`` 全消化 gate をすり抜けて自動マージされる
    実害が consumer で複数件実測された。安全側の対応として、``[手動]`` /
    ``[AI確認]`` / ``[AI確認-post-merge]`` いずれの prefix も変換せずそのまま
    維持する（アップグレードは人間が明示的に行う）。
    """
    return f"- [ ] {item.item_text}"


def _normalize_transfer_content(text: str) -> str:
    """転記項目をバッククォート・前後空白の差異を無視して比較する（prefix は保持する）.

    Issue #4214（PR #4215 レビュー指摘）: 以前は prefix を剥がしてから比較していたため、
    PR body に非 blocking な ``[AI確認-post-merge]`` 項目が既存の場合、同内容の
    blocking な ``[手動]``/``[AI確認]`` 項目が「重複」扱いされ追記されず、人間確認前に
    auto-merge され得る不具合があった。prefix を比較対象に残すことで、同一 prefix +
    内容の場合のみ重複とみなす（prefix が異なれば別項目として常に追記する）。
    """
    content = text.strip()
    content = content.replace("`", "")
    return re.sub(r"\s+", " ", content).strip()


# ── Issue body 更新 ───────────────────────────────────────────────────────────


def tick_issue_items(issue_body: str, items: list[TransferItem]) -> str:
    """Issue body の転記済み項目を ``- [x]`` に更新する.

    - ``items`` の ``raw_text`` と完全一致する行を ``- [x]`` に置換する
    - idempotent（既に tick 済みなら変更なし）
    """
    if not items:
        return issue_body

    target_texts = {item.raw_text.strip() for item in items}
    out_lines: list[str] = []
    for line in issue_body.splitlines():
        stripped = line.strip()
        if stripped in target_texts:
            # - [ ] → - [x] に置換
            updated = re.sub(r"^(\s*-\s*)\[\s\]", r"\1[x]", line)
            out_lines.append(updated)
        else:
            out_lines.append(line)
    result = "\n".join(out_lines)
    if issue_body.endswith("\n"):
        result += "\n"
    return result


# ── 転記済みコメント生成 ──────────────────────────────────────────────────────


def build_transfer_comment(items: list[TransferItem], pr_num: int | str) -> str:
    """Issue に投稿する転記済みコメントを生成する."""
    item_lines = "\n".join(f"- {item.item_text}" for item in items)
    return _TRANSFER_COMMENT_TEMPLATE.format(items=item_lines, pr_num=pr_num)


# ── CLI エントリポイント ──────────────────────────────────────────────────────


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """``tidd transfer-issue-items <PR番号>`` サブコマンドを登録する.

    PR 作成時（第 1 段）に Issue `## やること` の `[手動]`/`[AI確認]` 項目を、
    元の prefix を維持したまま PR Test plan に転記する（issue-next skill から呼び出す。
    Issue #4214: `[AI確認-post-merge]` への無条件アップグレードは廃止済み）。
    """
    parser = subparsers.add_parser(
        "transfer-issue-items",
        help="PR 作成時に Issue テスト項目を PR Test plan に自動転記する (#2026)",
        description=(
            "PR body から closes #N を辿り、Issue ## やること の [手動]/[AI確認] 項目を "
            "元の prefix を維持したまま PR Test plan に転記する。"
            "Issue body の対象項目を - [x] に更新し転記済みコメントを Issue に投稿する。"
        ),
    )
    parser.add_argument("pr_num", help="PR 番号")
    parser.add_argument("--dry-run", action="store_true", help="副作用なし（転記内容を表示のみ）")
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    """``tidd transfer-issue-items <PR番号>`` の入り口."""
    import os
    import sys

    from tidd_tools.shared import gh_client as gh

    pr_num = str(args.pr_num)
    repo: str = os.environ.get("REPO") or gh.repo_name_with_owner() or ""
    dry_run: bool = bool(getattr(args, "dry_run", False))

    # PR body 取得
    try:
        pr_body = gh.pr_body(pr_num, repo)
    except Exception as exc:
        print(f"ERROR: PR #{pr_num} の本文取得に失敗しました: {exc}", file=sys.stderr)
        return 1
    if not pr_body:
        print(f"==> PR #{pr_num}: 本文が空のためスキップします", file=sys.stderr)
        return 0

    closes = extract_closes_issues(pr_body)
    if not closes:
        print(f"==> PR #{pr_num}: closes #N が見つかりません。スキップします", file=sys.stderr)
        return 0

    all_items: list[TransferItem] = []
    for issue_num in closes:
        # Issue body 取得（gh issue view）
        try:
            issue_data = gh.issue_view(issue_num, repo=repo, fields=("body",))
        except Exception as exc:
            print(f"WARN: Issue #{issue_num} の本文取得に失敗しました: {exc}", file=sys.stderr)
            continue
        issue_body = issue_data.get("body") or ""
        if not issue_body:
            continue

        items = extract_transfer_targets(issue_body)
        if not items:
            print(f"==> Issue #{issue_num}: 転記対象なし（[手動]/[AI確認] 未 tick 項目がない）", file=sys.stderr)
            continue

        print(
            f"==> Issue #{issue_num}: {len(items)} 件の転記対象を発見しました",
            file=sys.stderr,
        )
        for item in items:
            print(f"  - {item.item_text}", file=sys.stderr)

        all_items.extend(items)

        if not dry_run:
            # Issue body を tick して更新
            updated_issue_body = tick_issue_items(issue_body, items)
            try:
                gh.issue_edit_body(issue_num, repo, updated_issue_body)
                print(f"==> Issue #{issue_num}: 対象項目を - [x] に更新しました", file=sys.stderr)
            except Exception as exc:
                print(f"WARN: Issue #{issue_num} の更新に失敗しました: {exc}", file=sys.stderr)

            # Issue にコメント投稿
            comment = build_transfer_comment(items, pr_num)
            try:
                gh.issue_comment(issue_num, repo, comment)
                print(f"==> Issue #{issue_num}: 転記済みコメントを投稿しました", file=sys.stderr)
            except Exception as exc:
                print(f"WARN: Issue #{issue_num} へのコメント投稿に失敗しました: {exc}", file=sys.stderr)

    if not all_items:
        print(f"==> PR #{pr_num}: 全 Issue に転記対象なし（exit 0）", file=sys.stderr)
        return 0

    # PR Test plan に追記
    updated_pr_body = append_to_test_plan(pr_body, all_items)
    if dry_run:
        print("==> [dry-run] PR Test plan 更新内容:", file=sys.stderr)
        print(updated_pr_body, file=sys.stderr)
    else:
        try:
            gh.update_pr_body(pr_num, repo, updated_pr_body)
            print(f"==> PR #{pr_num}: Test plan に {len(all_items)} 件を転記しました", file=sys.stderr)
        except Exception as exc:
            print(f"WARN: PR #{pr_num} の Test plan 更新に失敗しました: {exc}", file=sys.stderr)
            return 1

    return 0
