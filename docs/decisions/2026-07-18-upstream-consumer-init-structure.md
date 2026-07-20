# 決定の要約

新規 consumer 立ち上げの省力化は、配布物の個別改善 8 件をそのまま起票するのではなく、`tidd consumer-init` 一発コマンドを親 Issue とし、配布物・部品改善を子 Issue とする構成で上流 ai-dev-handbook に起票する。

- 決定日: 2026-07-18
- 記録者: Claude Code（ユーザー回答の自動記録）
- 参照: trade-scripts 環境監査（#21〜#24）・上流 adoption guide / #2219 / #2220

## 論点

trade-scripts 立ち上げで 9 Issue 分の手作業が発生した。次の新規リポジトリを最小手間にするための上流改善の Issue 構成をどうするか。

## 提示した選択肢

| 案 | 内容 | 備考 |
|----|------|------|
| A | `tidd consumer-init` 親 Issue + 配布物の子 Issue | 採用。立ち上げが実質 1〜2 ステップまで縮む |
| B | 当初の洗い出し 8 件をフラットに個別起票 | コマンド統合による更なる省力化が進まない |
| C | 起票せず trade-scripts 側に runbook のみ配置 | 上流のコピー作業削減が進まない |

## ユーザーの決定

A（consumer-init 親 + 子 Issue 構成）。

## 理由・背景

ユーザーは「ステップ数もっと減らすことはできない？」と更なる削減を要望。部品（repo-bootstrap / copier-update / health-check）が上流に既存のため、オーケストレーションコマンドへの統合が現実的と判断。

## 今後 AI が取るべき行動

- 上流への consumer 省力化提案は consumer-init への統合を前提に構成する
- 新規 consumer 立ち上げ時は consumer-init の実装状況を確認し、未実装部分のみ手動 runbook を使う
