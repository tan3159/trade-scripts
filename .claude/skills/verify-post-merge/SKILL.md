---
name: verify-post-merge
description: マージ済み PR の [AI確認-post-merge] 項目を post-merge-verifier subagent で自律検証し、成功項目を - [x] に更新・24h 超過の失敗項目は Issue 起票して追跡注記付きで消化する。cron 定期実行（引数なし）またはユーザーが「/verify-post-merge <PR番号>」で特定 PR を検証するときに使う。
---

> **実行環境（ツール名の読み替え）:** 本スキルのツール名参照は Claude Code 前提で記載している。Codex（`.agents/skills` symlink 経由）で実行する場合は、`Agent(subagent_type="x", ...)` → `spawn_agent(agent_type="x", task_name="x", message=...)` に読み替える（`task_name` のみでは default ロールの agent が起動し `.claude/agents/*.md` 相当のツール制約・output_format 契約が適用されない・Issue #3491。対応表・実測記録: `docs/reference/codex-interop.md`「6-4. spawn_agent の `agent_type` 未指定時は default ロールが起動する」）。`Edit` / `Write` → `apply_patch` に読み替える。GitHub 操作は Claude Code・Codex いずれも `gh` CLI を使う（`mcp__github__*` は廃止済み・Issue #3773）。

# verify-post-merge

マージ済み PR に残っている `- [ ] [AI確認-post-merge] <条件>` 項目を Agent tool 経由で
自律検証し、`- [x]` に更新する。マージ後 24h 以内に GREEN 化が確認できなければ
`type: fix` + `source: post-merge-verify` ラベル付きの Issue を新規作成する (Issue #1402)。

**引数あり（`/verify-post-merge 42`）:** PR #42 の未消化 `[AI確認-post-merge]` を検証する。

**引数なし:** cron routine（`tidd schedule` または Claude Code `CronCreate`）から起動される想定。
マージ後 24h 以内の PR を全件走査して各 PR で本 SKILL を再帰的に実行する。
cron 設定手順は本ファイル末尾の「cron routine 設定」節を参照。

## 背景 / なぜ post-merge 検証が必要か

pre-merge の `[AI確認]` タグは PR マージ前に AI がファイル読み取りで検証する。
しかし現実には以下のような **post-merge でしか観測できない** 項目が頻出する:

- 「マージ後、次回 nightly-tests の X ステップが GREEN になること」
- 「マージ後、cron job が実行され Y が生成されること」
- 「デプロイ後、監視ダッシュボードで Z メトリクスが 0 になること」

これらを `[手動]` にすると人間マージ介在で auto-merge がブロックされる。
本 SKILL は `[AI確認-post-merge]` タグを設けることで、auto-merge を妨げず、
マージ後に AI が自律的に検証結果を回収する仕組みを提供する。

## 手順

### STEP 0: 所要時間サマリの sweep（引数なし・cron 起動時のみ・#3517）

人間マージ経路（exit 4・`needs-human-merge`）には所要時間サマリの投稿処理が組み込まれておらず、
auto-merge 経路と異なりサマリが 1 件も残らないケースがある。cron 起動時は STEP 1 の前に以下を実行し、
直近マージ PR でサマリ未投稿のものを補完する:

```bash
tidd merge-summary sweep --days 1
```

既に投稿済みの PR は `find_summary_comment_id` 判定でスキップされるため二重投稿は起きない。
個々の PR で失敗しても exit code は常に 0 で返るため、後続の STEP 1〜7 の実行を妨げない。

### STEP 1: 対象 PR の特定

**引数あり（`<PR番号>`）:** その PR のみを対象とする。

**引数なし（cron 起動）:** 対象 PR を CLI で機械列挙する:

```bash
tidd verify-post-merge --list-candidates
```

このコマンドは closed PR のうち、以下の 3 条件をすべて満たす PR 番号を stdout に 1 行 1 件で出力する
（#3638。`mergedAt` の日時計算・body の文字列包含判定は CLI 側で機械実行する）:

1. `mergedAt` が非 null（マージ済み。未マージ close は除外）
2. `mergedAt` が現在時刻から `--within-hours`（デフォルト 24）以内
3. body に未消化の `- [ ] [AI確認-post-merge]` 行が 1 件以上ある（全項目 `- [x]` 済みは除外）

候補 0 件なら何も出力せず exit 0 を返す。時間窓は `--within-hours <N>` で変更できる。
出力された各行（PR 番号）を対象 PR とする。

### STEP 2: 未消化 `[AI確認-post-merge]` 項目の抽出

```bash
tidd verify-post-merge <PR番号>
```

このコマンドは PR ボディから `- [ ] [AI確認-post-merge] <条件>` 行を抽出して stdout に
1 行 1 項目で出力する。stderr には人間向けの案内が出る。

- 未消化項目が 0 件の場合: exit 0 で終了。SKILL も終了する
- 未消化項目が 1 件以上の場合: STEP 3 へ進む

### STEP 3: post-merge-verifier subagent で検証

Agent tool で `post-merge-verifier` subagent を起動する:

```
Agent(  # Claude Code: Agent tool。Codex: spawn_agent(agent_type="post_merge_verifier", task_name="post_merge_verifier", message=...) に読み替え
  subagent_type="post-merge-verifier",
  description="PR #<N> の [AI確認-post-merge] 検証",
  prompt="""PR #<N> のマージ後に、以下の [AI確認-post-merge] 項目を検証してください。

## 検証対象項目
（STEP 2 で抽出した各項目を index 付きで列挙）
- index=0: マージ後に nightly-tests の pytest ステップが GREEN になること
- index=1: ...

## PR 情報
- PR 番号: <N>
- マージ時刻: <mergedAt>
- head ref: <headRefName>

CircleCI API / gh api / gh run コマンドを使って各項目を検証し、
JSON 形式で verified true/false と evidence を返してください。
"""
)
```

**プロンプトインジェクション対策:**
- subagent の `tools:` は `Bash, Read, Grep` に限定（`.claude/agents/post-merge-verifier.md`）
- Bash allowlist（`gh api` / `gh run` / `gh api repos/{owner}/{repo}/pulls/{pull_number}` / CircleCI API への `curl` GET のみ）を SKILL で明示
- 書き込み系（PR 本文更新 / Issue 作成）は subagent には呼ばせず、SKILL 側で JSON を parse してから `gh` コマンドで実行する

### STEP 4: verified=true の項目を `[x]` に更新

subagent の JSON 出力を parse し、`verified=true` の項目のチェックボックスを更新する:

```bash
# PR ボディ取得
gh pr view <PR番号> --json body
# → body フィールドを取得

# 各 verified=true 項目に対して mark_post_merge_item_done を呼ぶ
# （tidd verify-post-merge の Python API を short one-liner で呼ぶか python で置換する）
# python3 -c "
# from tidd_tools.verify_post_merge import mark_post_merge_item_done
# body = '''<body の内容>'''
# body = mark_post_merge_item_done(body, '[AI確認-post-merge] マージ後に nightly が GREEN')
# print(body)
# " > /tmp/pr-body-<PR番号>.md

# 更新された本文を PR に反映（一時ファイル経由・シェル未経由）
gh pr edit <PR番号> --body-file /tmp/pr-body-<PR番号>.md
```

### STEP 5: PR コメント投稿

検証結果を PR にコメントとして残す:

```bash
# 検証結果本文を一時ファイルへ書き込んでから --body-file で渡す（シェル未経由）
gh pr comment <PR番号> --body-file <一時ファイル>  # 本文: "## post-merge 検証結果 (Issue #1402)\n\n| 項目 | verified | evidence |\n|---|---|---|\n| マージ後に nightly-tests の pytest ステップが GREEN になること | ✅ | gh run list --workflow nightly-tests --limit 1 → conclusion=success |\n| ... | ❌ | ... |\n\n---\n*post-merge-verifier subagent が自動生成しました*"
```

### STEP 6: 失敗項目の Issue 起票

verified=false かつマージから 24h 以上経過している項目については、
`type: fix` + `source: post-merge-verify` ラベル付きの Issue を新規作成する:

```bash
# 本文を一時ファイルへ書き込んでから --body-file で渡す（シェル未経由）
gh issue create \
  --title "🤖 fix: PR #<N> の post-merge 検証が失敗 (<失敗した項目の要約>)" \
  --label "type: fix" --label "source: post-merge-verify" --label "priority: high" \
  --body-file <一時ファイル>  # 本文: "## 背景\n\nPR #<N> のマージ後 24h 経過時点で post-merge-verifier が以下の項目を verified=false と判定した。\nregression の可能性があるため調査が必要。\n\n## やること\n\n- [ ] 検証失敗の原因を特定する\n- [ ] 必要な修正を行う（別 PR）\n- [ ] 修正後、検証条件が満たされたことを確認する（本 Issue クローズ時）\n\n## 参照\n\n- 元 PR: #<N>\n- 失敗項目: <条件文>\n- evidence: <subagent が返した evidence>"
```

作成した Issue 番号を PR に補足コメントとして投稿する:

```bash
gh pr comment <PR番号> --body "post-merge 検証失敗により Issue #<新Issue番号> を起票しました。"
```

**Issue 起票後、該当行を `- [x]` + 追跡注記に更新して cron の再検出（同内容 Issue の量産）を防ぐ（Issue #1967）:**

```bash
# 最新の PR ボディを取得してから置換する（STEP 4 を経ない経路でもエラーにならないよう必ず取得する）
gh pr view <PR番号> --json body
# body フィールドを取得し、mark_post_merge_item_done で置換後に更新する:
# python3 -c "
# from tidd_tools.verify_post_merge import mark_post_merge_item_done
# body = '''<body の内容>'''
# body = mark_post_merge_item_done(body, '[AI確認-post-merge] <条件文>', tracking_issue=<新Issue番号>)
# print(body)
# " > /tmp/pr-body-<PR番号>.md
gh pr edit <PR番号> --body-file /tmp/pr-body-<PR番号>.md
```

更新後の該当行は `- [x] [AI確認-post-merge] <条件>（→ Issue #<新Issue番号> で追跡中）` になり、
次回 cron 走査では検出されない。以後の追跡は起票した Issue 側で行う。

### STEP 7: 出力

- 全項目 verified=true: 「PR #<N> の post-merge 検証が完了しました（N 件全て verified）」を stderr に出力
- 一部 verified=false（24h 未経過）: 「PR #<N> は N 件中 M 件が verified、残りは次回検証待ち」を stderr に出力
- 一部 verified=false（24h 経過・Issue 起票済）: 「PR #<N> の N 件中 M 件が failed。filed Issue #<X>（Issue #<X> を起票しました）」を stderr に出力（`filed Issue #<X>` は Issue #1967 受け入れ基準の機械可読マーカー。省略禁止）

## cron routine 設定

本 SKILL は cron 定期実行を前提とする。以下いずれかで起動する:

### 方式 A: `tidd schedule`（推奨・chezmoi 管理）

`tidd schedule` で登録した job から `/verify-post-merge`（引数なし）を発火する。
schedule 定義は chezmoi でマシン間配布される。

推奨頻度: **毎時実行**（マージ後 24h の window を細かく監視するため）:

```
# ~/.local/share/chezmoi/private_dot_config/tidd/schedules.toml
[[schedule]]
name = "verify-post-merge"
cron = "5 * * * *"  # 毎時 5 分
command = "claude -p '/verify-post-merge'"
```

### 方式 B: Claude Code `CronCreate`（sandbox 環境用）

Claude Code セッション内で以下を実行して routine を登録する:

```
CronCreate({
  cron: "5 * * * *",
  prompt: "/verify-post-merge"
})
```

### routine 起動時の挙動

- `/verify-post-merge`（引数なし）が起動される
- 本 SKILL の STEP 0 で `tidd merge-summary sweep --days 1` を実行し、人間マージ経路で
  投稿が漏れた所要時間サマリを補完する（#3517）
- 本 SKILL の STEP 1 で「マージ後 24h 以内 + 本文に `[AI確認-post-merge]` を含む PR」を列挙
- 対象 PR ごとに STEP 2〜7 を実行
- 全 PR 処理後に summary を stderr に出力して exit 0

### 実装ステータス

Issue #1402 の Phase 1（本 PR）では以下を提供する:

- ✅ `[AI確認-post-merge]` タグの test-plan 通過（auto-merge を妨げない）
- ✅ `tidd verify-post-merge <PR>` の CLI ラッパー
- ✅ `.claude/skills/verify-post-merge/SKILL.md`（本ファイル）
- ✅ `.claude/agents/post-merge-verifier.md`

cron routine 実登録（`tidd schedule` エントリ or `CronCreate`）は Phase 2 で別 Issue として扱う。
本 PR ではワークフローが手動起動（`/verify-post-merge <PR>`）で完結することを検証する。

## Anthropic SDK 直接呼び出し禁止

本 SKILL 実装内で `anthropic` SDK を直接呼ばない。全ての意味判定は Agent tool
(`post-merge-verifier` subagent) 経由で行う。`ban-anthropic-import` hook が違反を機械強制でブロックする。

## 関連

- `.claude/agents/post-merge-verifier.md` — subagent 定義
- `tidd_tools.verify_post_merge` モジュール — CLI エントリポイント
- `.claude/rules/workflow.md` — Test plan チェックリスト記述ルール（`[AI確認-post-merge]` タグ）
- `docs/reference/post-merge-verify-workflow.md` — 詳細ドキュメント
- `.claude/skills/issue-next/SKILL.md` — pre-merge `[AI確認]` 検証（対比参照）
- Issue #1402 — 本 SKILL の設計 Issue
