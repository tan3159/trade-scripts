# `--unattended` フラグ: park-and-continue 処理

> **実行環境（ツール名の読み替え）:** 本スキルのツール名参照は Claude Code 前提で記載している。Codex（`.agents/skills` symlink 経由）で実行する場合は、`Agent(subagent_type="x", ...)` → `spawn_agent(agent_type="x", task_name="x", message=...)` に読み替える（`task_name` のみでは default ロールの agent が起動し `.claude/agents/*.md` 相当のツール制約・output_format 契約が適用されない・Issue #3491。対応表・実測記録: `docs/reference/codex-interop.md`「6-4. spawn_agent の `agent_type` 未指定時は default ロールが起動する」）。`Edit` / `Write` → `apply_patch` に読み替え、`mcp__github__*` は Codex 側の GitHub MCP 設定が済んでいれば同ツール名のまま、未設定なら `gh` CLI で実行する。

`--unattended` **なし（デフォルト）の場合、以下の節は一切参照せず本 SKILL の通常フロー（各 STEP のエスカレーション節）どおりに停止する。挙動は変更しない。**

`--unattended` **あり**の場合、以下のエスカレーションが発生した時点で人間の応答を待って停止する代わりに *park-and-continue*（またはそれに準ずる自動継続）を実行する。**深夜放置運用（Issue #2802）を前提とするため、いずれの経路でも人間へのエスカレーションで停止することを一切許容しない:**

**モードの確認（#3633）:** unattended モードは `init --unattended` で `cache/issue-next-state/issue-<N>.json` に `"unattended": true` として永続化される（`consume` 後も保持）。**現在のモードを確認するときは記憶に頼らず `tidd issue-next-state is-unattended <N>` を実行し、exit 0 = unattended / exit 1 = attended で判定する。** unattended 中は `block-unattended-escalation.py` hook が `AskUserQuestion` を exit 2 でブロックするため、人間待機で停止する経路は機械的に塞がれている（escape hatch: `SKIP_UNATTENDED_ESCALATION_GATE=1`・`docs/reference/hooks.md` 参照）。

- STEP 1.5-d: Issue 品質チェックが「修正不能・判断不能」と判定された場合
- STEP 2・STEP 5: issue-implementer / issue-fixer subagent が park を報告した場合（下記「STEP2/STEP5: park 報告時の拡張」参照）
- STEP 5: `tidd ai-review` が終了コード 5（テスト status gate による中断・PR 起因テスト失敗・解決不能時）を返した場合（詳細: `existing-test-failure.md`「PR/pre-flight 起因時のエスカレーション」起点 A の `--unattended` 手順）
- STEP 5: `tidd ai-review` が終了コード 2（エスカレーション）を返した場合
- STEP 5: 終了コード 4（`[手動]` 項目のみ残存）の場合（下記「exit 4: 手動確認待ちの `--unattended` 対応」）
- STEP 5: CI 待機ロジック（`tidd wait-ci`・Issue #3645）が exit 1（CI 失敗）または exit 2（タイムアウト）を返した場合
- STEP 6: `mcp__github__get_pull_request` で PR の state が `MERGED` と確認できない場合（#2802 以降は park-and-continue 対象。下記「STEP 6: MERGED 未確認時の park-and-continue」）

## exit 4: 手動確認待ちの `--unattended` 対応

`[AI確認]` 項目がなく `[手動]` 項目のみが残っている場合、`[手動]` 項目は既存 CRITICAL 制約（Issue #774）により自動で `- [x]` にできないため自動修正は行わない。A/B の選択肢報告の代わりに下記「park-and-continue 手順」を実行する:

1. Issue に「PR #<PR番号> は AIレビューで APPROVE 済みですが `[手動]` 確認待ち項目が残っているため park します（Test plan の未完了項目: <一覧>）」を選択肢形式でコメント投稿する
2. 「park-and-continue 手順」の 2〜5 をそのまま実行する（PR close・worktree クリーンアップ・次 Issue へ継続を含む）

## STEP 6: MERGED 未確認時の park-and-continue（#2802 で変更・旧仕様は #2428）

`mcp__github__get_pull_request({owner, repo, pull_number: <PR番号>})` の state が `MERGED` でない場合（未マージ・conflict・API エラー等）:

1. **worktree・branch はそのまま残す**（削除しない）
2. **PR は close しない**（`gh pr merge` を実行済みのため、マージ試行済みの PR を close すると状態を悪化させうる）
3. Issue に以下の文言をコメント投稿する（`.claude/rules/escalation-format.md` 準拠）:
   > PR #<PR番号> の state が MERGED と確認できなかったため、worktree・branch を削除せず手動確認に委ねます（PR は close していません）。
   > worktree: `<worktree パス>` / branch: `<branch 名>`
   >
   > A. PR の状態を再確認してから worktree・branch を削除する — `mcp__github__get_pull_request` で state を確認し MERGED なら `tidd cleanup-merged-branch <branch>` を実行（推奨）
   > B. worktree・branch をそのまま残して後で確認する — Issue から `🙋 needs-human-input` ラベルを外してから `/issue-next <Issue番号>` で再着手する
   >
   > 判断できなければ → A（PR URL を確認してください）
4. Issue に `🙋 needs-human-input` ラベルを付与する
5. **`needs-human-merge` ラベルは PR に付与しない**（`resume-needs-human-merge.md` の自動マージ判断フローに入り MERGED 未確認 PR の自動マージが試行されるリスクがあるため）
6. **次の Issue へ継続する**（park-and-continue 手順の 5 と同じ分岐。PR close は行わないため手順 3 相当は実施済みとして扱う）:
   - **引数なし:** STEP 1 の引数なしモード事前チェック（並行 PR 数の上限チェック）から再実行する
   - **単一番号指定:** 継続すべき次の番号がないため、park 完了を報告して終了する
   - **バッチモード:** `tidd issue-next-state consume` で未処理キューの先頭を取り出し STEP 2 から継続する。キューが空なら「バッチ処理完了（park 分含む）」を報告して終了する

**対話セッション（`--unattended` なし）との違い:** 対話時は A/B 選択肢形式でユーザーに直接確認し、回答を得てから動作する。headless 環境では AskUserQuestion tool が存在しないため、この委譲フローを使う。

## STEP2/STEP5: issue-implementer / issue-fixer の park 報告時の拡張（ブロッキングバグ自動起票）

`subagent-delegation.md`「park 時の処理」で issue-implementer / issue-fixer subagent からの park 報告（機械検証不一致を含む）を受けたとき、`--unattended` 時は以下の順で判定する:

1. **park 理由が対象 Issue のスコープ外である別バグ（ブロッカー）だと判断できる場合:**
   1. そのバグ用の Issue を自動起票する（`mcp__github__create_issue`・タイトル `🤖 fix: <バグ概要> を修正する`・ラベル `type: fix`・`priority: critical`・`source: rework`・`issue-creation.md` 準拠の `## 背景`・`## やること`・`## 振る舞い`）
   2. 元の Issue の処理を中断せず、新規バグ Issue を現在のキューの最優先として先に処理する（バッチモードなら未処理キューの先頭に挿入、単一番号指定・引数なしなら新規バグ Issue を先に STEP 1 から処理してから元の Issue に戻る）
   3. バグ Issue の処理完了（マージ・park いずれか）後、元の Issue の処理を再開する
2. **上記に当てはまらない、または自動判断もできない場合（デフォルト行動）:** 下記「park-and-continue 手順」をそのまま実行する（PR が存在すれば close する。**Issue 自体は close しない**）。これが `--unattended` におけるエスカレーションの唯一の着地点である

## `--unattended` 下の設計判断の優先順位（Issue #2802）

実装中に「教科書的に見て複数の実装方針が考えられ人間の好みが分からない」等、停止せず自動で決めなければならない設計判断が発生した場合、以下の優先順位で自動的に決定し停止しない:

1. 教科書的・業界標準の選択肢
2. モダンな選択肢
3. ワークフローが高速に回る選択肢

## park-and-continue 手順

1. **Issue にエスカレーション内容を選択肢形式でコメント投稿する**（`.claude/rules/escalation-format.md` 準拠。Issue **本文は編集しない**、コメントのみに残す）
2. Issue に `🙋 needs-human-input` ラベルを付与する
3. **PR が存在する場合**は `mcp__github__update_pull_request({owner, repo, pull_number: <PR番号>, state: "closed"})` で取り下げる（ブランチ・レビューコメントは削除しない。Issue コメントに PR 番号をリンクする）。MCP 呼び出しが失敗した場合はコメントに「PR close 失敗（PR #<番号>）: 手動で close してください」を追記し、ラベル付与は維持したまま処理を継続する
4. **worktree が存在する場合**はクリーンアップする（メインリポジトリへ移動して `git worktree remove` → `git branch -D`）。STEP 1.5 時点のエスカレーション（STEP 2 未到達）では PR も worktree も存在しないため 3・4 はスキップする
5. 次の Issue へ継続する（park 上限は設けない。無制限に継続する）:
   - **引数なし:** STEP 1 の引数なしモード事前チェック（並行 PR 数の上限チェック）から再実行する
   - **単一番号指定:** 継続すべき次の番号がないため、park 完了を報告して終了する
   - **バッチモード:** `tidd issue-next-state consume` で未処理キューの先頭を取り出し STEP 2 から継続する。キューが空なら「バッチ処理完了（park 分含む）」を報告して終了する

## 復帰手順（人間向け）

1. `🙋 needs-human-input` ラベルが付いた Issue のコメントを確認し、提示された選択肢（A/B/...）に回答する
2. PR を復元したい場合は `gh pr reopen <PR番号>` で再オープンする（ブランチ・レビューコメントは保持されている）
3. Issue から `🙋 needs-human-input` ラベルを外し、`/issue-next <Issue番号>`（単一番号指定）で再着手する
