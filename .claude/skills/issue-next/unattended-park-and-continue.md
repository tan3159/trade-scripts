# `--unattended` フラグ: park-and-continue 処理

`--unattended` **なし（デフォルト）の場合、以下の節は一切参照せず本 SKILL の通常フロー（各 STEP のエスカレーション節）どおりに停止する。挙動は変更しない。**

`--unattended` **あり**の場合、以下のエスカレーションが発生した時点で人間の応答を待って停止する代わりに *park-and-continue* を実行する:

- STEP 1.5-d: Issue 品質チェックが「修正不能・判断不能」と判定された場合
- STEP 5: `tidd ai-review` が終了コード 2（エスカレーション）を返した場合
- STEP 5: CI 待機ロジック（`gh pr checks --watch --fail-fast`）が失敗した場合

## park-and-continue 手順

1. **Issue にエスカレーション内容を選択肢形式でコメント投稿する**（`.claude/rules/escalation-format.md` 準拠。Issue **本文は編集しない**、コメントのみに残す）
2. Issue に `🙋 needs-human-input` ラベルを付与する
3. **PR が存在する場合**は `gh pr close <PR番号>` で取り下げる（ブランチ・レビューコメントは削除しない。Issue コメントに PR 番号をリンクする）。`gh pr close` が失敗した場合はコメントに「PR close 失敗（PR #<番号>）: 手動で close してください」を追記し、ラベル付与は維持したまま処理を継続する
4. **worktree が存在する場合**はクリーンアップする（メインリポジトリへ移動して `git worktree remove` → `git branch -D`）。STEP 1.5 時点のエスカレーション（STEP 2 未到達）では PR も worktree も存在しないため 3・4 はスキップする
5. 次の Issue へ継続する（park 上限は設けない。無制限に継続する）:
   - **引数なし:** STEP 0（並行 PR 数の上限チェック）から再実行する
   - **単一番号指定:** 継続すべき次の番号がないため、park 完了を報告して終了する
   - **バッチモード:** `tidd issue-next-state consume` で未処理キューの先頭を取り出し STEP 2 から継続する。キューが空なら「バッチ処理完了（park 分含む）」を報告して終了する

## 復帰手順（人間向け）

1. `🙋 needs-human-input` ラベルが付いた Issue のコメントを確認し、提示された選択肢（A/B/...）に回答する
2. PR を復元したい場合は `gh pr reopen <PR番号>` で再オープンする（ブランチ・レビューコメントは保持されている）
3. Issue から `🙋 needs-human-input` ラベルを外し、`/issue-next <Issue番号>`（単一番号指定）で再着手する
