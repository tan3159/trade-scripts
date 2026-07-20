# 決定の要約

s-tanaka-dotfiles の open Issue/PR 全処理にあたり 3 件を決定: 未コミット diff は退避後破棄、#35 は suffix テンプレート定数化、#22 は案 A（.ps1 並列配置）。

- **決定日:** 2026-07-19
- **記録者:** Claude（ユーザー回答に基づく）
- **参照:** being-gaia-plan/s-tanaka-dotfiles#35 / #36 / #22 / #39-#42

## 論点

1. dotfiles main の未コミット diff（watch-main-tests 用 WATCH_MAIN_TESTS_ENABLED 再追加）— bacf013 で意図的に削除済みの残骸。破棄してよいか
2. #35（bw アイテム名 suffix の CWD 非依存化）— Issue 記載の `git config --global` 参照案は #36（global noreply 化）と衝突する
3. #22（chezmoi スクリプト 5 個の Windows 対応方針）— Issue 記載の 3 案から選定

## 提示した選択肢

| 論点 | 選択肢 | 決定 |
|---|---|---|
| 1 | A: 即破棄 / B: patch 退避後破棄 | **B**（デフォルト採用。/tmp/dotfiles-watch-main-tests-stale.patch に退避済み） |
| 2 | A: テンプレート定数 `s-tanaka` 埋め込み / B: --global 参照 / C: env 明示必須化 | **A**（デフォルト採用） |
| 3 | A: .ps1 並列配置 / B: 単一 tmpl OS 分岐 / C: WSL2 前提維持 | **A**（ユーザー明示選択） |

## ユーザーの決定

#22 のみ明示回答（A）。他 2 件はデフォルト動作を承認（無回答＝デフォルト適用）。

## 理由・背景

- 論点 2: #36 マージ後は global email が noreply になり、`--global` 参照でも suffix が壊れる。bw アイテムはユーザー単位（s-tanaka）でありマシン・リポジトリに依存しないため定数が正しいスコープ
- 論点 3: Windows ネイティブ対応を行う方針。chezmoi 慣習（OS 別ファイル並列）に忠実な案を採用

## 今後 AI が取るべき行動

1. dotfiles の Windows 対応は #39（.chezmoiignore）→ #40（winget .ps1）→ #41（Task Scheduler / agy shim）→ #42（README）の順に実装する
2. bw アイテム名に関わる変更では suffix `s-tanaka` 定数（dot_bashrc.tmpl）を前提にする。git config からの導出を再導入しない
3. dotfiles で「同種の質問」（Windows 対応の要否）を再度エスカレーションしない
