# ワークフロー規約

> **注記:** 以下の機械強制（`require-issue` / `require-red-first` / `require-merge-ci-status` / `require-mypy` / `require-ruff-format` 等）は `hooks-config.json` で対応 hook を enable した利用者にのみ適用される。`copier copy` 直後の consumer はすべて default OFF のため、規約記述は参照目的。

## 実装前：既存ソリューションを探す

以下の順で確認し、見つかれば採用を優先する:
1. **Technology Radar** — `docs/research/technology-radar-guide.md` 参照
2. `docs/research/` — 調査済みリファレンス
3. `anthropics/claude-plugins-official` — 公式プラグイン
4. コミュニティリポジトリ（`wscffaa/claude-gh-skills` 等）
5. GitHub 検索（`path:.claude/skills` 等）

---

## GitHub 操作の使い分け

| 実行環境 | 優先ツール |
|---|---|
| Claude Code セッション内 | **`mcp__github__*`** |
| tidd_tools・CI・cron | `gh` subprocess |

詳細: [docs/research/github-mcp-server.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/research/github-mcp-server.md)

---

## TiDD ワークフロー

**NO TICKET NO WORK。Issue なしのコミットは禁止。**

### 着手前

1. `gh issue view <N>`
2. `git fetch origin`
3. `git worktree add -b <type>/issue-N-slug ../<repo>-issue-N-slug origin/main`（末尾 `origin/main` 必須）
4. `tidd` は `uv tool install` でグローバル導入済み（[docs/setup/copier-workflow-adoption.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/copier-workflow-adoption.md) §3）のため、worktree ごとの `.venv` 初期化は不要。`tidd --version` で疎通確認できる
5. worktree ディレクトリで作業

`git checkout -b` 等は hook がブロック（詳細: [docs/reference/hooks.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/hooks.md)）

### 実装中

- 調査・判断の経緯は Issue コメントに残す
- コミットに `closes #N` を含める
- 別問題を発見したら即 Issue 起票（非対話的に `gh issue create`・機密情報は含めない）
- **例外: 脆弱性を発見した場合は Issue 化せずユーザーに直接相談する**

### 完了時

1. PR 本文に `closes #N` 必須（`gh pr create`）
2. PR タイトル: `<type>(<scope>): #N 説明`（hook 強制）
3. **同期（前景）実行必須（#1232）** — `run_in_background` / `nohup` / `&` / `ScheduleWakeup` 禁止:
   `tidd ai-review <PR> 1`

| exit | 意味 | 対応 |
|---|---|---|
| 0 | APPROVE | 自動マージ（停止条件ファイルなければ） |
| 1 | REQUEST_CHANGES | 修正→push→試行回数++→再実行 |
| 2 | エスカレーション | 人間に委ねる |
| 3 | 全バックエンド利用不可 | [review-backends.md](./review-backends.md) 参照。**IMPORTANT: `ai-fallback-reviewer` の直接起動は禁止。`ai-fallback-reviewer` を直接 Agent tool で起動してはならない。必ず `tidd ai-review` が exit 3 を返した後にのみ起動する。** |
| 4 | 手動確認待ち | APPROVE 済みだが人間マージが必要 |

4. マージ後: `gh pr view <PR> --json state` が `MERGED` なら無確認で worktree remove・ブランチ削除・main sync を自動実行する（手順詳細: `.claude/skills/issue-next/SKILL.md` STEP 6・#2097）。`MERGED` 以外は AskUserQuestion で確認する

---

## その他の制約

- `.sh` / `.bats` 新規作成禁止（#1090）— hook がブロック（[docs/reference/hooks.md#ban-shell-filespy](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/hooks.md#ban-shell-filespy)）
- テスト: `testing-framework.md`・`test-plan-checklist.md`

---

## PR 分割

**1 Issue 1 PR。** 500 行超えそうなら分割検討・1000 行超は必ず分割（XXL は REQUEST_CHANGES gate）。

垂直分割 5 基準: 1) hook/rule/template/CI は別 PR 2) `.claude/` と `projects/py/` は別 PR 3) migration は追加/削除を別 PR 4) template 変更と consumer 検証を別 PR 5) 大規模リファクタは準備と本体を別 PR

分割不能 6 例外: API 変更+呼び出し側 / RED→GREEN セット / DB migration+依存コード / 依存追加+使用箇所 / hook+settings.json 登録 / 単純修正

詳細: [docs/reference/pr-splitting-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/pr-splitting-guide.md)

---

## セッション運用

1. **1 Issue 1 セッション** — マージ後は `/clear` してから次 Issue に着手
2. **ai-review 前に会話を軽く保つ** — TTL 5 分超過で全会話非キャッシュ再読となりコスト増
3. **subagent prompt は自己完結** — 往復を減らしてトークン節約
