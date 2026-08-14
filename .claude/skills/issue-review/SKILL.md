---
name: issue-review
description: GitHub Issue の品質を意味的に評価する（Pain の深さ・Gherkin の検証可能性）。Anthropic API 直接呼び出しは廃止し、Claude Code の Agent tool で issue-reviewer subagent を起動する（Issue #1301 で全廃）。
---

> **実行環境（ツール名の読み替え）:** 本スキルのツール名参照は Claude Code 前提で記載している。Codex（`.agents/skills` symlink 経由）で実行する場合は、`Agent(subagent_type="x", ...)` → `spawn_agent(agent_type="x", task_name="x", message=...)` に読み替える（`task_name` のみでは default ロールの agent が起動し `.claude/agents/*.md` 相当のツール制約・output_format 契約が適用されない・Issue #3491。対応表・実測記録: `docs/reference/codex-interop.md`「6-4. spawn_agent の `agent_type` 未指定時は default ロールが起動する」）。`Edit` / `Write` → `apply_patch` に読み替える。GitHub 操作は Claude Code・Codex いずれも `gh` CLI を使う（`mcp__github__*` は廃止済み・Issue #3773）。

# /issue-review

引数として Issue 番号（例: `/issue-review 1234`）を受け取り、`.claude/rules/issue-creation.md`
の判定基準に従って意味的品質チェックを実行し、Issue にコメントを投稿する。

**背景:** 従来は `uv run --project projects/py/tidd_tools tidd issue-quality-check` サブコマンドが Anthropic API を直接叩いていた
が、Issue #1301 で全廃した。代わりに Claude Code の Agent tool で `issue-reviewer` subagent
を起動し、Claude Max サブスク枠内で処理する。

## 引数

- `<N>`: 品質チェックする Issue 番号（必須）

## 手順

### STEP 1: Issue 情報を取得

```bash
gh issue view <N> --json title,body,labels
```

取得した title / body / labels を context として保持する。

### STEP 2: type を判定

`labels` から `type: <feat|fix|docs|refactor|build|ci|research>` を抽出する。ない場合は
静的チェック段階で REQUEST_CHANGES する（Agent tool 呼び出し不要）。

### STEP 3: Agent tool で issue-reviewer subagent を起動

Agent tool を以下のように呼ぶ:

```text
Agent(  # Claude Code: Agent tool。Codex: spawn_agent(agent_type="issue_reviewer", task_name="issue_reviewer", message=...) に読み替え
  subagent_type="issue-reviewer",
  description="Issue #<N> の品質チェック",
  prompt=<Issue の title/body/labels/type を含むプロンプト>
)
```

`.claude/agents/issue-reviewer.md` の `output_format` に従い、subagent は次の JSON を返す:

```json
{
  "verdict": "PASS" | "FAIL",
  "pain_score": 1 | 2 | 3,
  "pain_reason": "...",
  "gherkin_issues": ["..."],
  "boundary_missing": false,
  "boundary_reason": "...",
  "prose_only_unjustified": false,
  "prose_only_reason": "...",
  "size_over_1000_possible": false,
  "size_reason": "..."
}
```

- `boundary_missing` (Issue #1288・#1378): critical モジュール（`tidd_tools/ai_review/**`・
  `.claude/hooks/validate-issue.py`・`.claude/hooks/require-issue.py`）を `## 参照` に含む Issue で、
  境界値異常系 Scenario が `## 振る舞い` に含まれていない場合に `true`。`true` の場合 FAIL 扱い。
- **PASS の場合（size_over_1000_possible が false）:**

```bash
gh issue comment <N> --body-file <一時ファイル>  # 本文: "## Issue品質チェック結果\n\n✅ このIssueは実装可能な状態です。\n\n`/issue-next` で着手してください（`🙋 needs-human-input` がなければ自動選定されます）。"
```

- **PASS の場合（size_over_1000_possible が true）:** PASS コメントに加えて、**非ブロッキングの分割提案**を追記する:

```bash
gh issue comment <N> --body-file <一時ファイル>  # 本文: "## Issue品質チェック結果\n\n✅ このIssueは実装可能な状態です。\n\n`/issue-next` で着手してください（`🙋 needs-human-input` がなければ自動選定されます）。\n\n---\n\n### 規模の目安（非ブロッキング）\n\n実装規模が 1000 行を超える可能性があります（<size_reason>）。\n`docs/reference/pr-splitting-guide.md` を参照し、PR を分割できないか検討してください。\n着手はブロックされません。分割するかどうかは実装者の判断に委ねます。"
```

- **FAIL の場合:** subagent が返した `pain_reason` / `gherkin_issues` / `boundary_reason` / `prose_only_reason` を列挙する。`size_over_1000_possible` は FAIL 理由に含めない:

```bash
gh issue comment <N> --body-file <一時ファイル>  # 本文: "## Issue品質チェック結果\n\n❌ このIssueは以下の点を修正してください。\n\n### 修正が必要な項目\n\n- <pain_reason>\n- <gherkin_issues の各項目>\n- <boundary_missing == true の場合は boundary_reason を追記>\n- <prose_only_unjustified == true の場合は prose_only_reason を追記>\n\n修正後、`/issue-review <N>` を再実行してください。"
```

### STEP 5: 終了

- PASS: exit 0
- FAIL: exit 0（コメント投稿は成功しているため）

## Anthropic SDK 直接呼び出し禁止（Issue #1281・#1301）

本 skill / subagent の実装では **`import anthropic` / `from anthropic import` を使わない。**
Claude Code の Agent tool 経由で subagent を起動することで、Claude Max サブスク枠内で
処理する。`.claude/hooks/ban-anthropic-import.py` が違反を機械強制でブロックする。

## 関連

- `.claude/agents/issue-reviewer.md` — subagent 定義
- `.claude/rules/issue-creation.md` — 判定基準
- `.claude/rules/tool-calling.md` — subagent 前提の Tool Calling 設計指針
- docs/reference/issue-review-skill.md — 詳細ドキュメント
- `tidd_tools.issue_quality_check` モジュール — 互換性スタブ（常に PASS）
