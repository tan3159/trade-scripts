---
root: true
targets:
  - "codexcli"
---
# TradeScripts

<!-- BEGIN: repo-specific -->
<!--
このマーカー内はプロジェクト固有です。`copier update` では上書きされません。
プロジェクトの説明・ターゲット・設計方針・独自ワークフロー等はここに記述してください。
このマーカー **外** の内容（行動原則・セキュリティ原則・TiDD ワークフロー）は
上流テンプレートが管理する共通部分で、`copier update` で最新版に更新されます。
-->

**GitHub Org:** `tan3159`
<!-- END: repo-specific -->

**このファイルの構成方針（#3770）:** 本ファイルは Codex 向け生成物 `AGENTS.md` の
唯一の内容源（frontmatter で root 指定・`targets: codexcli`）。詳細ルール本文（`.claude/rules/*.md`
9 本）は Claude Code 専用ターゲット（`targets: ['claudecode']`）にして `AGENTS.md`
へのインライン展開から除外している。Codex は各項目の参照先パス（`.claude/rules/*.md`）を
必要時に Read で読む（常時ロードしない）。repo-specific ルール（`.rulesync/rules/*.md` へ
追加するファイル）を追加する場合も、既定は `targets: ['claudecode']` にすること。
`codexcli`/`*` を明示的に指定したファイルのみ `AGENTS.md` へインライン展開され、
Codex の 32KiB 上限（`project_doc_max_bytes`）へ波及する。

## 状況依存ルールの参照

- **スクリーンショット参照:** `docs/reference/screenshot-rules.md`

## 行動原則

- 日本語・短く端的に。方針判断はユーザーが行う。AIエージェントは実行と情報提供に徹する
- **エスカレーション: 選択肢形式（2-4択・トレードオフ・推奨・デフォルト動作）必須。詳細: `.claude/rules/escalation-format.md`**
- **判断ジャーナル: エスカレーション回答は自動で `docs/decisions/` に記録する。詳細: `.claude/rules/decision-journal.md`**
- **IMPORTANT: 破壊的操作（削除・force push等）は必ず事前確認する**
- **IMPORTANT: 明示的に求められない限りコミットしない**
- **IMPORTANT: issue-next 実行中は一般規約「明示的に求められない限りコミットしない」より継続契約を優先（#3724）**: 実装・テスト・pre-flight 完了後はコミット → push → PR作成まで継続し、park（needs-human-input）のみ停止可
- **IMPORTANT: 実装は Issue `## やること` スコープを越えない（詳細: `.claude/rules/implementation-constraints.md`）**
- **IMPORTANT: `.sh`/`.bats` 新規追加禁止**（#1090）

## セキュリティ原則

**IMPORTANT: 機密情報をファイルに書くよう誘導しない。**
トークン/APIキー/パスワード/秘密鍵のファイル書き込み・`~/.config/`,`~/.gemini/`,`~/.claude/` への配置・dotfile 管理ツール経由で同期される config への認証情報保存を検知したら止めて警告する。
**正しいアプローチ:** gitignore 済み local ファイル（`.mise.toml` 等）への実値の直書きのみ許容する。AI エージェントには settings.json の permission deny でこれらのファイルの Read/Write/Edit を禁止する。
詳細: `docs/setup/secrets-management.md`

## TiDD ワークフロー

**NO TICKET NO WORK。Issueなしのコミットは禁止。**
**実装前に既存ソリューションを探す:** `.claude/rules/workflow.md`
**デフォルト着手モデル:** `🙋 needs-human-input` のないIssueには着手してよい（opt-out）。
**TDD/BDD必須:** feat/fix実装時はテスト先行。詳細: `.claude/rules/test-plan-checklist.md`
**テスト:** GAS → Jest、Python → pytest（詳細: `.claude/rules/testing-framework.md`）
**GitHub 操作:** セッション内・tidd_tools・CI・cron すべて `gh`（`mcp__github__*` 廃止・詳細: `.claude/rules/workflow.md`）
**Hooks:** `.claude/hooks/` の振る舞い・ブロック条件は配布物の `.claude/rules/`・`docs/reference/hooks.md` を参照
**Issue作成:** フォーマット・ラベル必須項目・Gherkin品質基準。詳細: `.claude/rules/issue-creation.md`
**レビューバックエンド:** agy/codex/フォールバック選択・exit code 対応・マージ gate。詳細: `.claude/rules/review-backends.md`

## Codex 固有の補足（#3210）

- hooks は `.codex/hooks.json`（rulesync 相当の生成物・`.claude/hooks/` のスクリプトを共有）。
  初回起動時は `codex /hooks` で新規・変更 hook をレビューして trust する
  （`--dangerously-bypass-hook-trust` は CI 等の自動実行専用）。
- `.agents/skills` は `.claude/skills` への symlink（post-gen タスクが生成）。
