# マージ完了時の所要時間サマリ出力（#2313・#2453 8フェーズ対応・#2734 セカンダリ行制御）

STEP 6 のマージ完了後・Issue やることチェックボックス更新の前に実行する。

## 目次

- [report の必須 step が欠落している場合](#report-の必須-step-が欠落している場合fail-open3517旧-exit-1-挙動は-2935-で導入3517-で変更)
- [sweep: 人間マージ経路の投稿漏れ補完](#sweep-人間マージ経路の投稿漏れ補完3517)
- [ゲート: report 未実行時の clear ブロック](#ゲート-report-未実行時の-clear-ブロック2391)

```bash
tidd merge-summary report issue-<N> --pr <PR番号>
```

デフォルト出力は 1〜2 行の quiet サマリ（Issue 番号・合計時間・異常フェーズ有無・表の参照先。Issue #3432）。
表全文（8 フェーズ × 6 列の Markdown 表 `## 所要時間サマリ` と「自動チェック結果」セクション）は `--full` を付けると従来どおり出力される
（Issue #2453）。セカンダリレビューの実行記録が存在する場合のみ AIレビュー(セカンダリ) 行が追加される（Issue #2734）。

| フェーズ | 主担当 | mark による計測 |
|---|---|---|
| Issue品質チェック | Claude Code | step1.5-quality-check |
| ブランチ作成 | git worktree・uv | step2-implementation ～ step2-branch-created |
| 実装 | Claude Code | step2-branch-created ～ 最初の step3-preflight-start（#3558・step3-edit-done 廃止） |
| 検証・テスト | pytest・mypy・ruff | per-issue JSONL（`issue-<N>.jsonl`）を権威ソースとして使用・mark はフォールバック（#2741） |
| プルリクエスト | git・gh | step3-preflight-end ～ step4-pr-created（`record-timing-boundaries.py` hook が自己記録。step4-pr-created 欠落時は GitHub API の PR 作成時刻へフォールバック・#3516） |
| AIレビュー(プライマリ) | (実際のLLM) | step5-airview-start ～ step5-airview-end（`tidd ai-review` の backend 呼び出し前後で自己記録・#3516） |
| AIレビュー(セカンダリ) | - | 実行記録がある場合のみ出力（通常PRでは出力なし） |
| CI・マージ | gh・GitHub API | step6-merge-start ～ step6-merged（`tidd ai-review` の auto-merge 経路がマージ実行前後で自己記録・#3516。欠落時は auto-merge span（pr-\<N>）→ PR リードタイムへフォールバック） |
| 後処理 | git・tidd・GitHub MCP | step6-merged ～ step6-cleanup-done |

**行順・日付表示（#3389・参考記載）:** 表の行は上表の宣言順ではなく、全フェーズ連結後に実測 `start_at` 昇順で再ソートされる
（計測不可の行は末尾）。開始・終了列の日付（`MM/DD`）は表全体が JST で 2 日以上にまたがる場合のみ全行に付記され、
単日に収まる場合は時刻のみ表示する（レンダリング実行日ではなく表自体の日付範囲で判定する）。

`--branch` 引数は後方互換のために残されているが、#2453 mark が揃っている場合は不要（実装フェーズは mark から自動算出される）。

メインセッションへの報告には、quiet 出力の 1〜2 行（Issue 番号・合計時間・異常フェーズ有無・表の参照先）をそのまま含める。
表全文（`## 所要時間サマリ`）は PR コメントへ投稿済みのため、チャット報告に再掲しない（#3432）。

バッチモード（複数 Issue 連続消化）の場合は、消化した Issue 番号のリストだけを控えておき、
キューが空になった最終報告の直前に以下を実行してロールアップ表も報告に含める
（合計時間は各 Issue の統一日誌から自動算出されるため、Issue ごとの合計を控えておく必要はない。Issue #3433）:

```bash
tidd merge-summary rollup --issues 42,43,44
```

記録が欠落して合計を算出できない Issue は「計測不可」として表に含まれ、stderr に警告 1 行が出る
（バッチ全体の報告は止めない・exit code は 0 のまま）。従来の JSON 引数形式
（`rollup '[["#N1","5分00秒"], ...]'`）も後方互換のため使用できる。

## report の必須 step が欠落している場合（fail-open・#3517。旧 exit 1 挙動は #2935 で導入・#3517 で変更）

統一イベントログ（`issue-<N>.jsonl`）が存在する Issue では、`tidd merge-summary report` は
最新 attempt に必須 step（`step6-merged`・`step6-cleanup-done`）が揃っているかを事前チェックする。
**#3517 以降は欠落があっても表の生成・GitHub 投稿を止めない（fail-open）。** 欠落 step は表の該当行が
「計測不可」として出力されるだけで、コマンドは exit code 0 で終了する。マージ済み PR に
所要時間サマリのコメントが 1 件も残らない事態（旧 exit 1 挙動が原因で発生していた）を防ぐための変更。

欠落が検出されると stderr に以下の警告が出力され、`loop_error_log` にも
`merge-summary:incomplete-marks`（欠落 step 名をカンマ区切りで含む）が 1 回記録される:

```
WARNING: merge-summary: Issue issue-<N> の記録が未完了です（欠落 step: step6-merged, step6-cleanup-done）。
該当フェーズは「計測不可」として表を生成します。
```

この警告が出た場合、必須ではないが、欠落 step の記録元ツールのログ（`step6-merged` は `tidd ai-review` の auto-merge 経路が自己記録・`step6-cleanup-done` は `tidd cleanup-merged-branch` が自己記録・#3556）を確認し、記録漏れの原因調査に活用できる。
過去 attempt（中断して再着手した Issue）のイベントは自動的に除外され、最新 attempt のみが
集計対象になるため、古い attempt の記録が残っていても影響しない。

## sweep: 人間マージ経路の投稿漏れ補完（#3517）

サマリ投稿は通常 `post_merge_summary_from_pr_body`（auto-merge 成功パス）でのみ実行され、
人間マージ経路（ai-review exit 4・`needs-human-merge`）には投稿処理が組み込まれていない。
`tidd merge-summary sweep [--days N]`（デフォルト 7 日）は直近マージ PR を列挙し、
`find_summary_comment_id(pr_num)` が None（＝サマリ未投稿）の PR のみ、本文の `closes #N` から
Issue 番号を解決して `report` 相当の投稿を事後実行する。

```bash
tidd merge-summary sweep --days 7
```

- 既存の marker（`<N>.txt` / `<N>.marker`）判定をそのまま使うため二重投稿は起きない
- 個々の PR で例外が出ても残りの PR の処理を継続し、コマンド全体は常に exit code 0 で終了する
- `verify-post-merge` skill の cron 定期実行フロー（STEP 0）から `--days 1` で自動起動される

## ゲート: report 未実行時の clear ブロック（#2391）

`tidd merge-summary report` を実行すると `cache/merge-summary-emitted/<Issue番号>.marker`
が書き込まれる。`tidd issue-next-state clear` は `current_issue` が設定されている場合に
この marker の存在を確認し、**未存在なら exit 1 でブロック**する。

```
Error: Issue #N の merge-summary report が未実行です。
       先に `tidd merge-summary report` を実行してください。
```

`clear` 成功時は marker を削除して次 Issue での誤検知を防ぐ。
`current_issue` が `None` の場合はゲートをスキップしてそのまま削除する。

**park 免除（#2881）:** STEP 1.5-d（Issue品質チェック不合格）・STEP 1.7 skip（競合検出）等
STEP 2（ブランチ・PR作成）未到達で park した場合、そもそも PR が存在せず
`tidd merge-summary report --pr` を実行する手段がない。この場合 `clear` は該当 Issue に
「closes #N」を含む PR が過去に一度でも作成されたか（open/closed/merged 問わず）を確認し、
1件も見つからなければゲートを免除して marker なしで state ファイルを削除する
（存在確認自体が失敗した場合は判定不能として安全側＝ブロック維持にフォールバックする）。
