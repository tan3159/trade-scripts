# 経済指標カレンダーのデータソース調査

Issue #51 論点5（2026-07-22 確定）。用途は FX ドル円アラートシステムの
**指標60分前の新規エントリー禁止ゲート / 30分前の決済リマインド**（発表時刻と重要度のみ必要。
結果値・予想値は不要）。

## 採用: Forex Factory 週間カレンダー JSON フィード

人間は TradingView のカレンダーを見るが、機械取得は Forex Factory フィードが最適（ユーザー決定）。

### 実仕様（2026-07-22 実フェッチで確認済み）

- URL: `https://nfs.faireconomy.media/ff_calendar_thisweek.json`
  （`https://cdn-nfs.faireconomy.media/...` も同内容）
- 認証不要・ブラウザ不要の素の HTTP GET。確認時点で今週分 70 イベント
- スキーマ: `{title, country, date, impact, forecast, previous}`
- `date`: ISO 8601 オフセット付き（例 `2026-07-19T18:45:00-04:00`・NY 時間表示）。
  JST 変換は標準ライブラリ（`datetime.fromisoformat`）で可
- `impact`: `High` / `Medium` / `Low` / `Holiday` の4値
- `country`: 通貨コード（`USD` / `JPY` でフィルタ）

### 比較した候補

| 候補 | 評価 |
|---|---|
| **A. Forex Factory 週間 JSON フィード（採用）** | 無料・認証不要・重要度付き・デファクト。非公式フィードのため仕様変更リスクあり |
| B. investing.com 等のスクレイピング | 情報量は多いが規約グレー・HTML 変更で壊れやすい |
| C. Trading Economics 等の商用 API | 公式で安定だが有料（無料枠は制限が強い） |

### 実装方針

- **週1回取得してローカルキャッシュ**。60分前ゲート / 30分前リマインドはキャッシュ参照で判定
  （actual が載らない点は用途上問題なし）
- フィルタ: `country in (USD, JPY) and impact == High`（Medium を含めるかはキャリブレーション対象)
- **非公式フィードのため取得層を抽象化**し、取得失敗・スキーマ不一致時は沈黙せず
  「カレンダー取得不能」を通知する（判定不能ルールと同じ思想）
- 週またぎ判定（触らない条件ゲート）にも `Holiday` / 金曜クローズ前をこのカレンダーで判定する

## 関連

- [docs/design/fx-usdjpy-alert-system.md](../design/fx-usdjpy-alert-system.md) — Layer 1 ゲート #4
- `docs/decisions/2026-07-22-fx-calendar-forex-factory.md` — 決定記録
