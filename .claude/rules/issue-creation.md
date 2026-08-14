# Issue作成ルール

hookとAgy（`/issue-review`）の両方から参照される単一の真実源。
詳細（合格例・source分類・priority判定・Gherkin品質・prose-only検討）は `docs/reference/issue-creation-guide.md` 参照。

---

## 全Issueに必須のチェック項目

**フォーマット:** `## 背景`・`## やること` 両方が必須。タイトル: `<type>: <説明（動詞止め）>`（`🤖` prefix は不要・過去の付与済みタイトルは後方互換で受理・#2072）。
やること各行は `- [ ]`/`- [x]` 形式（平箇条書き不可）。
**Pain:** `## 背景` に「〇〇ができないせいで△△が起きている」レベルで記述。
**ラベル:**
- `type:`: `feat`/`fix`/`docs`/`refactor`/`ci`/`build`/`research`
- `priority:`: `critical`/`high`/`medium`/`low`
- `source:`: `type: fix` のみ: `ci`/`rework`/`human-report`/`new-bug`/`spec-change`

**粒度:** 1 PR 完結。**ドキュメント更新:** `docs/`・`CLAUDE.md` の更新が `## やること` に含まれるか。
**方針整合性:** `docs/decisions/`・`docs/conventions.md` と矛盾しないか。依存関係は GitHub ネイティブの `blocked-by`（依存先 Issue 番号）で表明でき、未解決の blocker がある Issue は `/issue-next-all` の自動選定から除外される（#3640）。依存理由はコメントに記載（合否不問）。

---

## feat系: 設計の選択肢

`type: feat`: `## 設計の選択肢` に採用案・不採用案（最低1つ）が必須。

---

## feat/fix系: Gherkin品質 / hook契約系除外

**hook 契約系除外:** `## やること` の変更対象が `.claude/hooks/` のみ（かつファイルパス記述あり）→ `## 振る舞い` 不要。代わりに `test_<hookname>_hook.py` 契約テストを書く。

**Gherkin品質基準（検証可能性ゲート）:**
- `## 振る舞い` セクションに Scenario が1つ以上あること
- 正常系・異常系の両方が含まれること（異常系なしは不合格）
- Then 句に観測可能な状態（exit code・出力文字列・ファイル存在等）を示すこと

---

## 期待する出力例（任意セクション）

任意セクション（未記載でもペナルティなし・`validate-issue.py` は存在を強制しない）。出力テキストを貼ると実装 AI がその内容を初回スナップショット/テスト期待値として固定してから実装する。

---

## チェック結果の判定 / hookの動作

**合格条件:** 全チェックをパス。feat系は設計の選択肢も必須。
**コメント形式:** `## Issue品質チェック結果` + ✅/❌ + 不合格項目リスト。
`PreToolUse` hookが `gh issue create` を捕捉してフォーマット・ラベル・セクション存在を機械チェック（詳細: `hooks.md#validate-issuepy`）。

---

## 関連ドキュメント

- `docs/reference/issue-creation-guide.md` — 詳細ガイド
- `docs/conventions.md` — 規約
- `docs/reference/hooks.md` — Hooks リファレンス
