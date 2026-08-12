---
root: false
targets:
  - '*'
---
# ワークフロー規約

> **注記:** 機械強制 hook は `config.json` で enable した利用者のみ適用（`copier copy` 直後は default OFF）。

**詳細（exit code 対応・環境初期化・関連 Issue）:** `docs/reference/workflow-guide.md`

## 実装前：既存ソリューションを探す

1. Technology Radar 2. `docs/research/` 3. `anthropics/claude-plugins-official` 4. コミュニティリポジトリ 5. GitHub 検索（詳細: `docs/research/technology-radar-guide.md`）

**ライブラリ採用時:** 新規パッケージ採用時は `.claude/rules/dependency-allowlist.yaml` へ追加する PR を同梱する（`check-dependency-allowlist.py` hook が未登録をブロック・#2561）。

---

## GitHub 操作の使い分け

Claude Code セッション内: **`mcp__github__*`** / tidd_tools・CI・cron: `gh`（詳細: `docs/research/github-mcp-server.md`）

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
- **エラーに遭遇したら無視せず原因を調査する** — 一時的か恒常的かを判断し、「散発するから仕方ない」で片付けない。恒常的なエラーは `gh issue list` で重複確認の上、再発防止 Issue を起票する
- **脆弱性を発見したら Issue 化せずユーザーに直接相談する**
- **`## 期待する出力例` がある場合**: 内容を改変せず初回スナップショット/テスト期待値として固定する（未記載時は従来フロー）
- **config.json に `impl-delegation: true` かつ `impl-backend` が設定されている場合**: RED/GREEN 各ステップ + refactor/docs 編集ステップで `tidd propose-step` を使いコード提案を外部 backend（agy/codex/custom）へ委譲できる（`impl-delegation` 無効時またはいずれか未設定時は従来どおり自分で実装。詳細: `docs/reference/propose-step-guide.md`・#3118・#3131・#3132）

### 完了時

1. `gh pr create` 前に `tidd pre-flight` を実行し GREEN（exit 0）を確認する
   - src/hooks 変更時は docs 更新必須（不要なら `<!-- no-doc-update: <理由> -->`）
2. PR 本文に `closes #N` 必須。タイトル: `<type>(<scope>): #N 説明`（hook 強制）
3. **同期（前景）実行必須** — `run_in_background`/`nohup`/`&`/`ScheduleWakeup` 禁止:
   `tidd ai-review <PR> 1`
4. exit code ごとの対応・マージ後クリーンアップ（#3398: `sweep-merged-branches` hook が自動実行）・verify-post-merge 登録: `docs/reference/workflow-guide.md` 参照

---

## その他の制約 / PR 分割

- `.sh` / `.bats` 新規作成禁止（#1090）— hook がブロック
- テスト: `testing-framework.md`・`test-plan-checklist.md`
- **1 Issue 1 PR。** 500 行超: 分割検討。1000 行超: 必ず分割（`tidd pre-flight` が機械強制・#3081）。詳細: `docs/reference/pr-splitting-guide.md`

---

## セッション運用

- **1 Issue 1 セッション** — マージ後は `/clear` して次 Issue へ
- **ai-review 前に会話を軽く保つ**（TTL 5 分超過で全会話非キャッシュ再読）
- **subagent prompt は自己完結**
- **`issue-next` 連続自走は `/loop` へ**（詳細: `docs/reference/issue-next-loop-operations.md`）
