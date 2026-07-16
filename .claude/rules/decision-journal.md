# 判断ジャーナル規約

エスカレーション（選択肢形式）にユーザーが回答したら、AI はそのセッション内で
[docs/decisions/YYYY-MM-DD-<slug>.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/decisions/YYYY-MM-DD-<slug>.md) に決定を自動記録する。
**人間への指示不要。AI が自動追記する。**

---

## いつ記録するか（3 条件）

1. **エスカレーション回答時** — どの選択肢を選んだか・条件・理由
2. **壁打ちで採用 / 不採用 / 条件付き採用を決定したとき**
3. **同種の質問に方針を示したとき** — 以後 AI が同じ質問を繰り返さないために記録

ファイル命名: [docs/decisions/YYYY-MM-DD-<slug>.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/decisions/YYYY-MM-DD-<slug>.md)（1 決定 1 ファイル）

---

## 記録ファイルの構成

`# 決定の要約` + ヘッダ（決定日・記録者・参照）> `## 論点` > `## 提示した選択肢`（表）> `## ユーザーの決定` > `## 理由・背景` > `## 今後 AI が取るべき行動`

テンプレート全文・ファイル命名・コミット方法・記録不要ケース: [docs/reference/decision-journal-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/decision-journal-guide.md)

---

## 関連

- `.claude/rules/escalation-format.md` — 回答を受けたら本規約に従って記録する
