# issue-fixer リトライ時の Opus 動的エスカレーション（Issue #2803）

> **実行環境（ツール名の読み替え）:** 本スキルのツール名参照は Claude Code 前提で記載している。Codex（`.agents/skills` symlink 経由）で実行する場合は、`Agent(subagent_type="x", ...)` → `spawn_agent(agent_type="x", task_name="x", message=...)` に読み替える（`task_name` のみでは default ロールの agent が起動し `.claude/agents/*.md` 相当のツール制約・output_format 契約が適用されない・Issue #3491。対応表・実測記録: `docs/reference/codex-interop.md`「6-4. spawn_agent の `agent_type` 未指定時は default ロールが起動する」）。`Edit` / `Write` → `apply_patch` に読み替え、`mcp__github__*` は Codex 側の GitHub MCP 設定が済んでいれば同ツール名のまま、未設定なら `gh` CLI で実行する。

`/issue-next` STEP 5「リトライループ（issue-fixer 委譲）」で issue-fixer subagent を呼び出す際、
retry attempt（直前に実行した `tidd ai-review <PR番号> <試行回数>` の `<試行回数>` の値）に応じて
`model` パラメータの要否を判定する。

## 判定分岐

- retry attempt が **1**（初回の issue-fixer 呼び出し）→ `model` パラメータを渡さない
  （`issue-fixer.md` の frontmatter 通り sonnet で実行）
- retry attempt が **2 以上**（2 回目以降の issue-fixer 呼び出し）→ `model="opus"` を明示的に渡す
- 何らかの理由で retry attempt の値が取得できない場合 → `model` パラメータを渡さない
  （sonnet にフォールバック。誤って Opus に昇格させない）

## 呼び出し例

```
# retry attempt = 1（初回・sonnet 固定・model 省略）
Agent(  # Claude Code: Agent tool。Codex: spawn_agent(agent_type="issue_fixer", task_name="issue_fixer", message=...) に読み替え
  subagent_type="issue-fixer",
  description="PR #<PR番号> の指摘修正",
  prompt="PR番号: <PR番号>"
)

# retry attempt >= 2（2 回目以降・Opus へエスカレーション）
Agent(  # Claude Code: Agent tool。Codex: spawn_agent(agent_type="issue_fixer", task_name="issue_fixer", message=...) に読み替え
  subagent_type="issue-fixer",
  description="PR #<PR番号> の指摘修正（Opus エスカレーション）",
  prompt="PR番号: <PR番号>",
  model="opus"
)
```

subagent は `pushed: <SHA>`（正常終了）または `park: <理由>` / `issue: #<N>（needs-human-input 付与済み）`
（park）のいずれかの形式で最終応答する。

## スコープ外

STEP 2 の issue-implementer 初回呼び出しは本エスカレーション対象外。`model` パラメータを渡さず
`issue-implementer.md` の frontmatter 通り sonnet 固定のまま変更しない。

## 発動条件・コスト影響

| retry attempt | `model` パラメータ | 実行 model |
|----------------|--------------------|-----------|
| 1（初回の issue-fixer 呼び出し） | 渡さない | `issue-fixer.md` frontmatter 通り sonnet |
| 2 以上（2 回目以降） | `model="opus"` を明示 | opus |
| 取得不能（何らかの理由で attempt 値が分からない） | 渡さない | sonnet にフォールバック（誤って Opus に昇格させない） |

- **手戻りが発生しない PR（大多数）:** コスト影響なし。issue-fixer 自体が呼ばれないか、呼ばれても
  retry attempt = 1 の 1 回のみで sonnet のまま完結する
- **手戻りが発生する PR（少数）:** 2 回目以降の issue-fixer 呼び出しのみ Opus 単価が乗る。
  Opus は Sonnet より単価が高いため、手戻り回数が多い PR ほどコスト増分が大きくなるが、
  対象は「実際にレビュー指摘が繰り返された PR」に限定されるため、#2451 が懸念した
  「Opus 継承によるコスト予測不能な全面膨張」は再燃しない
- **判定コスト:** retry attempt の値は既存の `tidd ai-review <PR番号> <試行回数>` 呼び出し引数を
  そのまま参照するため、追加の状態管理・分類 subagent 起動などのオーバーヘッドは発生しない

## 関連

- `docs/decisions/2026-07-23-issue-implementer-fixer-model-sonnet.md`「追記（Issue #2803）」節
- `docs/reference/issue-next-loop-operations.md`「(h.2)」
- `docs/reference/subagent-design-guide.md`「model は明示指定必須」
