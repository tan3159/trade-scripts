---
root: false
targets:
  - '*'
---
# レビューバックエンド規約

agy / codex / Claude フォールバックの選択基準・exit code 対応・マージ gate。

**関連:** [`.claude/rules/workflow.md`](./workflow.md)
**詳細:** docs/reference/review-backends-guide.md

---

## スキル使い分け

| スキル | 用途 |
|--------|------|
| `/review` | **PR コードレビュー専用**（`agy /review <PR番号>`） |
| `/issue-review` | **Issue 品質チェック専用**（`agy /issue-review <N>`） |

**IMPORTANT: Issue 品質チェックは `agy /issue-review <N>` を使うこと。`/review` は PR 専用であり Issue に対して使ってはならない。**

---

## tidd ai-review exit code 対応表

| code | 意味 | 対応 |
|------|------|------|
| 0 | APPROVE・全消化済み | 自動マージ |
| 1 | REQUEST_CHANGES | 修正して push → 再実行 |
| 2 | エスカレーション | 人間に委ねる |
| 3 | 全バックエンド利用不可 | `ai-fallback-reviewer` subagent を起動（guide 参照）|
| 4 | 人間マージ待ち | `[手動]` 未完了 または Issue やること未消化 → 人間マージ |
| 5 | テスト status gate による中断（#1982） | テスト修正して push → 再実行 |
| 6 | parser critical PR を --stop-before-merge 無しで呼んだ（#3630） | `--stop-before-merge` を付けて再実行 |

**#1982:** commit status に `pytest/*`・`jest/*` の FAILURE があれば呼び出し前に exit 5 で中断（`AI_REVIEW_SKIP_TEST_STATUS_GATE=1` で無効化）。

**#3630:** parser critical PR を `--stop-before-merge` 無しで呼ぶと exit 6 中断（`AI_REVIEW_SKIP_PARSER_CRITICAL_GATE=1`）。

**#3636:** リトライ 2 回目以降の同一 SHA 再レビュー gate（exit 2・`AI_REVIEW_SKIP_NO_NEW_COMMIT_GATE=1`）。前回レビュー以降に新コミットが無いまま attempt >= 2 で再実行すると exit 2 で中断。詳細: review-backends-guide.md。

---

## Issue やること全消化 gate（#1756）

APPROVE 後に `closes #N` の Issue `## やること` に prefix なし未消化項目が残れば **exit 4 + `needs-human-merge` ラベル付与**。全消化なら auto-merge。`PR_AUTO_ADMIN_MERGE` は廃止済み。

---

## yaru auto-tick（#1534）

`AI_REVIEW_YARU_AUTO_TICK=1`（本番）または `dry-run`（監査ログのみ）で evidence-based 自動 tick を有効化する。導入手順詳細は review-backends-guide.md 参照。

---

## scope-diff チェック（#2597）

APPROVE パスで Issue やること・振る舞いと PR diff を突き合わせ、スコープ超過・未消化の可能性を非ブロッキング PR コメントで指摘する。escape hatch: `AI_REVIEW_SKIP_SCOPE_DIFF=1`。
詳細: docs/reference/review-backends-guide.md

---

## custom backend（OpenAI 互換 API・#3116）

`AI_REVIEW_BACKEND=custom` 固定実行時のみ有効。config.json の `custom-backend` + `ai-review-custom`（デフォルト false・opt-in）で設定する。auto フォールバックには非対応。詳細: review-backends-guide.md。

**#3117:** auto チェーン順序は `ai-review-chain` で変更可。詳細は同上。

---

## 関連ドキュメント

- docs/reference/review-backends-guide.md — 詳細（手順・research）
- docs/setup/codex-setup.md — codex セットアップ
- docs/setup/ai-review-credentials.md — 認証情報
