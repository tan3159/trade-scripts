# Technology Radar 運用ガイド

AI 技術の採用ライフサイクルを ThoughtWorks BYOR（Build Your Own Radar）形式で管理するための運用ガイド。


> **Technology Radar は未設定です。** セットアップ手順は `docs/setup/technology-radar.md` を参照してください。Radar を利用する場合は `.copier-answers.yml` の `technology_radar_url` に URL を設定して `tidd copier-update` を実行してください。


**データソース:** Google スプレッドシート（`radar` シート）→ `radar-export` GAS が secret Gist に CSV を週次 PATCH → BYOR が CSV を読み取って描画

---

## なぜ Technology Radar を使うのか

- 新しい AI ツール・手法が次々登場するため、「いま何を試している／何を採用済みか」を可視化したい
- 実装着手前に「既存の調査済みソリューションがあるか」を確認する基点として使う（`.claude/rules/workflow.md` 参照）
- 月次リサーチ（Issue #3 系）で出てきた候補を `検討` として追加し、評価が進むにつれて `試行` → `推進` へリングを移動させる

---

## Quadrant（象限）

| # | 名前 | 範囲 | 例 |
|---|------|------|----|
| 1 | **AIツール** | AIエージェント・LLMを活用したツール | Claude Code, agy, Codex, Gemini CLI |
| 2 | **業務フロー** | 人間×AIの働き方・品質・手順 | TiDD, BDD/TDD, issue品質保証, 月次リサーチ |
| 3 | **自動化** | AIの自律動作・制御・耐障害 | self-improving loop, auto-resume, fallback戦略 |
| 4 | **環境** | 開発を支えるツール・外部サービス・基盤 | bats, mise, chezmoi, Slack, GitHub, GAS |

## Ring（リング）

| 名前 | 意味 | 行動指針 |
|------|------|---------|
| **推進** | 積極的に使っている・チームに勧められる | 新規案件でデフォルト採用する |
| **試行** | 試験的に導入中・有望だが確信はまだ | 小規模で使って知見を積む |
| **検討** | 注目しているが本格導入前 | 調査・PoC レベル。慎重に評価する |
| **保留** | 見送り・現時点では採用しない | 使わない。理由を記録しておく |

> Quadrant・Ring の定義は Issue #523 で確定。詳細は [`technology-radar-definitions.md`](./technology-radar-definitions.md) を参照。

---

## 登録基準

新しいブリップ（項目）を追加するとき、以下の基準でリングを判定する。

| Ring | 登録基準 |
|------|---------|
| **推進** | 実運用で効果が確認されており、他メンバーにも勧められる状態 |
| **試行** | 本リポジトリで実際に使い始めており、評価が進行中 |
| **検討** | `docs/research/` に調査ファイルがある、または月次リサーチで候補に上がった |
| **保留** | 評価したが採用しなかった。または過去は推進/試行だったが外した |

---

## 更新タイミング

- **月次リサーチ（Issue #3 系）の結果を反映する**
  - 月次リサーチで挙がった候補を `検討` として追加する
  - 既存ブリップのリング移動も同タイミングで検討する
- **新ツール・手法の導入時に「検討」として追加する**
  - 着手 Issue を立てた段階でブリップを追加する
- **3ヶ月以上 `検討` のままのアイテムは月次リサーチで再評価する**
  - 評価が進まないまま放置されているものは「保留」に移すか、Issue 化して試行に進める

---

## 運用フロー

### ブリップを追加する

1. Google スプレッドシート（`radar` シート）を開く
2. 新しい行を追加し、以下のカラムを埋める:
   - `name`: ブリップ名（例: `Claude Code`）
   - `ring`: `推進` / `試行` / `検討` / `保留` のいずれか
   - `quadrant`: `AIツール` / `業務フロー` / `自動化` / `環境` のいずれか
   - `isNew`: 直近 3 ヶ月以内に追加・移動したかどうか（`TRUE` / `FALSE`）
   - `description`: 短い説明（複数行可、改行は `\n` で記述）
3. 翌週月曜 9 時の `radar-export` 週次トリガーで自動的に secret Gist に反映される
4. 即時反映したい場合は GAS スクリプトエディタで `exportToGist` を手動実行する

### リングを変更する（試行 → 推進など）

1. スプレッドシートの該当行で `ring` カラムを書き換える
2. `isNew` を `TRUE` にする（移動を明示するため）
3. 必要に応じて `description` に変更理由を追記する
4. 週次トリガー、または手動実行で反映する

### ブリップを削除する

- 「使わなくなった」場合は削除せず、リングを `保留` に移すのが原則（履歴を残す）
- どうしても削除する場合は行ごと削除し、削除理由をコミットログ相当として残す方法を別途検討する（現状は手動運用）

---

## システム構成

```
[Google Sheets] ──→ [radar-export GAS] ──週次 PATCH──→ [secret Gist] ──→ [BYOR] ──→ [Technology Radar HTML]
   radar シート         (projects/gas/             radar.csv          GitHub Pages
                        radar-export/)
```

| コンポーネント | 役割 | 場所 |
|---|---|---|
| Google スプレッドシート | データ入力先 | スプレッドシートの `radar` シート |
| `radar-export` GAS | スプレッドシート → secret Gist 同期 | `projects/gas/radar-export/Code.js` |
| secret Gist | CSV 公開（非公開 URL） | GitHub Gist（URL 漏洩リスク回避のため secret） |
| BYOR | レーダー描画 | `tan3159/build-your-own-radar`（GitHub Pages） |

セットアップ手順は [`docs/setup/technology-radar.md`](../setup/technology-radar.md) を参照。

---

## 関連ドキュメント

- [Technology Radar 定義](./technology-radar-definitions.md) — Quadrant・Ring の正式定義
- [Technology Radar セットアップ手順](../setup/technology-radar.md) — 初回設定・スプレッドシート共有設定
- [ワークフロー規約](../../.claude/rules/workflow.md) — 「既存ソリューションを探す」ステップでレーダーを参照する
