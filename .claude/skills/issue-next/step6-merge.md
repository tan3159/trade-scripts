# STEP 6: 自動マージ（`/issue-next` 詳細フロー）

> **実行環境（ツール名の読み替え）:** 本スキルのツール名参照は Claude Code 前提で記載している。Codex（`.agents/skills` symlink 経由）で実行する場合は、`Agent(subagent_type="x", ...)` → `spawn_agent(agent_type="x", task_name="x", message=...)` に読み替える（`task_name` のみでは default ロールの agent が起動し `.claude/agents/*.md` 相当のツール制約・output_format 契約が適用されない・Issue #3491。対応表・実測記録: `docs/reference/codex-interop.md`「6-4. spawn_agent の `agent_type` 未指定時は default ロールが起動する」）。`Edit` / `Write` → `apply_patch` に読み替え、`mcp__github__*` は Codex 側の GitHub MCP 設定が済んでいれば同ツール名のまま、未設定なら `gh` CLI で実行する。

`/issue-next` の STEP 6 詳細フロー。AIレビュー APPROVE・CI 通過後に読む。

```bash
gh pr merge <PR番号> --squash --delete-branch
git fetch origin
```

**`step6-merge-start`/`step6-merged` は手動 mark 不要（#3516）:** `tidd ai-review` の auto-merge 経路がマージ実行前後に統一日誌へ自己記録するため、手動 mark コマンドは廃止した。

**マージ後に worktree をクリーンアップする。`mcp__github__get_pull_request({owner, repo, pull_number: <PR番号>})` で state フィールドが `MERGED` と機械確認できた場合のみ無確認で自動実行する（#2097）。worktree 内からは削除できないためメインリポジトリへ移動してから実行する。`MERGED` 以外（未マージ・conflict 等）の場合は以下のとおり分岐する:**
**`is-unattended <N>` が exit 0 のとき（#2802 以降・#3633）:** AskUserQuestion を呼ばず `unattended-park-and-continue.md`「STEP 6: MERGED 未確認時の park-and-continue」を実行する（worktree・branch は削除せず残す。PR は close せず、次 Issue へ継続する）。
**対話セッション（`--unattended` なし）時:** A/B の選択肢形式（`.claude/rules/escalation-format.md` 準拠）でユーザーに削除可否を確認し、A（削除する）と回答されるまで worktree・branch を削除しない。**

```
mcp__github__get_pull_request({owner, repo, pull_number: <PR番号>})  # state が "MERGED" であることを確認
```
```bash
cd /path/to/<repo>
tidd cleanup-merged-branch <branch>  # worktree remove + branch -D を安全に実行（#2370）
git pull origin main --ff-only
```

**CRITICAL（#2670）: `git pull origin main --ff-only` の終了コードを必ず確認する。** 出力を `tail` 等に通して流し見しない。exit 0 なら次へ進む。exit 0 以外の場合（未追跡ファイル衝突・コンフリクト・ネットワーク断等）は `.claude/skills/issue-next/main-sync-recovery.md` を読んで対処する（black-hole させて次 Issue へ進んではならない）。

**`step6-cleanup-done` は手動 mark 不要（#3556）:** 直前の `tidd cleanup-merged-branch` が worktree・branch 削除完了時に統一日誌へ自己記録する（PR 本文の `closes #N` から Issue を解決し、解決できない場合は記録しない・exit code は変えない）。手打ち mark は禁止。

**`tidd cleanup-merged-branch` の動作:** PR が MERGED でローカル HEAD が PR の headRefOid と一致する場合のみ worktree 削除とブランチ削除を実行する（exit 0）。安全条件を満たさない場合は stderr にエラーを出して exit 1（ブランチは削除されない）。`Bash(git branch -D*)` の deny は引き続き有効であるため、raw な `git branch -D` は使わないこと。

**所要時間サマリを出力する（#2313）:** `.claude/skills/issue-next/merge-summary-output.md` を読んで実行する。**ただし、`tidd ai-review` の auto-merge で PR がマージされた場合、サマリは ai-review プロセス内で自動投稿される（#2790）。** この場合に `tidd merge-summary report` を再実行しても、投稿前に PR のコメントを確認して既存のサマリを検出するため二重投稿されない（スキップされる）。ai-review 以外の手段でマージされた場合は引き続き手動で実行する。**CodeRabbit マージ後スクリーニング（use_coderabbit=true consumer限定・Issue #2340）:** 直前に実行した `tidd cleanup-merged-branch` の出力（stderr）に `coderabbit-screening: required` が含まれる場合のみ `.claude/skills/issue-next/coderabbit-postmerge-screening.md` を読んで実行する。含まれない場合はスキップする（#3637）。

**マージ完了後に Issue の `## やること` チェックボックスを更新する（CRITICAL: 必ず実行する）。**

チェックボックス判断の保守的基準（Issue やること全消化 gate 判定時と同じ）:

1. `mcp__github__list_pull_request_files({owner, repo, pull_number: <PR番号>})` で PR の実際の変更ファイル一覧を取得する
2. `mcp__github__get_issue({owner, repo, issue_number: N})` で Issue 本文を取得する
3. 変更ファイルとの対応が確認できた `## やること` 項目のみ `[x]` に変更する
4. `mcp__github__update_issue({owner, repo, issue_number: N, body: <更新後本文>})` で反映する

マージ完了後:

- 引数なし → `tidd issue-next-state clear <N>` で状態ファイルを削除してから「Issue #N 完了」をメインセッションに報告して終了する（**#2663: 引数なしモードでも clear が必要**）。次 Issue への継続は `/loop <固定間隔> /issue-next`（CronCreate バックエンド）の外部発火に委ねる（詳細: `docs/reference/issue-next-loop-operations.md`）
- 単一番号指定 → `tidd issue-next-state clear N` で状態ファイルを削除してから「Issue #N 完了」をメインセッションに報告して終了する（**#2474: `N` を明示**）
- バッチモード（未処理キューあり）→ `issue-next-state consume <anchor番号>` で次を取り出し STEP 2 へ進む（**#2474: バッチ anchor 番号を明示**）
- バッチモード（キュー空）→ 「バッチ処理完了（#N1, #N2, ... 全件完了）」を報告して終了する
