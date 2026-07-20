# 決定の要約

trade-scripts の操作は tan3159 権限で行い、アカウント使い分けはリポジトリ（パス）単位の仕組みで実現する: git は SSH ホストエイリアス、gh は GH_TOKEN、github MCP はプロジェクトごとの PAT。認証情報はすべて Bitwarden 管理（.envrc / direnv は使わない）。グローバルの `gh auth switch` は使わない。

- **決定日:** 2026-07-19
- **記録者:** Claude（ユーザー指示に基づく）
- **参照:** tan3159/trade-scripts#23 / #29

## 論点

trade-scripts の push が active アカウント（s-tanaka-being）の HTTPS + OAuth token で行われ、workflow scope 不足で拒否された。リポジトリごとにアカウントを使い分ける恒久的な仕組みをどうするか。

## 決定内容

1. **git push/fetch:** trade-scripts の origin を `git@github-tan3159:tan3159/trade-scripts.git`（SSH エイリアス）へ切替済み。SSH 鍵は OAuth workflow scope 制限の対象外のため `.github/workflows/` 変更も push 可能。HTTPS には戻さない
2. **gh API:** `gh auth switch` は他セッション（ai-dev-handbook の /loop）に影響するため使用禁止。当面はコマンド単位で `GH_TOKEN=$(gh auth token -u tan3159)` 前置。恒久化は #23 で Bitwarden ベース（bashrc の `_load_ai_review_secrets` + `GH_TOKEN_ITEM` 上書き）で行う
3. **github MCP:** サーバー起動時 PAT 固定のため、per-repo 化は `GITHUB_MCP_PAT_ITEM` 環境変数で bw アイテム `ai-review/github-mcp-pat-tan3159`（要作成）を指す方式で行う（#23）

## 理由・背景

- SSH エイリアス（github-tan3159 / github-being）と鍵は dotfiles で構築済み。handbook は SSH 運用済みで trade-scripts のみ移行漏れだった
- 両アカウントの OAuth token は workflow scope なし。scope 追加は /loop 稼働中のアカウント切替リスクがあるが、SSH 化により不要になった
- **2026-07-19 追記:** 当初提案の .envrc（direnv）方式はユーザーが却下（「.envrc は使いたくない。引き続き Bitwarden でやりたい」）。認証情報・環境変数の恒久化は bw + bashrc 関数（既存の `_load_ai_review_secrets` の item 上書き機構）で行う

## 今後 AI が取るべき行動

1. trade-scripts の remote を HTTPS に戻す提案をしない
2. `gh auth switch` によるグローバル切替を提案しない
3. #23 着手時は Bitwarden ベース（`GH_TOKEN_ITEM` / `GITHUB_MCP_PAT_ITEM` で bw アイテムを指す）で実装する。.envrc / direnv は提案しない
