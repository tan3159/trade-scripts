# Issue作成ルール

hookとAgy（`/issue-review`）が参照する単一の真実源。詳細: docs/reference/issue-creation-guide.md

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
合格例・不合格例は docs/reference/issue-creation-guide.md 参照。

### ラベル

| 項目 | 判断基準 |
|------|---------|
| `type:` ラベル | `type: feat` / `fix` / `docs` / `refactor` / `ci` / `build` / `research` のいずれか |
| `priority:` ラベル | `priority: critical` / `high` / `medium` / `low` のいずれか |
| `source:` ラベル | **🤖 + `type: fix` のみ**: `ci`/`rework`/`human-report`/`new-bug`/`spec-change` のいずれか（人間起票は対象外・分類詳細は guide 参照） |

priority 相対判定の基準は docs/reference/issue-creation-guide.md 参照。

### 粒度・依存関係・ドキュメント更新・方針整合性

| 項目 | 判断基準 |
|------|---------|
| 粒度 | 1つのPRで完結するか（複数の関心事が混在していないか） |
| 依存関係 | 依存Issueの状態をコメントに記載（合否に影響しない）。`blocked-by` は未解決依存を自動選定から除外（#3640） |
| ドキュメント更新 | `docs/`・`CLAUDE.md` 等の更新が `## やること` に含まれているか |
| 方針整合性 | `docs/decisions/`・`conventions.md`・`CLAUDE.md` と矛盾していないか |

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

**除外条件（両方満たす）:** 1) パス参照が `.claude/hooks/` のみ 2) パス記述が存在する

### Gherkin品質基準（検証可能性ゲート）

| 項目 | 判断基準 |
|------|---------|
| セクション存在 | `## 振る舞い` セクションが存在するか |
| Scenario数 | Scenario が1つ以上あるか |
| 正常系・異常系 | 正常系・異常系の**両方**が含まれているか（異常系なしは不合格） |
| Then句 | 具体的な値・観測可能な状態（exit code・出力文字列等）を示しているか |

詳細（合格例・不合格例・critical モジュール境界値ルール・positive list）は
docs/reference/issue-creation-guide.md 参照。

---

## 期待する出力例（任意セクション）

`## 期待する出力例` は任意（未記載でもペナルティなし）。
出力テキスト（Markdown 表・JSON・ログ行等）を貼ると、実装 AI がそれをスナップショット/テスト期待値として固定するため diff で確認しやすくなる。
合格例・hook の動作詳細は
docs/reference/issue-creation-guide.md 参照。

---

## チェック結果の判定

**合格条件:** 全チェックをパス。feat系は設計の選択肢チェックも必須。

**コメント形式:** `## Issue品質チェック結果` + ✅/❌ + 不合格項目リスト。
テンプレートは docs/reference/issue-creation-guide.md 参照。

---

## hookの動作（Claude作成時）

`PreToolUse` hookが `gh issue create` を捕捉し、上記チェック項目を機械的に検証する。
不備があればブロックし、Claudeが修正して再実行する。詳細: docs/reference/hooks.md#validate-issuepy

---

## 関連ドキュメント

- docs/reference/issue-creation-guide.md — 詳細ガイド
- `docs/conventions.md` — Issue・PR・コミット規約
- [`workflow.md`](./workflow.md) — ワークフロー規約
