# レビューバックエンド規約

agy / codex / Claude フォールバックの選択基準・exit code 対応・マージ gate。

**関連:** [`.claude/rules/workflow.md`](./workflow.md)
**詳細:** [docs/reference/review-backends-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/review-backends-guide.md)

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

---

## Issue やること全消化 gate（#1756）

APPROVE 後に `closes #N` の Issue `## やること` に prefix なし未消化項目が残れば **exit 4 + `needs-human-merge` ラベル付与**。全消化なら auto-merge。`PR_AUTO_ADMIN_MERGE` は廃止済み。

---

## yaru auto-tick（#1534）

`AI_REVIEW_YARU_AUTO_TICK=1`（本番）または `dry-run`（監査ログのみ）で evidence-based 自動 tick を有効化する。導入手順詳細は [review-backends-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/review-backends-guide.md) 参照。

---

## 関連ドキュメント

- [docs/reference/review-backends-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/review-backends-guide.md) — 詳細（手順・research）
- [docs/setup/codex-setup.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/codex-setup.md) — codex セットアップ
- [docs/setup/ai-review-credentials.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/ai-review-credentials.md) — 認証情報
