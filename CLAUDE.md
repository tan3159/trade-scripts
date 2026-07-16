# TradeScripts

<!-- BEGIN: repo-specific -->
<!--
このマーカー内はプロジェクト固有です。`copier update` では上書きされません。
プロジェクトの説明・ターゲット・設計方針・独自ワークフロー等はここに記述してください。
このマーカー **外** の内容（行動原則・セキュリティ原則・TiDD ワークフロー）は
ai-dev-handbook が管理する共通部分で、`copier update` で最新版に更新されます。
-->

**GitHub Org:** `tan3159`
<!-- END: repo-specific -->

## 行動原則

- 日本語・短く端的に。方針判断はユーザーが行う。Claudeは実行と情報提供に徹する
- **エスカレーション: 選択肢形式（2-4択・トレードオフ・推奨・デフォルト動作）必須。詳細: `.claude/rules/escalation-format.md`**
- **IMPORTANT: 破壊的操作（削除・force push等）は必ず事前確認する**
- **IMPORTANT: 明示的に求められない限りコミットしない**
- **IMPORTANT: 実装は Issue `## やること` スコープを越えない（詳細: `.claude/rules/implementation-constraints.md`）**
- **IMPORTANT: `.sh`/`.bats` 新規追加禁止**（#1090）

## セキュリティ原則

**IMPORTANT: 機密情報をファイルに書くよう誘導しない。**
トークン/APIキー/パスワード/秘密鍵のファイル書き込み・`~/.config/`,`~/.gemini/`,`~/.claude/` への配置・config/.env への認証情報保存を検知したら止めて警告する。
**正しいアプローチ:** `bw`（Bitwarden CLI）で管理し `~/.bashrc` には `$(bw get password "...")` 参照のみ。
**例外:** gitignore 済み local ファイル（`.envrc`・`.mise.toml` 等）への実値書き込みは許容。
詳細: `docs/setup/secrets-management.md`

## TiDD ワークフロー

**NO TICKET NO WORK。Issueなしのコミットは禁止。**
**実装前に既存ソリューションを探す:** `.claude/rules/workflow.md`
**デフォルト着手モデル:** `🙋 needs-human-input` のないIssueには着手してよい（opt-out）。
**TDD/BDD必須:** feat/fix実装時はテスト先行。詳細: `.claude/rules/test-plan-checklist.md`
**テスト:** GAS → Jest、Python → pytest（詳細: `.claude/rules/testing-framework.md`）
**GitHub 操作:** セッション内は `mcp__github__*` 優先。tidd_tools/CI/cron は `gh`（詳細: `.claude/rules/workflow.md`）
**Hooks:** `.claude/hooks/` の振る舞い・ブロック条件: `docs/reference/hooks.md`
