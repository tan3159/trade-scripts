# GitHub リポジトリ設定（TiDD 運用向け）

Issue #2 で適用した設定の記録と再現手順。**public リポジトリ**前提でセキュリティ機能を最大限有効化している。
設定はすべて gh CLI（REST API）で適用しており、本ドキュメントのコマンドで再現できる。

## 適用済み設定一覧（2026-07-17）

| 分類 | 設定 | 値 | 目的 |
|------|------|----|----|
| マージ | squash merge | ✅ 許可（唯一） | 1 PR = 1 コミットの履歴規約 |
| マージ | merge commit / rebase merge | ❌ 無効 | 同上 |
| マージ | squash コミットメッセージ | PR タイトル + 本文 | `closes #N` を確実にコミットへ残す |
| マージ | delete branch on merge | ✅ | マージ後ブランチ自動削除（tidd 運用前提） |
| セキュリティ | secret scanning | ✅（public は常時有効） | 機密情報 push の検知 |
| セキュリティ | secret scanning push protection | ✅ | 機密情報を **push 時点でブロック** |
| セキュリティ | vulnerability alerts | ✅ | 依存脆弱性の通知 |
| セキュリティ | Dependabot security updates | ✅ | 脆弱性依存の自動修正 PR |
| 保護 | ruleset `protect-main` | ✅ active | 下記参照 |

secret scanning non-provider patterns / validity checks は REST API 経由で有効化できなかったため未設定（ベストエフォート項目。Web UI から有効化可能になり次第追加する）。

## ruleset `protect-main`（default branch 対象）

- **deletion 禁止** — main の削除を防ぐ
- **non_fast_forward 禁止** — force push を防ぐ
- **pull_request 必須** — main への直接 push を禁止。approving review 0 件・squash マージのみ許可（tidd ai-review の auto-merge と整合）
- bypass actor なし（break-glass が必要な場合は ruleset を一時 disable する）

将来 CI（Issue #8）導入後、required status checks を本 ruleset に追加する。

## 再現手順

```bash
REPO=<owner>/<repo>

# マージ設定
gh api -X PATCH "repos/$REPO" \
  -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false \
  -F delete_branch_on_merge=true \
  -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY

# セキュリティ機能
gh api -X PUT "repos/$REPO/vulnerability-alerts"
gh api -X PUT "repos/$REPO/automated-security-fixes"
echo '{"security_and_analysis":{"secret_scanning_push_protection":{"status":"enabled"}}}' \
  | gh api -X PATCH "repos/$REPO" --input -

# ruleset
gh api -X POST "repos/$REPO/rulesets" --input - <<'JSON'
{
  "name": "protect-main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": false,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": false,
      "allowed_merge_methods": ["squash"]
    }}
  ]
}
JSON
```

## 検証コマンド

```bash
gh api "repos/$REPO" --jq '{squash:.allow_squash_merge,merge:.allow_merge_commit,rebase:.allow_rebase_merge,del:.delete_branch_on_merge,sec:.security_and_analysis}'
gh api "repos/$REPO/rulesets" --jq '.[].name'
```

## ラベル

TiDD 必須ラベル 28 種（`type:` / `priority:` / `source:` / `size/*` / `needs-human-merge` /
`duplicate-suspect` / `status: blocked` / `🙋 needs-human-input`）は 2026-07-17 に gh CLI で作成済み。
ラベル定義の単一ソース化（consumer ブートストラップ）は上流 Issue（ai-dev-handbook#2219）で検討中。
