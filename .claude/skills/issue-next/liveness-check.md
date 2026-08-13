# session-count gate 詳細（引数なしモード・#3626）

`/issue-next` 引数なしモードの同時実行セッション数上限判定（session-count gate・#3457）は、
`tidd issue-next-state init --enforce-session-limit <N>` が `init` 実行前に機械実行する（#3626）。
引数あり（単一番号・バッチモード）は `init <N>` を使うため gate は発動しない。

## exit code 対応

| exit code | 意味 | 対応 |
|-----------|------|------|
| 0 | TTL 内 state ファイル数が上限未満 → state 作成成功 | STEP 1.5 へ進む |
| 1 | TTL 内 state ファイル数が上限（`ISSUE_NEXT_MAX_SESSIONS`・デフォルト 2）以上 | state・ロック・ラベルを一切作らず停止してエスカレーション |

## exit 1 時のエスカレーション

stderr に「同時実行セッション数が上限」を含むメッセージと active 件数が出力される。

```
同時実行セッション数が上限に達しています。

A. いずれかのセッションの完了を待ってから /issue-next を再実行する — 最もスムーズに継続できる（推奨）
B. `tidd issue-next-state clear <N>` で停止した Issue の状態をリセットしてから /issue-next を再実行する — 作業中セッションが実際には死んでいると確信できる場合のみ

判断できなければ → A（`cache/issue-next-state/` 配下の state ファイルを確認して対象 Issue/PR のステータスを確認してください）
```

## TTL 設計

デフォルト TTL: 1800 秒（30 分）。`ISSUE_NEXT_LIVENESS_TTL_SECONDS` 環境変数でオーバーライド可。
TTL 超過・state 不在・JSON 破損はカウントに含めない（フェイルセーフ: 作業中でないと判定）。

同時実行セッション数の上限: デフォルト 2。`ISSUE_NEXT_MAX_SESSIONS` 環境変数でオーバーライド可
（非数値・0 以下はデフォルトへフォールバック）。`ISSUE_NEXT_MAX_SESSIONS=1` にすると #3457 以前と
同じ「1 セッションでもブロック」の直列動作に戻せる。

詳細: `docs/reference/issue-next-loop-operations.md#liveness-判定仕様`

## `check-liveness <N>`（per-issue liveness 判定・#2374）

`tidd issue-next-state check-liveness <N>` は TTL 付きで「Issue #N が作業中か」を判定する
（exit 1=作業中、exit 0=非作業中）。`subagent-delegation.md` の委譲時判定など、Issue 番号が
確定した後の個別判定に使う。引数なしの session-count gate は `init --enforce-session-limit` へ
統合済み（#3626）のため、`check-liveness` は Issue 番号を明示する per-issue 判定に専念する。

## liveness ファイルと `🔧 in-progress` ラベルの役割分担（Issue #2804）

本ファイルベースの liveness 機構と `🔧 in-progress` ラベル（`tidd check-in-progress-label` / `issue_progress_label` モジュール）は、検知範囲が異なる別レイヤーの排他機構であり、互いを代替しない:

| | liveness ファイル（session-count gate・#3457） | `🔧 in-progress` ラベル |
|---|---|---|
| 保存場所 | `cache/issue-next-state/issue-<N>.json`（ローカルディスク） | GitHub Issue のラベル（GitHub 上） |
| 可視範囲 | 同一マシン内のみ | マシンをまたいで可視（自宅・オフィス等の別セッションからも見える） |
| 判定対象 | 「同時実行セッション数が上限に達しているか」（引数なしモードの `init --enforce-session-limit` 専用の上限判定・#3457/#3626） | 「その Issue 番号に着手中か」（STEP 1 の候補選定・単一番号/バッチ着手時の個別判定） |
| 失効 | TTL（デフォルト 30 分）で自動失効 | 自動失効なし。`tidd issue-next-state clear <N>` 実行時にのみ除去（軽量実装・#2804） |

同一マシンで複数ターミナルを使う場合は liveness ファイルが機能するが、別マシンから並行して同じ Issue に着手しようとするケースは liveness ファイルでは検知できない。`🔧 in-progress` ラベルはこのマシン間の隙間を埋めるために GitHub 上の状態として着手中を可視化する。
