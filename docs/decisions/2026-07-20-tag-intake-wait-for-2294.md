# 決定の要約

次期上流タグは #2294（run-project-tests の uv run 化）のマージを待ってから切り、#2274〜#2277 の成果と合わせて 1 回の copier update で取り込む。#27/#28 の先行着手はしない。

- **決定日:** 2026-07-20
- **記録者:** Claude（ユーザー指示に基づく）
- **参照:** being-gaia-plan/ai-dev-handbook#2294 / tan3159/trade-scripts#27 #28 / docs/decisions/2026-07-20-upstream-ci-failures-leave-to-loop.md

## 論点

#2292（nightly 失敗）が解消され #2294 が /loop 処理中。#27/#28 のタグ待ちを解消するタグをいつ切るか。

## 提示した選択肢

| 案 | 内容 |
|----|------|
| A | #2294 マージ後に新タグ → 一括取り込み（推奨）。タグ作業 1 回・uv tool pytest も同時撤去可 |
| B | 今すぐ新タグで #27/#28 先行。早いがタグ作成〜nightly GREEN 確認の一連が 2 回 |

## ユーザーの決定

**A を採用。**

## 理由・背景

- #2294 は /loop 処理中で待ちは短い見込み
- タグ取り込みは nightly GREEN 確認を含む一連作業のため回数を減らしたい

## 今後 AI が取るべき行動

1. #2294 の CLOSED を確認してから新タグ作成を提案する（タグ時点の nightly GREEN 確認必須）
2. copier update 後、uv tool の pytest/mypy/ruff/mutmut を `uv tool uninstall` で撤去する（#2294 反映でグローバル pytest 不要になるため）
3. 本ジャーナルはタグ取り込み Issue の PR に同乗コミットする
