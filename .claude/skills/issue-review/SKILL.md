---
name: issue-review
description: GitHub Issue の品質を意味的に評価する（Pain の深さ・Gherkin の検証可能性）。Anthropic API 直接呼び出しは廃止し、Claude Code の Agent tool で issue-reviewer subagent を起動する（Issue #1301 で全廃）。
---

# /issue-review

引数として Issue 番号（例: `/issue-review 1234`）を受け取り、`.claude/rules/issue-creation.md`
の判定基準に従って意味的品質チェックを実行し、Issue にコメントを投稿する。

**背景:** 従来は `tidd issue-quality-check` サブコマンドが Anthropic API を直接叩いていた
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
Agent(
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
  "boundary_reason": "..."
}
```

- `boundary_missing` (Issue #1288・#1378): critical モジュール（`tidd_tools/ai_review/**`・
  `.claude/hooks/validate-issue.py`・`.claude/hooks/require-issue.py`）を `## 参照` に含む Issue で、
  境界値異常系 Scenario が `## 振る舞い` に含まれていない場合に `true`。`true` の場合 FAIL 扱い。

### STEP 4: Issue にコメントを投稿

判定結果に応じて以下のコメントを投稿する:

- **PASS の場合:**

```bash
gh issue comment <N> --body "## Issue品質チェック結果

✅ このIssueは実装可能な状態です。

\`/issue-next\` で着手してください（\`🙋 needs-human-input\` がなければ自動選定されます）。"
```

- **FAIL の場合:** subagent が返した `pain_reason` / `gherkin_issues` / `boundary_reason` を列挙する:

```bash
gh issue comment <N> --body "## Issue品質チェック結果

❌ このIssueは以下の点を修正してください。

### 修正が必要な項目

- <pain_reason>
- <gherkin_issues の各項目>
- <boundary_missing == true の場合は boundary_reason を追記>

修正後、\`/issue-review <N>\` を再実行してください。"
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
- [docs/reference/issue-review-skill.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/issue-review-skill.md) — 詳細ドキュメント
- `tidd_tools.issue_quality_check` モジュール — 互換性スタブ（常に PASS）
