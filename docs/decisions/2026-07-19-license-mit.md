# 決定の要約

trade-scripts のライセンスは MIT を採用する（Issue #26）。

- **決定日:** 2026-07-19
- **記録者:** Claude（ユーザー回答に基づく）
- **参照:** tan3159/trade-scripts#26

## 論点

public リポジトリに LICENSE がなく all rights reserved 扱い。どのライセンスを採用するか。

## 提示した選択肢

| 案 | 内容 |
|---|---|
| A（推奨） | MIT — 最短・最も一般的。OSS 向け無料枠（CodeRabbit 等）の前提も満たす |
| B | Apache-2.0 — 特許条項付きだが個人トレードスクリプトには過剰 |
| C | 現状維持（ライセンスなし） |

## ユーザーの決定

**A**（MIT）

## 理由・背景

個人リポジトリで特許条項の必要性が低く、public repo 前提サービスの OSS 無料枠適用を明確にすることが主目的。

## 今後 AI が取るべき行動

1. #26 実装時は MIT License 全文（copyright holder: tan3159）で LICENSE を作成する
2. ライセンス選定の再エスカレーションをしない
