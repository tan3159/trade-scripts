---
name: ai-review
description: PR のコードレビューを tidd ai-review --stop-before-merge で実行し、verdict-extractor subagent で verdict を構造化抽出する（Issue #1303）。Anthropic SDK を直接使わない。tidd ai-review の --continue-with-verdict で後続処理（マージ・タスクチェック等）を委譲する。
---

> **実行環境（ツール名の読み替え）:** 本スキルのツール名参照は Claude Code 前提で記載している。Codex（`.agents/skills` symlink 経由）で実行する場合は、`Agent(subagent_type="x", ...)` → `spawn_agent(agent_type="x", task_name="x", message=...)` に読み替える（`task_name` のみでは default ロールの agent が起動し `.claude/agents/*.md` 相当のツール制約・output_format 契約が適用されない・Issue #3491。対応表・実測記録: `docs/reference/codex-interop.md`「6-4. spawn_agent の `agent_type` 未指定時は default ロールが起動する」）。`Edit` / `Write` → `apply_patch` に読み替え、`mcp__github__*` は Codex 側の GitHub MCP 設定が済んでいれば同ツール名のまま、未設定なら `gh` CLI で実行する。

# /ai-review

引数として PR 番号（例: `/ai-review 1234`）を受け取り、agy/codex でレビューを実行し、
`verdict-extractor` subagent で verdict を構造化抽出して GitHub に投稿する。

**背景:** `tidd ai-review` の Python 実装では `verdict.py` が Anthropic SDK（`claude-haiku-4-5`）
を直接呼び出していた（Issue #1243）。Issue #1303 でこれを廃止し、Claude Code の Agent tool
経由で `verdict-extractor` subagent による構造化抽出に置換した。

### STEP 1: PR 情報と parser critical 判定を実施

```
mcp__github__get_pull_request({owner, repo, pull_number: <N>})
```

続いて変更ファイル一覧を取得し、parser critical PR かどうかを判定する:

```
mcp__github__list_pull_request_files({owner, repo, pull_number: <N>})
```

以下のパスが含まれるか確認する:
- `tidd_tools/ai_review/`（サブディレクトリ含む）
- `.claude/hooks/validate-issue.py`
- `.claude/hooks/require-issue.py`

**いずれかが含まれる場合、この PR は parser critical PR**（以下 `_is_parser_critical=true` と呼ぶ）。
verdict 確定後の STEP 3 で secondary consensus チェックを実行する。

### STEP 2: tidd ai-review --stop-before-merge でレビューを実行

**旧 STEP 2-4（手書き diff 取得・プロンプト組み立て・agy/codex 実行・本文保存）を廃止。**
代わりに `tidd ai-review --stop-before-merge` の単一コマンドを実行する（Issue #2645）。

```bash
tidd ai-review --stop-before-merge <N>
```

**終了コードの解釈:**
- 10 → APPROVE（verdict 確定。レビュー本文が `$AGENT_REVIEW_DIR/agent-review-<N>.md` に保存済み）
- 11 → REQUEST_CHANGES（verdict 確定。レビュー本文が保存済み）
- 3 → 全バックエンド利用不可（exit 3 のまま終了する）
- その他 → エラー（人間にエスカレーション）

**exit 3 の場合:** `issue-next` SKILL.md 「exit 3 フォールバック」節に従って fallback-review subagent を起動する。

**注記（Issue #2029）:** `tee` 禁止・生セッションログ混入は `tidd ai-review` 内部で防護済み。
`backend 名の記録` / `timing.json 書き込み` / `プロンプトの出力フォーマット` も Python 経路で担保される。

レビュー本文は `$AGENT_REVIEW_DIR/agent-review-<N>.md`（環境変数未設定時は `/tmp/agent-review-<N>.md`）に保存される。

### STEP 3: verdict-extractor subagent で verdict を構造化抽出

```bash
cat "$AGENT_REVIEW_DIR/agent-review-<N>.md"
# または
cat /tmp/agent-review-<N>.md
```

で保存済みレビュー本文を読み込み、Agent tool を以下のように呼ぶ:

```
Agent(  # Claude Code: Agent tool。Codex: spawn_agent(agent_type="verdict_extractor", task_name="verdict_extractor", message=...) に読み替え
  subagent_type="verdict-extractor",
  description="PR #<N> の verdict 抽出",
  prompt="以下のレビュー出力から VERDICT を構造化して返してください。\n\n<レビュー本文>"
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

### STEP 3.5: parser critical PR の secondary consensus チェック

`_is_parser_critical=true` かつ STEP 3 の verdict が `APPROVE` の場合のみ実行する。
`_is_parser_critical=false` または verdict が `REQUEST_CHANGES` / `ESCALATE` の場合はスキップして STEP 4 へ進む。

**secondary レビューより先に primary review コメントを投稿する（Issue #2314・#2523）:**

STEP 4 の `--continue-with-verdict` は primary review コメントを内部で投稿するが、
STEP 3.5 は STEP 4 より前に実行されるため、そのまま進むと secondary review コメント・
consensus コメントが primary review コメントより先に GitHub 上へ投稿されてしまう
（投稿順序が primary → secondary → consensus からずれる）。secondary レビューを実行する前に
以下を実行し、primary review コメントを先に投稿しておく:

```bash
tidd ai-review --post-primary-review <N>
```

**重要（Issue #2523）:** この `--post-primary-review` は PR コメントのみで投稿し、
正式レビューは残さない（`as_formal_review=False`）。
consensus 確定前の中間コメントに正式レビューを残すと、secondary が後で REQUEST_CHANGES を
返してエスカレーションになった際も bot の「Approved」正式レビューが GitHub Reviewers 欄に
残ったままになる矛盾を防ぐため。正式な approve は STEP 4 の
`--continue-with-verdict APPROVE` 実行時（consensus 確定後）にのみ発行される。

投稿済みフラグ（`cwv-approved`）が立つため、STEP 4 の `--continue-with-verdict APPROVE <N>` は
コメントを再投稿せずにマージ判定へ直接進み、その時点で正式 `gh pr review --approve` を発行する。

**まず primary backend を確認する:**

```bash
cat "${HOME}/.cache/tidd/ai-reviewer/pr-<N>/backend-name"
# → "agy" / "codex" なら非 Claude backend（multi-backend 可）
# → "claude-code" なら Claude fallback（同一 backend → multi-backend 不可）
```

**primary が `claude-code`（Claude fallback）の場合:**
同一 backend 系統のため multi-backend consensus を満たせない。人間にエスカレーションする:

```bash
# bot アカウントで投稿する（Issue #2656）
# --post-comment が exit 1 を返した場合も再投稿を行わない（exit 2 でエスカレーション）
tidd ai-review --post-comment <N> "parser critical PR のため multi-backend consensus が必要ですが、primary backend（agy/codex）が利用不可でした。人間レビューが必要です。" --no-reviewer-footer
# update_pull_request は labels を受け付けないため gh api でラベル追加する
gh api -X POST /repos/{owner}/{repo}/issues/<N>/labels --input - <<< '{"labels":["needs-human-merge"]}'
exit 2
```

**primary が `agy` または `codex`（非 Claude backend）の場合:**
Agent tool で secondary レビューを実行する:

```
Agent(  # Claude Code: Agent tool。Codex: spawn_agent(agent_type="ai_reviewer", task_name="ai_reviewer", message=...) に読み替え
  subagent_type="ai-reviewer",
  description="PR #<N> secondary consensus レビュー",
  prompt="parser critical PR #<N> を独立にレビューしてください。\n\n変更ファイル:\n<mcp__github__list_pull_request_files の filename 一覧>\n\n差分は提供しません。変更ファイルは Read / Grep / Glob で直接読んでください。現在の作業ディレクトリは PR ブランチの worktree であり、Read したファイルは PR 適用後の内容です。"
)
```

secondary subagent（`.claude/agents/ai-reviewer.md`）は primary review 本文を参照せず、
コードを独立に読んで判定する（Read / Grep / Glob ツールで変更ファイルを直接参照。
実行 CWD は PR ブランチの worktree のため、Read したファイルは PR 適用後の内容である）。

**⚠️ CRITICAL（Issue #1410）: Claude inline fallback 禁止**

Agent tool が使えない環境（deferred toolset・auto-compact 後・claude-code #14018 バグ等）で
`ai-reviewer` subagent の起動に失敗した場合、**Claude が自身で inline に ai-reviewer 役を演じてはならない**。

Claude が同一プロセス内で primary/secondary の両方を担当すると独立判定にならず、
multi-backend consensus の設計（`#1290`）が実質的に崩れる（同一モデルによる自作自演）。

**subagent 起動不能を検知したら以下を実行:**

```bash
# bot アカウントで投稿する（Issue #2656）
# --post-comment が exit 1 を返した場合も再投稿を行わない（exit 2 でエスカレーション）
tidd ai-review --post-comment <N> "secondary consensus 不能（ai-reviewer subagent 起動不可）。手動確認が必要です。" --no-reviewer-footer
# update_pull_request は labels を受け付けないため gh api でラベル追加する
gh api -X POST /repos/{owner}/{repo}/issues/<N>/labels --input - <<< '{"labels":["needs-human-merge"]}'
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
投稿する前に、secondary の判定内容（verdict・issues・rationale）を PR コメントとして必ず投稿する。

**フッターには secondary backend 名（`ai-reviewer subagent`）を使うこと（Issue #2660）。**
`tidd ai-review --post-comment` に `--reviewer "ai-reviewer subagent"` を渡すことで
primary backend 名（`STATE_DIR/backend-name`）を誤ってフッターに使う問題を防ぐ:

```bash
tidd ai-review --post-comment <N> "$(cat <<'EOF'
## secondary レビュー（ai-reviewer subagent・consensus 用）

**verdict:** <subagent が返した verdict>

**issues:**
<subagent が返した issues を箇条書きで列挙。空配列の場合は「指摘なし」>

**rationale:** <subagent が返した rationale>
EOF
)" --reviewer "ai-reviewer subagent"
```

**consensus 実行の観測可能な記録（監査可能性の担保）:**

`ai-reviewer` subagent 起動成功時のみ以下を書き込む（inline Claude 判定は「未実施」として扱い書き込まない）。
`verdict`・`issues`・`rationale` は上記「返却 JSON」で subagent が返した値をそのまま書き込む
（メタデータのみだった従来スキーマから監査可能な内容へ拡張・Issue #2079）:

```bash
mkdir -p "${HOME}/.cache/tidd/ai-reviewer/pr-<N>"
cat > "${HOME}/.cache/tidd/ai-reviewer/pr-<N>/consensus.json" <<EOF
{
  "secondary_backend": "ai-reviewer-subagent",
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "primary_backend": "$(cat ${HOME}/.cache/tidd/ai-reviewer/pr-<N>/backend-name)",
  "pr_num": "<N>",
  "verdict": "<subagent が返した verdict>",
  "issues": <subagent が返した issues 配列>,
  "rationale": "<subagent が返した rationale>"
}
EOF
```

後日 `~/.cache/tidd/ai-reviewer/pr-*/consensus.json` を集計すれば、どの parser critical PR で
subagent 経由の consensus が実施されたか、および secondary が何を判定したかを監査できる。
ファイルが無い parser critical PR は「consensus 未実施」として扱う。

**consensus 判定（Issue #2657 でリトライ方式に更新）:**

| primary backend | primary verdict | secondary | 最終判定 |
|---|---|---|---|
| agy / codex | APPROVE | APPROVE | STEP 4 へ（コメント: `consensus: 2/2 APPROVE`） |
| agy / codex | APPROVE | REQUEST_CHANGES（1 回目） | リトライ継続（exit 1・`needs-human-merge` ラベル付与なし）|
| agy / codex | APPROVE | REQUEST_CHANGES（2 回目・同一指摘） | exit 2（コメント + `needs-human-merge` ラベル付与）|
| agy / codex | APPROVE | REQUEST_CHANGES（2 回目・異なる指摘） | リトライ継続（exit 1）|
| claude-code | APPROVE | — | exit 2（同一 backend → 人間エスカレーション） |

secondary consensus の判定は `tidd ai-review --consensus-verdict` サブコマンドで実行する（Issue #2657）:

```bash
# consensus 判定の実行（secondary issues は JSON 配列文字列で渡す）
SECONDARY_ISSUES='["[CRITICAL] core.py::handle_sha_cache_hit() が stop_before_merge を受け取らない"]'
tidd ai-review --consensus-verdict <N> REQUEST_CHANGES "$SECONDARY_ISSUES" <ATTEMPT>
# exit 0: secondary APPROVE（consensus 通過）→ STEP 4 へ
# exit 1: リトライ継続（needs-human-merge ラベル付与なし）
#   1 回目不一致の出力例: "consensus 不一致（1 回目）: リトライします"
# exit 2: エスカレーション（needs-human-merge ラベル付与）
#   2 回連続同一指摘の例: "consensus: 1/2 APPROVE, needs human review（同じ secondary 指摘が 2 回連続）"

# consensus APPROVE の場合（agy/codex primary + ai-reviewer APPROVE）
# consensus 判定コメントは特定 backend に紐づかないため --no-reviewer-footer を使う（Issue #2660）
tidd ai-review --post-comment <N> "consensus: 2/2 APPROVE" --no-reviewer-footer
# → STEP 4 へ進む
```

### STEP 4: --continue-with-verdict で後続処理を実行

verdict に応じて:

```bash
tidd ai-review \
  --continue-with-verdict <APPROVE|REQUEST_CHANGES> <N>
```

**ESCALATE の場合:** `--continue-with-verdict` は呼ばず、人間にエスカレーションを報告する。

STEP 1 で検出し、primary（tidd ai-review --stop-before-merge via STEP 2）と
secondary（`.claude/agents/ai-reviewer.md`）の両方が APPROVE を返したときにのみ最終 APPROVE とする。

一方でも REQUEST_CHANGES を返した場合は `tidd ai-review --consensus-verdict` で判定する（Issue #2657）。
1 回目の不一致はエスカレーションせずリトライを継続し、同一指摘が 2 回連続した場合のみ
`needs-human-merge` ラベルを付与して exit 2 でエスカレーションする。

詳細は `docs/reference/multi-backend-consensus.md` および `docs/reference/review-backends-guide.md` を参照。

## 関連

- `.claude/agents/verdict-extractor.md` — verdict 抽出 subagent
- `tidd_tools/ai_review/core.py` — Python の tidd ai-review（`--stop-before-merge` 実装）
- `docs/reference/ai-review-skill.md` — 詳細ドキュメント
- `.claude/rules/tool-calling.md` — subagent 前提の Tool Calling 設計指針
