# 決定の要約

nightly 失敗の主要因である「dotfiles 変更 → handbook chezmoi golden 割れ」クラスへの追加対策は行わず、現状の仕組み（nightly 検知 → `tidd watch-circleci-failures` 自動起票 → /loop 修理）に任せる。

- **決定日:** 2026-07-21
- **記録者:** Claude（ユーザー指示に基づく）
- **参照:** being-gaia-plan/ai-dev-handbook#2292 #2298 #2302 #2332 / docs/decisions/2026-07-20-upstream-ci-failures-leave-to-loop.md

## 論点

直近 4 件の nightly 失敗を分類した結果、環境差異クラスは燃え尽き型で対策不要、再発源は golden 追随漏れ（4 件中 2 件・直近 2 件連続）。小さな対策で失敗率を下げられるか。

## 提示した選択肢

| 案 | 内容 |
|----|------|
| A | dotfiles CI で handbook golden テストを実行（変更源で即検知）（推奨） |
| B | golden を契約テスト化して薄くする（brittleness 根治・工数中） |
| C | 現状維持（nightly 検知 → 自動起票 → /loop 修理に任せる） |

## ユーザーの決定

**C を採用。**

## 理由・背景

- 検知 → 起票 → /loop 修理のループは実際に機能している（#2332 は同日中に #2333 で修正完了）
- 検知遅延（最大 24h）とタグ取り込みの一時停止は許容する

## 今後 AI が取るべき行動

1. golden 割れ nightly 失敗を見ても dotfiles CI 追加や契約テスト化を再提案しない（再発頻度が明らかに増えた場合のみ再エスカレーション可）
2. タグ取り込み前の nightly GREEN 確認運用（2026-07-20 決定)は継続する
