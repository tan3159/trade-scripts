---
name: issue-review
description: GitHub Issue の品質を意味的に評価する（Pain の深さ・Gherkin の検証可能性）。Anthropic API 直接呼び出しは廃止し、Claude Code の Agent tool で issue-reviewer subagent を起動する（Issue #1301 で全廃）。
---

```
mcp__github__get_issue({owner, repo, issue_number: <N>})
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

```
mcp__github__add_issue_comment({owner, repo, issue_number: <N>, body: "## Issue品質チェック結果\n\n✅ このIssueは実装可能な状態です。\n\n`/issue-next` で着手してください（`🙋 needs-human-input` がなければ自動選定されます）。"})
```

- **PASS の場合（size_over_1000_possible が true）:** PASS コメントに加えて、**非ブロッキングの分割提案**を追記する:

```
mcp__github__add_issue_comment({owner, repo, issue_number: <N>, body: "## Issue品質チェック結果\n\n✅ このIssueは実装可能な状態です。\n\n`/issue-next` で着手してください（`🙋 needs-human-input` がなければ自動選定されます）。\n\n---\n\n### 規模の目安（非ブロッキング）\n\n実装規模が 1000 行を超える可能性があります（<size_reason>）。\n`docs/reference/pr-splitting-guide.md` を参照し、PR を分割できないか検討してください。\n着手はブロックされません。分割するかどうかは実装者の判断に委ねます。"})
```

- **FAIL の場合:** subagent が返した `pain_reason` / `gherkin_issues` / `boundary_reason` / `prose_only_reason` を列挙する。`size_over_1000_possible` は FAIL 理由に含めない:

```
mcp__github__add_issue_comment({owner, repo, issue_number: <N>, body: "## Issue品質チェック結果\n\n❌ このIssueは以下の点を修正してください。\n\n### 修正が必要な項目\n\n- <pain_reason>\n- <gherkin_issues の各項目>\n- <boundary_missing == true の場合は boundary_reason を追記>\n- <prose_only_unjustified == true の場合は prose_only_reason を追記>\n\n修正後、`/issue-review <N>` を再実行してください。"})
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
