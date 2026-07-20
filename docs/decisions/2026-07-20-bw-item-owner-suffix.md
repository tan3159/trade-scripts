# 決定の要約

per-repo アカウント切替（#23 の恒久化）は、bw アイテム名の suffix を **repo owner 名**（`git remote get-url origin` の owner 部分）に統一し、bashrc の `_load_ai_review_secrets` が remote から owner を導出してアイテムを選択する方式で実現する。.envrc / direnv は使わない。

- **決定日:** 2026-07-20
- **記録者:** Claude（ユーザー指示に基づく）
- **参照:** tan3159/trade-scripts#23 / being-gaia-plan/s-tanaka-dotfiles#43 / docs/decisions/2026-07-19-per-repo-account-separation.md

## 論点

2026-07-19 の per-repo アカウント分離決定で「恒久化は .envrc（direnv）」としていたが、ユーザーが .envrc を却下（「.envrc は使いたくない。引き続き Bitwarden でやりたい」）。Bitwarden のみで per-repo 切替をどう実現するか。

## 提示した選択肢

| 案 | 内容 | 備考 |
|----|------|------|
| A | bw アイテム suffix = owner 名に統一・remote から自動導出 | 引数不要・map 不要・remote（SSH エイリアス分離済み）が単一の真実源 |
| B | suffix = アカウント名維持 + bashrc に org→アカウント map | bw リネーム不要だが map のメンテが増える |

（前段の選択肢: 関数引数で suffix 指定 / PWD 自動判定 cd フック — owner 名導出案の提示により統合）

## ユーザーの決定

**A を採用。** suffix は正確には「org 名」ではなく「repo owner 名」（個人アカウントでも org でも remote URL の同じ位置）であることを確認済み（tan3159 は個人アカウント・being-gaia-plan は org）。

## 理由・背景

- 認証情報は Bitwarden のみで管理する方針（CLAUDE.md セキュリティ原則・.envrc/direnv 却下）
- bashrc `_load_ai_review_secrets` は `GH_TOKEN_ITEM` / `GITHUB_MCP_PAT_ITEM` の env 上書きに対応済みで、suffix 導出の拡張だけで済む
- 同一アカウントで複数 owner にアクセスする場合は owner ごとに bw アイテムが必要（同じ PAT の重複保持）。現状は 2 owner（tan3159 / being-gaia-plan）のみで問題なし
- suffix 付与対象は `gh-token-*` / `github-mcp-pat-*` のみ。GitHub App 系（app-id 等）は共有のため suffix なし

## 今後 AI が取るべき行動

1. .envrc / direnv / ファイルへの認証情報記述を提案しない（Bitwarden + bashrc 関数のみ）
2. 実装分担: 導出ロジックと既存アイテムリネーム（`-s-tanaka` → `-being-gaia-plan`）は dotfiles#43、tan3159 側 bw アイテム作成と MCP 接続検証は trade-scripts#23
3. 配布 docs（secrets-management.md 等）の修正が必要になったら ai-dev-handbook に起票し copier update で反映する（上流大原則）
