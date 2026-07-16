---
name: ai-review
description: PR のコードレビューを agy/codex subprocess で実行し、verdict-extractor subagent で verdict を構造化抽出する（Issue #1303）。Anthropic SDK を直接使わない。tidd ai-review の --continue-with-verdict で後続処理（マージ・タスクチェック等）を委譲する。
---

# /ai-review

引数として PR 番号（例: `/ai-review 1234`）を受け取り、agy/codex でレビューを実行し、
`verdict-extractor` subagent で verdict を構造化抽出して GitHub に投稿する。

**背景:** `tidd ai-review` の Python 実装では `verdict.py` が Anthropic SDK（`claude-haiku-4-5`）
を直接呼び出していた（Issue #1243）。Issue #1303 でこれを廃止し、Claude Code の Agent tool
経由で `verdict-extractor` subagent による構造化抽出に置換した。

## 引数

- `<N>`: レビューする PR 番号（必須）
- `<ATTEMPT>`: 試行回数（省略時 1）

## 手順

### STEP 1: PR 情報を取得

```bash
gh pr view <N> --json number,title,body,headRefName,baseRefName
```

### STEP 2: PR diff を取得

```bash
gh pr diff <N>
```

package-lock.json / yarn.lock / uv.lock のブロックを除外する（500KB 超は切り詰める）。

### STEP 3: レビュープロンプトを組み立てて agy で実行

プロンプトは以下の要素で構成する:

```
Activate the code-review-commons skill. Skip security vulnerability findings — those are
handled separately by gemini-cli-security.

## Severity classification override (Cloudflare-style, evidence-based)

Before assigning any severity, follow this chain of thought:
1. Identify the exact behavior the code produces under real runtime conditions.
2. Ask: does this CURRENTLY cause an outage, data loss, or wrong observable outcome?
3. If yes, quote the diff line (file:line). Then pick severity.
4. If no (theoretical concern), pick MEDIUM or LOW.

Severity definitions:
- CRITICAL: Will cause an outage, data loss, or is exploitable in production.
- HIGH: Causes a wrong observable outcome under normal production usage.
- MEDIUM: Maintainability / readability / non-blocking improvement.
- LOW: Nit / style / suggestion.

Evidence requirement: Never emit HIGH or CRITICAL without citing a specific diff line.

---

以下の PR のコードをレビューし、最後に VERDICT: APPROVE または VERDICT: REQUEST_CHANGES
（および [CRITICAL] / [HIGH] 等の指摘事項）を出力してください。

PR #<N>: <title>
---
<PR diff>
```

実行:
```bash
agy --prompt "<プロンプト + diff>" 2>&1
```

**CRITICAL（Issue #2029）: `tee` 禁止。レビュー本文のみを `/tmp/agent-review-<N>.md` に保存する。**
`agy ... | tee /tmp/agent-review-<N>.md` や `codex exec ... | tee ...` のように backend の
生出力をそのままファイルに保存してはならない。codex の生セッションログ（161KB 実例: PR #2027）が
丸ごと保存され、`--continue-with-verdict` がそれを PR コメントとして投稿して破損する。
backend 出力からレビュー本文（サマリー・指摘事項・VERDICT 行）だけを抽出し、STEP 4 で
Write ツールにより保存すること。

agy が利用不可（QUOTA_EXCEEDED・未インストール）の場合は codex にフォールバック:
```bash
codex exec --quiet "<プロンプト + diff>"
```

どちらも利用不可の場合は exit 3 を返す。

### STEP 3.5: parser critical PR 判定（§1-3-3）

STEP 2 で取得した diff に以下のパスが含まれるか確認する:

- `projects/py/tidd_tools/src/tidd_tools/ai_review/**`
- `.claude/hooks/validate-issue.py`
- `.claude/hooks/require-issue.py`

**いずれかが含まれる場合、この PR は parser critical PR**（以下 `_is_parser_critical=true` と呼ぶ）。
verdict 確定後の STEP 5.5 で secondary consensus チェックを実行する。

`_is_parser_critical` の値にかかわらず STEP 4 に進む。

### STEP 4: レビュー本文と backend 名を保存

Write ツールでレビュー本文を保存する（`--continue-with-verdict` が読む）:

```
保存先: /tmp/agent-review-<N>.md
```

**注記（Issue #2029）: `tee` を使わないこと。** STEP 3 の実行結果をパイプで直接保存すると
生セッションログが混入する。必ず Write ツールでレビュー本文のみ（サマリー・指摘事項・
VERDICT 行）を保存する。なお `--continue-with-verdict` 側にも GitHub コメント上限
（65536 文字）を超える body を PR コメントに投稿しないガードがある（二重ガード）。

backend 名を記録:
```bash
mkdir -p "${HOME}/.cache/ai-dev-handbook/ai-reviewer/pr-<N>"
printf 'agy\n' > "${HOME}/.cache/ai-dev-handbook/ai-reviewer/pr-<N>/backend-name"
```

### STEP 5: verdict-extractor subagent で verdict を構造化抽出

Agent tool を以下のように呼ぶ:

```
Agent(
  subagent_type="verdict-extractor",
  description="PR #<N> の verdict 抽出",
  prompt="以下のレビュー出力から VERDICT を構造化して返してください。\n\n<STEP 3 のレビュー本文>"
)
```

subagent は次の JSON を返す:

```json
{
  "verdict": "APPROVE" | "REQUEST_CHANGES" | "ESCALATE",
  "issues": ["[CRITICAL] ...", "[HIGH] ..."],
  "confidence": "high" | "low"
}
```

`confidence: "low"` の場合は VERDICT ログに注記する。

### STEP 5.5: parser critical PR の secondary consensus チェック

`_is_parser_critical=true` かつ STEP 5 の verdict が `APPROVE` の場合のみ実行する。
`_is_parser_critical=false` または verdict が `REQUEST_CHANGES` / `ESCALATE` の場合はスキップして STEP 6 へ進む。

**まず primary backend を確認する:**

```bash
cat "${HOME}/.cache/ai-dev-handbook/ai-reviewer/pr-<N>/backend-name"
# → "agy" / "codex" なら非 Claude backend（multi-backend 可）
# → "claude-code" なら Claude fallback（同一 backend → multi-backend 不可）
```

**primary が `claude-code`（Claude fallback）の場合:**
同一 backend 系統のため multi-backend consensus を満たせない。人間にエスカレーションする:

```bash
gh pr comment <N> --body "parser critical PR のため multi-backend consensus が必要ですが、primary backend（agy/codex）が利用不可でした。人間レビューが必要です。"
gh pr edit <N> --add-label "needs-human-merge"
exit 2
```

**primary が `agy` または `codex`（非 Claude backend）の場合:**
Agent tool で secondary レビューを実行する:

```
Agent(
  subagent_type="ai-reviewer",
  description="PR #<N> secondary consensus レビュー",
  prompt="parser critical PR #<N> を独立にレビューしてください。\n\n変更ファイル:\n<gh pr diff <N> --name-only の出力>\n\n差分:\n<gh pr diff <N> の出力（uv.lock 等 lockfile 除外）>"
)
```

secondary subagent（`.claude/agents/ai-reviewer.md`）は primary review 本文を参照せず、
コードを独立に読んで判定する（Read / Grep / Glob ツールで変更ファイルを直接参照）。

**⚠️ CRITICAL（Issue #1410）: Claude inline fallback 禁止**

Agent tool が使えない環境（deferred toolset・auto-compact 後・claude-code #14018 バグ等）で
`ai-reviewer` subagent の起動に失敗した場合、**Claude が自身で inline に ai-reviewer 役を演じてはならない**。

Claude が同一プロセス内で primary/secondary の両方を担当すると独立判定にならず、
multi-backend consensus の設計（`#1290`）が実質的に崩れる（同一モデルによる自作自演）。

**subagent 起動不能を検知したら以下を実行:**

```bash
gh pr comment <N> --body "secondary consensus 不能（ai-reviewer subagent 起動不可）。手動確認が必要です。"
gh pr edit <N> --add-label "needs-human-merge"
exit 2
```

返却 JSON:

```json
{
  "verdict": "APPROVE" | "REQUEST_CHANGES",
  "issues": ["[HIGH] src/tidd_tools/ai_review/verdict.py:42 ..."],
  "rationale": "判定根拠（1-3 文）"
}
```

**secondary レビュー本文を PR に投稿する（監査証跡・Issue #2079）:**

集約タリー（`consensus: 2/2 APPROVE` 等）のみでは secondary が実際に何を検証してどう判断したかが
事後に確認できない（consensus.json にもメタデータしか残らないため）。consensus 判定コメントを
投稿する前に、secondary の判定内容（verdict・issues・rationale）を PR コメントとして必ず投稿する:

```bash
gh pr comment <N> --body "$(cat <<'EOF'
## secondary レビュー（ai-reviewer subagent・consensus 用）

**verdict:** <subagent が返した verdict>

**issues:**
<subagent が返した issues を箇条書きで列挙。空配列の場合は「指摘なし」>

**rationale:** <subagent が返した rationale>
EOF
)"
```

**consensus 実行の観測可能な記録（監査可能性の担保）:**

`ai-reviewer` subagent 起動成功時のみ以下を書き込む（inline Claude 判定は「未実施」として扱い書き込まない）。
`verdict`・`issues`・`rationale` は上記「返却 JSON」で subagent が返した値をそのまま書き込む
（メタデータのみだった従来スキーマから監査可能な内容へ拡張・Issue #2079）:

```bash
mkdir -p "${HOME}/.cache/ai-dev-handbook/ai-reviewer/pr-<N>"
cat > "${HOME}/.cache/ai-dev-handbook/ai-reviewer/pr-<N>/consensus.json" <<EOF
{
  "secondary_backend": "ai-reviewer-subagent",
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "primary_backend": "$(cat ${HOME}/.cache/ai-dev-handbook/ai-reviewer/pr-<N>/backend-name)",
  "pr_num": "<N>",
  "verdict": "<subagent が返した verdict>",
  "issues": <subagent が返した issues 配列>,
  "rationale": "<subagent が返した rationale>"
}
EOF
```

後日 `~/.cache/ai-dev-handbook/ai-reviewer/pr-*/consensus.json` を集計すれば、どの parser critical PR で
subagent 経由の consensus が実施されたか、および secondary が何を判定したかを監査できる。
ファイルが無い parser critical PR は「consensus 未実施」として扱う。

**consensus 判定:**

| primary backend | primary verdict | secondary | 最終判定 |
|---|---|---|---|
| agy / codex | APPROVE | APPROVE | STEP 6 へ（コメント: `consensus: 2/2 APPROVE`） |
| agy / codex | APPROVE | REQUEST_CHANGES | exit 2（コメント: `consensus: 1/2 APPROVE, needs human review` + ラベル） |
| claude-code | APPROVE | — | exit 2（同一 backend → 人間エスカレーション） |

```bash
# consensus APPROVE の場合（agy/codex primary + ai-reviewer APPROVE）
gh pr comment <N> --body "consensus: 2/2 APPROVE"
# → STEP 6 へ進む

# consensus 不一致の場合
gh pr comment <N> --body "consensus: 1/2 APPROVE, needs human review"
gh pr edit <N> --add-label "needs-human-merge"
exit 2
```

### STEP 6: --continue-with-verdict で後続処理を実行

verdict に応じて:

```bash
uv run --project projects/py/tidd_tools python -m tidd_tools ai-review \
  --continue-with-verdict <APPROVE|REQUEST_CHANGES> <N>
```

**ESCALATE の場合:** `--continue-with-verdict` は呼ばず、人間にエスカレーションを報告する。

終了コードの解釈:
- 0 → APPROVE 自動マージ完了
- 1 → REQUEST_CHANGES（指摘あり。修正後に再実行）
- 2 → エスカレーション（PR コメント・通知済み）
- 4 → 停止条件あり / `[AI確認]` / `[手動]` タスク残り

## Anthropic SDK 直接呼び出し禁止（Issue #1281・#1303）

本 skill / subagent の実装では **`import anthropic` を使わない。**
verdict 抽出は `verdict-extractor` subagent（Agent tool）経由で行う。
`.claude/hooks/ban-anthropic-import.py` が違反を機械強制でブロックする。

## §1-3-3 多 backend 一致（parser critical PR）

parser critical PR（`ai_review/**`・`validate-issue.py`・`require-issue.py` を変更する PR）は
STEP 3.5 で検出し、primary（agy/codex via STEP 3）と secondary（`.claude/agents/ai-reviewer.md`）
の両方が APPROVE を返したときにのみ最終 APPROVE とする。

一方でも REQUEST_CHANGES を返した場合は `consensus: N/2 APPROVE, needs human review` を
PR コメントに投稿し、`needs-human-merge` ラベルを付与して exit 2 で終了する。

詳細は `docs/reference/multi-backend-consensus.md` を参照。

## 関連

- `.claude/agents/verdict-extractor.md` — verdict 抽出 subagent
- `projects/py/tidd_tools/src/tidd_tools/ai_review/core.py` — Python の tidd ai-review
- `docs/reference/ai-review-skill.md` — 詳細ドキュメント
- `.claude/rules/tool-calling.md` — subagent 前提の Tool Calling 設計指針
