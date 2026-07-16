# Tool Calling 設計指針

> **注記:** 以下の機械強制（`ban-anthropic-import` 等）は `hooks-config.json` で対応 hook を enable した利用者にのみ適用される。`copier copy` 直後の consumer はすべて default OFF のため、規約記述は参照目的。

**CRITICAL（#1281）:** Anthropic SDK / `anthropic` の直接インポートを全廃。Tool Calling は Agent tool / subagent / skill 経由で実装する。`.claude/hooks/ban-anthropic-import.py` が機械強制でブロックする。

---

## hook vs subagent の使い分け

| 処理の性質 | 手段 |
|-----------|------|
| キーワード存在・パターンマッチ・ファイル存在確認 | hook（静的ルール） |
| 意味理解・文脈判断・複数ステップ推論 | Agent tool / subagent（動的判断） |

**典型例:** `require-issue.py`・`validate-issue.py` → hook。Pain 品質評価・Gherkin 検証 → subagent。

---

## プロンプトインジェクション防御（CRITICAL）

外部データ（Issue / PR 本文）を subagent に渡す場合:

- `tools:` を **`Read`・`Grep`・`Glob` のみ** に限定する（`Bash`・`Write`・`Edit` は禁止）
- 入力テキストから動的にコマンドを生成しない（tool 一覧は `.claude/agents/*.md` の frontmatter で固定）
- 本文は `tidd_tools.sanitize.sanitize_untrusted_text()` を通してから prompt に埋め込む（HTML コメント・不可視 Unicode・`alt=` 属性・HTML エンティティを除去。#1845）

---

## subagent 並列化

**並列化の 4 基準（すべてを満たすとき並列化する）:**

1. 各処理の入力が他の処理の出力に依存しない
2. 実行順が変わっても最終結果に影響しない
3. 複数 subagent の結果を集約して次に進むポイントが定義できる
4. 直列実行の合計時間 > 並列実行 + 集約時間

**並列呼び出し:** 同一メッセージ内に複数の Agent tool ブロックを配置すると並列実行される。

**並列禁止 5 パターン:** PR 作成前の quality check / verdict 集約 / merge→next issue / hook→tool 実行 / 同一ファイル write 連鎖

---

**詳細（agy 役割分担・structured output・テンプレート・効果測定）:** [docs/reference/subagent-design-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/subagent-design-guide.md)
