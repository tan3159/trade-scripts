# 決定の要約

上流 ai-dev-handbook の CI 失敗 Issue（例: #2292 nightly-tests 失敗）は handbook 側 /loop に任せ、trade-scripts セッションからは介入しない。trade-scripts 側の防御は「次回タグ取り込み時に nightly GREEN を確認する」運用で行う。

- **決定日:** 2026-07-20
- **記録者:** Claude（ユーザー指示に基づく）
- **参照:** being-gaia-plan/ai-dev-handbook#2292

## 論点

handbook の nightly-tests が failing（#2292・再発 2 回）。trade-scripts は次の上流タグに依存する（#27/#28 等）ため無関係ではない。trade-scripts セッションから調査・支援すべきか。

## 提示した選択肢

| 案 | 内容 |
|----|------|
| A | /loop に任せて静観。次回タグ取り込み時に nightly GREEN を確認（推奨） |
| B | read-only で原因調査し所見を #2292 にコメント（二重着手・推測ノイズのリスク） |

## ユーザーの決定

**A を採用。**

## 理由・背景

- /loop は #2274〜#2288 を全消化しており健全。#2292 は needs-human-input なし・priority high で自動着手される見込み
- 上流 CircleCI/tidd_tools テストは handbook 環境の管轄で、trade-scripts セッションからの介入は二重着手リスクがある

## 今後 AI が取るべき行動

1. 上流の CI 失敗 Issue を見つけても trade-scripts セッションから調査・修正・コメントをしない（/loop に任せる）
2. copier update でタグを取り込む際は、そのタグ時点の nightly（または main CI）が GREEN であることを確認してから取り込む
3. 上流 CI が長期間赤いままでタグ待ち Issue（#27/#28 等）が停滞する場合のみ、ユーザーにエスカレーションする
