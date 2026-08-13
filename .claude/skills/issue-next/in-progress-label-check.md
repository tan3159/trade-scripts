# STEP 1: `🔧 in-progress` ラベルによる多重着手防止（Issue #2804）

`/issue-next` の STEP 1（単一番号指定・バッチモード）で `tidd issue-next-state init` を呼ぶ前に実行する事前チェック。
liveness ファイル（`liveness-check.md` 記載の生存確認）とは別レイヤーの多重着手防止であり、ラベルは liveness の生死判定に依存しない。

## バッチモード

anchor 番号（例: 42、キュー: 43 44）に対して:

```bash
tidd check-in-progress-label 42  # exit 0=ラベルなし（着手可能） / exit 1=ラベルあり
```

exit 1 の場合は `init` を実行せず、残りキュー番号（今回取り出した anchor 番号を含む）を明示したうえで下記の選択肢形式で報告して停止する:

```
Issue #42 には既に 🔧 in-progress ラベルが付与されています。他セッションが作業中の可能性があります。

A. 少し待ってから /issue-next 42 43 44 を再実行する — 他セッションの作業完了を待てる（推奨）
B. ラベルを手動で確認し、誤付与であれば剥がしてから再実行する — 他セッションが実際には動いていない場合

判断できなければ → A
```

exit 0 の場合のみ SKILL.md STEP 1 の `init` 手順（状態ファイル永続化）に進む。`init` 成功時に issue_progress_label 経由で `🔧 in-progress` ラベルが自動付与される。

## 単一番号指定

```bash
tidd check-in-progress-label <Issue番号>  # exit 0=ラベルなし（着手可能） / exit 1=ラベルあり
```

exit 1 の場合は `init` を実行せず、下記の選択肢形式で報告して停止する:

```
Issue #<Issue番号> には既に 🔧 in-progress ラベルが付与されています。他セッションが作業中の可能性があります。

A. 少し待ってから /issue-next <Issue番号> を再実行する — 他セッションの作業完了を待てる（推奨）
B. ラベルを手動で確認し、誤付与であれば剥がしてから再実行する — 他セッションが実際には動いていない場合

判断できなければ → A
```

exit 0 の場合のみ SKILL.md STEP 1 の `init` 手順（状態ファイル永続化）に進む。

## ラベルの付与・除去タイミング

ラベルは常に「いま作業中の 1 件」だけに付く。付与・除去は `issue_next_state` の各サブコマンドが
`issue_progress_label` 経由で自動的に行うため、AI が手動で `gh` を叩く必要はない。

| サブコマンド | ラベル操作 |
|---|---|
| `init <A> <B> <C>` | Issue A に付与する |
| `consume <A>`（キューあり） | 直前の `current_issue` から除去し、新しい `current_issue` へ付与する（#2817） |
| `consume <A>`（キュー空） | 直前の `current_issue` から除去するだけで、新たな付与はしない。exit 0・stdout 空でバッチ終端を示す |
| `clear <A>` | state ファイルの `current_issue`（anchor 番号ではない）から除去する |

**バッチモードでの移動タイミング（#2817）:** `consume` はキュー消費と同時にラベルを次の Issue へ移動させる。
STEP 6 のマージ後処理で `consume` を呼んだ時点で、完了した Issue のラベルは外れ、次に着手する Issue に付く。
`consume` を経ずに次の Issue へ進むとラベルが前の Issue に残り続けるため、バッチ内の Issue 遷移は必ず `consume` で行う。

キュー空の `consume` で解放済みの場合、後続の `clear` は除去 API を再呼び出ししない（存在しないラベルへの
DELETE で誤解を招く WARN が出るのを防ぐ）。

除去は成功可否をブロックしない軽量実装（TTL 自動失効・定期監査は対象外）のため、`consume`・`clear` 呼び出し漏れがあると
次回自動選定でその Issue が候補から除外され続ける点に注意する。

## `init` 自体の機械強制ロック（Issue #3452）

上記の `check-in-progress-label` は「事前チェック」であり、チェック通過後に別プロセスがほぼ同時に
同一 Issue へ着手すると（複数マシン同時実行時など）チェックと `init` の間にレースが生じ、両方が
`init` に成功してしまう可能性がある（TOCTOU）。この隙間を塞ぐため、`init` は内部で
`refs/locks/issue-<N>` という git ref への push を排他制御プリミティブとして使い、ロックを
獲得できたプロセスのみ状態ファイルを書き込む。これは AI エージェントへのプロンプト指示ではなく
`tidd_tools.issue_next_lock`（`issue_next_state.py` から呼び出し）によるコード側の機械強制であり、
`check-in-progress-label` を経由しても回避できない。

**`init` が exit 1 で失敗した場合（`check-in-progress-label` は exit 0 だったのに `init` 自体が
失敗するケース）は、ロックを他プロセスが保持中であることを意味する。** `needs-human-input` ラベルは
付与せず、既存の競合 PR スキップ（STEP 1.7・#2452）と同じ扱いにする:
- 引数なし・バッチモード: この Issue をスキップして次候補の選定へ進む（バッチは残キューへ、
  引数なしは STEP 1 の選定をやり直す）
- 単一番号指定: 「Issue #N は他プロセスが着手中の可能性があります（ロック未取得）」と報告して終了する

異常終了などでロックが解放されないまま残った場合（stale ロック）は `ISSUE_NEXT_LIVENESS_TTL_SECONDS`
（デフォルト 1800 秒）超過かつ `🔧 in-progress` ラベルなし・Open PR なしの条件で `init` 内部が
自動的に解放し再獲得を試みるため、AI エージェント側で手動解放する必要はない。
