# 決定の要約

public repo 監査で拾い出したセキュリティ・ワークフロー改善は案 A（推奨セット全部を Issue 化）を採用。ただし仕組みはすべて ai-dev-handbook 側で実装し（copier flag + repo-bootstrap 拡張）、trade-scripts 側は copier 反映後に flag 切替のみ行う。

- **決定日:** 2026-07-19
- **記録者:** Claude（ユーザー回答に基づく）
- **参照:** being-gaia-plan/ai-dev-handbook#2274-#2277 / tan3159/trade-scripts#26-#28

## 論点

public リポジトリで無料化される機能（CodeQL・CodeRabbit・PVR・secret scanning 拡張・Scorecard・LICENSE）の未活用ギャップをどの範囲・どの形で Issue 化するか。

## 提示した選択肢

| 案 | 内容 |
|---|---|
| A（推奨） | 推奨セット 6 項目を一括起票（community files は見送り） |
| B | CodeRabbit + CodeQL + LICENSE の最小 3 件のみ |
| C | 個別選択 |

## ユーザーの決定

**A**。追加指示: スクリプト・設定の on/off 切替を含む仕組みは ai-dev-handbook 側に起票して実装し、trade-scripts には copier で反映して flag を変えるだけにする。

## 理由・背景

- consumer 側に独自ファイル・独自スクリプトを置くと copier update と衝突し、次の consumer で同じ作業を繰り返すことになる
- CodeRabbit は advisory とし、auto-merge gate は tidd ai-review を維持する（required check 化は #21 と合わせて将来検討）

## 起票結果

| 場所 | Issue | 内容 |
|---|---|---|
| 上流 | #2274 | copier flag で .coderabbit.yaml 配布 |
| 上流 | #2275 | repo-bootstrap に CodeQL default setup・PVR・secret scanning 拡張 |
| 上流 | #2276 | copier flag で SECURITY.md 配布 |
| 上流 | #2277 | copier flag で Scorecard workflow 配布 |
| 本repo | #26 | LICENSE 追加（ライセンス選定はユーザー判断） |
| 本repo | #27 | CodeRabbit 有効化（#2274 + #24 依存） |
| 本repo | #28 | セキュリティ設定有効化（#2275-#2277 + #24 依存） |

## 今後 AI が取るべき行動

1. public repo 向け機能の追加要望が出たら、まず上流（copier flag / repo-bootstrap）での実装を検討し、consumer 直置きを提案しない
2. #27・#28 は上流実装 + タグリリース + #24 完了まで着手しない
3. CodeRabbit と tidd ai-review の gate 二重化を提案しない（advisory 運用が決定事項）
