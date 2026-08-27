# session-count gate 詳細（引数なしモード・#3626）

`/issue-next` 引数なしモードの同時実行セッション数上限判定（session-count gate・#3457）は、
`uv run --project projects/py/tidd_tools tidd issue-next-state init --enforce-session-limit <N>` が `init` 実行前に機械実行する（#3626）。
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
B. `uv run --project projects/py/tidd_tools tidd issue-next-state clear <N>` で停止した Issue の状態をリセットしてから /issue-next を再実行する — 作業中セッションが実際には死んでいると確信できる場合のみ

判断できなければ → A（`cache/issue-next-state/` 配下の state ファイルを確認して対象 Issue/PR のステータスを確認してください）
```

## TTL 設計

デフォルト TTL: 1800 秒（30 分）。`ISSUE_NEXT_LIVENESS_TTL_SECONDS` 環境変数でオーバーライド可。
TTL 超過・state 不在・JSON 破損はカウントに含めない（フェイルセーフ: 作業中でないと判定）。

同時実行セッション数の上限: デフォルト 2。`ISSUE_NEXT_MAX_SESSIONS` 環境変数でオーバーライド可
（非数値・0 以下はデフォルトへフォールバック）。`ISSUE_NEXT_MAX_SESSIONS=1` にすると #3457 以前と
同じ「1 セッションでもブロック」の直列動作に戻せる。

詳細: ai-dev-handbook 本体の docs/reference/ 配下・`issue-next-loop-operations.md`（consumer 未配布・`#liveness-判定仕様`節）

## `check-liveness <N>`（per-issue liveness 判定・#2374）

`uv run --project projects/py/tidd_tools tidd issue-next-state check-liveness <N>` は TTL 付きで「Issue #N が作業中か」を判定する
（exit 1=作業中、exit 0=非作業中）。`subagent-delegation.md` の委譲時判定など、Issue 番号が
確定した後の個別判定に使う。引数なしの session-count gate は `init --enforce-session-limit` へ
統合済み（#3626）のため、`check-liveness` は Issue 番号を明示する per-issue 判定に専念する。

### `--self-session <session_id>`（所有者識別・#4221）

`init` は state ファイル作成時に `🔧 in-progress` ラベルを付与し、`stamp-issue-next-session.py`
（PostToolUse hook）が state ファイルへ発行元セッションの `session_id` を後付けで記録する
（#3779）。専用 `issue-implementer` を起動できず**汎用 worker** に代替した場合、その worker が
単純に `check-liveness <N>` を呼ぶと、実は自分自身（親セッション）が `init` した state・ラベルを
「別セッションが着手中」と誤認し、処理を止めてしまう事故が起きうる。

`--self-session <session_id>` を付けると、state が active（TTL 内）のときに限り state の
`session_id` と照合し、以下のとおり所有者を区別した exit code を返す:

| exit code | 意味 | worker の対応 |
|-----------|------|---------------|
| 0 | state が非 active、または active かつ `session_id` が `--self-session` と一致（自セッション所有） | 処理を継続する |
| 1 | active かつ `session_id` が `--self-session` と不一致（別セッション所有） | 処理を停止する（stderr に「別セッションが作業中です」） |
| 2 | active だが `session_id` が欠落・非文字列・空文字列（所有者情報が不整合） | 処理を継続しない（stderr に「所有者情報が不整合です」） |

`--self-session` を付けない場合は従来どおり所有者を問わず active なら exit 1 を返す（後方互換）。

## liveness ファイルと `🔧 in-progress` ラベルの役割分担（Issue #2804）

本ファイルベースの liveness 機構と `🔧 in-progress` ラベル（`uv run --project projects/py/tidd_tools tidd check-in-progress-label` / `issue_progress_label` モジュール）は、検知範囲が異なる別レイヤーの排他機構であり、互いを代替しない:

| | liveness ファイル（session-count gate・#3457） | `🔧 in-progress` ラベル |
|---|---|---|
| 保存場所 | `cache/issue-next-state/issue-<N>.json`（ローカルディスク） | GitHub Issue のラベル（GitHub 上） |
| 可視範囲 | 同一マシン内のみ | マシンをまたいで可視（自宅・オフィス等の別セッションからも見える） |
| 判定対象 | 「同時実行セッション数が上限に達しているか」（引数なしモードの `init --enforce-session-limit` 専用の上限判定・#3457/#3626） | 「その Issue 番号に着手中か」（STEP 1 の候補選定・単一番号/バッチ着手時の個別判定） |
| 失効 | TTL（デフォルト 30 分）で自動失効 | 自動失効なし。`uv run --project projects/py/tidd_tools tidd issue-next-state clear <N>` 実行時にのみ除去（軽量実装・#2804） |

同一マシンで複数ターミナルを使う場合は liveness ファイルが機能するが、別マシンから並行して同じ Issue に着手しようとするケースは liveness ファイルでは検知できない。`🔧 in-progress` ラベルはこのマシン間の隙間を埋めるために GitHub 上の状態として着手中を可視化する。
