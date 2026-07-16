# Issue作成ルール

hookとAgy（`/issue-review`）の両方から参照される単一の真実源。
合格例・source 分類詳細・priority 相対判定・Gherkin 品質基準詳細は
[docs/reference/issue-creation-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/issue-creation-guide.md) を参照。

---

## 全Issueに必須のチェック項目

### フォーマット

| 項目 | 判断基準 |
|------|---------|
| セクション | `## 背景` と `## やること` の両方が存在するか |
| タイトル形式 | AI作成の場合: `🤖 <type>: <説明（動詞止め）>`（🤖プレフィックス必須） |
| やること形式 | 各行が `- [ ]` または `- [x]` 形式（平箇条書き `- タスク名` は不可） |

### Pain（背景の記述品質）

`## 背景` に「〇〇ができないせいで△△が起きている」レベルで Pain が書かれているか。
合格例・不合格例は [docs/reference/issue-creation-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/issue-creation-guide.md) 参照。

### ラベル

| 項目 | 判断基準 |
|------|---------|
| `type:` ラベル | `type: feat` / `fix` / `docs` / `refactor` / `ci` / `build` / `research` のいずれか |
| `priority:` ラベル | `priority: critical` / `high` / `medium` / `low` のいずれか |
| `source:` ラベル | **🤖 + `type: fix` の場合のみ**: `source: ci` / `rework` / `human-report` / `new-bug` / `spec-change` のいずれか（人間起票は対象外）。5 分類定義・優先順位は [docs/reference/issue-creation-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/issue-creation-guide.md) 参照 |

priority 相対判定（`/create-issue` skill が分布提示・`issue-writer` subagent が相対評価で選択）の基準は
[docs/reference/issue-creation-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/issue-creation-guide.md) 参照。

### 粒度・依存関係・ドキュメント更新・方針整合性

| 項目 | 判断基準 |
|------|---------|
| 粒度 | 1つのPRで完結するか（複数の関心事が混在していないか） |
| 依存関係 | 依存Issueの状態を確認してコメントに記載（合否に影響しない） |
| ドキュメント更新 | 実装に伴う `docs/`・`CLAUDE.md` 等の更新が `## やること` に含まれているか（意味チェック） |
| 方針整合性 | `docs/decisions/`・`docs/conventions.md`・`CLAUDE.md` の方針と矛盾していないか |

---

## feat系Issueの追加チェック

`type: feat` ラベルのIssueは以下を追加確認する。

`## 設計の選択肢` セクションに採用案・不採用案（最低1つ）が存在するか。

---

## feat/fix系Issueの追加チェック

`type: feat` または `type: fix` ラベルのIssueは以下を追加確認する。

### hook 契約系 Issue の除外（Issue #1855）

**`## やること` の変更対象が `.claude/hooks/` のみの Issue は `## 振る舞い` 不要。**
代わりに `test_<hookname>_hook.py` 契約テストを書く。

**除外条件（両方を満たすこと）:**
1. `## やること` 内のファイルパス参照が `.claude/hooks/` のみ（他パスが混在しない）
2. `## やること` にファイルパスの記述が存在する

### Gherkin品質基準（検証可能性ゲート）

| 項目 | 判断基準 |
|------|---------|
| セクション存在 | `## 振る舞い` セクションが存在するか |
| Scenario数 | Scenario が1つ以上あるか |
| 正常系・異常系 | 正常系・異常系の**両方**が含まれているか（異常系なしは不合格） |
| Then句 | 具体的な値・観測可能な状態（exit code・出力文字列・ファイル存在等）を示しているか |

詳細（合格例・不合格例・critical モジュール境界値ルール・positive list）は
[docs/reference/issue-creation-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/issue-creation-guide.md) 参照。

---

## チェック結果の判定

**合格条件:** 全チェックをパス。feat系は設計の選択肢チェックも必須。

**コメント形式:** `## Issue品質チェック結果` + ✅/❌ + 不合格項目リスト。
テンプレートは [docs/reference/issue-creation-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/issue-creation-guide.md) 参照。

---

## hookの動作（Claude作成時）

`PreToolUse` hookが `gh issue create` コマンドを捕捉し、以下を機械的にチェックする:

1. `## 背景`・`## やること` セクション存在
2. タイトルが `🤖 <type>: <説明>` 形式か（🤖プレフィックス必須）
3. `## やること` 各行が `- [ ]` または `- [x]` 形式か
4. feat系: `## 設計の選択肢` セクション存在
5. feat/fix系: `## 振る舞い` セクション存在（hook契約系 Issue は除外）

不備があればコマンドをブロックし、Claudeが修正して再実行する。
実装詳細は [docs/reference/hooks.md#validate-issuepy](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/hooks.md#validate-issuepy) 参照。

---

## 関連ドキュメント

- [docs/reference/issue-creation-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/issue-creation-guide.md) — 詳細ガイド（合格例・source分類・Gherkin品質）
- [`docs/conventions.md`](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/conventions.md) — Issue・PR・コミット規約
- [docs/reference/hooks.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/hooks.md) — Hooks リファレンス
- [`workflow.md`](./workflow.md) — ワークフロー規約
