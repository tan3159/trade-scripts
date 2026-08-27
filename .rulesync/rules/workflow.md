---
root: false
targets:
  - 'claudecode'
---
# ワークフロー規約

> **注記:** 機械強制 hook は `config.json` で enable した利用者のみ適用（`copier copy` 直後は default OFF）。

**詳細（exit code 対応・環境初期化・関連 Issue）:** ai-dev-handbook 本体の docs/reference/ 配下・`workflow-guide.md`（consumer 未配布）

## 上流 hook の有効化

`.tidd/config.json`（`--repo`）または `~/.config/tidd_tools/config.json`（`--machine`）で
enable する。**`tidd config init --all-on --repo|--machine` で全 hook を一括有効化**
（安全系のみなら `--safety-only`）。個別 hook 名・現在の実効値は `tidd config show` で確認できる。
詳細: ai-dev-handbook 本体の docs/reference/ 配下・`hooks.md`（consumer 未配布）

## 実装前：既存ソリューションを探す

1. Technology Radar 2. `docs/research/` 3. claude-plugins-official 4. コミュニティ 5. GitHub 検索（詳細: `docs/research/technology-radar-guide.md`）

**ライブラリ採用時:** `.claude/rules/dependency-allowlist.yaml` へ追加する PR を同梱する（未登録は hook がブロック・#2561）。

---

## GitHub 操作の使い分け

Claude Code セッション内・tidd_tools・CI・cron すべて `gh`（`mcp__github__*` 廃止）。

### consumer が持つ 2 種類の PAT

consumer（copier 配布先）は操作対象で **2 種類の PAT** を使い分ける（既定トークンは上流に届かない）:
- consumer 自身のリポジトリ → consumer 自身の PAT（既定トークン）
- 上流リポジトリ → 上流用 PAT

上流用 PAT は値を読まずに使う: クローン先で `mise exec -- gh ...`。変数名は `mise env --json | jq 'keys'` で **キーのみ**。

---

## TiDD ワークフロー

**NO TICKET NO WORK。Issue なしのコミットは禁止。**

### 着手前

1. `gh issue view <N>` → `git fetch origin`
2. `git worktree add -b <type>/issue-N-slug ../<repo>-issue-N-slug origin/main`（末尾 `origin/main` 必須）
3. **`.venv` 初期化は `SessionStart` hook が自動実行する**（#2596・無効時の手動コマンドは guide 参照）
4. worktree ディレクトリで作業（`git checkout -b` 等は hook がブロック）

### 実装中

- 調査・判断の経緯は Issue コメントに残す。コミットに `closes #N` を含める
- 別問題を発見したら即 Issue 起票（非対話的に `gh issue create`・機密情報は含めない）
- **エラーに遭遇したら無視せず原因を調査する** — 一時的か恒常的かを判断する。恒常的なら `gh issue list` で重複確認の上、再発防止 Issue を起票する
- **脆弱性を発見したら Issue 化せずユーザーに直接相談する**
- **`## 期待する出力例` がある場合**: 内容を改変せず初回スナップショット/テスト期待値として固定する（未記載時は従来フロー）
- **`impl-delegation`/`impl-backend` 設定時**: `tidd propose-step` で各ステップのコード提案を外部 backend へ委譲可（無効時は自分で実装）。詳細: ai-dev-handbook 本体の docs/reference/ 配下・`propose-step-guide.md`（consumer 未配布）

### 完了時

1. `gh pr create` 前に `tidd pre-flight` を実行し GREEN（exit 0）を確認する
   - src/hooks 変更時は docs 更新必須（不要なら `<!-- no-doc-update: <理由> -->`）
2. PR 本文に `closes #N` 必須。タイトル: `<type>(<scope>): #N 説明`（hook 強制）
3. **同期（前景）実行必須** — `run_in_background`/`nohup`/`&`/`ScheduleWakeup` 禁止:
   `tidd ai-review <PR> 1`
4. exit code 対応・マージ後クリーンアップ・verify-post-merge 登録: ai-dev-handbook 本体の docs/reference/ 配下・`workflow-guide.md`（consumer 未配布）参照

---

## その他の制約 / PR 分割

- `.sh` / `.bats` 新規作成禁止（#1090）— hook がブロック
- テスト: `testing-framework.md`・`test-plan-checklist.md`
- **1 Issue 1 PR。** 1000 行超は分割必須（`tidd pre-flight` 機械強制・#3081）。詳細: ai-dev-handbook 本体の docs/reference/ 配下・`pr-splitting-guide.md`（consumer 未配布）
  - **例外（#4169）:** `docs/decisions/**` のみ直接 push 可。詳細: `.claude/rules/decision-journal.md`

---

## セッション運用

- **1 Issue 1 セッション** — マージ後は `/clear` して次 Issue へ
- **ai-review 前に会話を軽く保つ**（TTL 5 分超過で全会話非キャッシュ再読）
- **subagent prompt は自己完結**
- **`issue-next` 連続自走は `/loop` へ**（詳細: ai-dev-handbook 本体の docs/reference/ 配下・`issue-next-loop-operations.md`（consumer 未配布））
