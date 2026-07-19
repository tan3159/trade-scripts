# parser critical PR のレビューフロー（STEP 5 詳細）

`/issue-next` の STEP 5 詳細フロー。parser critical PR の場合のみ読む。

**parser critical PR の定義:** PR の変更ファイルに以下のいずれかが含まれる:
- `tidd_tools/ai_review/`（サブディレクトリ含む）
- `.claude/hooks/validate-issue.py`
- `.claude/hooks/require-issue.py`

**なぜ special なのか:** `tidd ai-review` は exit 0 = 自動マージ完了のため、merge 前に secondary consensus を実行できない。代わりに `/ai-review` SKILL を直接起動して STEP 5.5 で secondary consensus を確保する（#1290）。

## 判定手順

Claude Code セッション内で `mcp__github__list_pull_request_files({owner, repo, pull_number: <PR番号>})` を呼び、返り値の `filename` フィールドで上記パスが含まれるか確認する。

```bash
# shell 実行時のリファレンス
gh pr view <PR番号> --json files -q '[.files[].path]'
```

含まれない → parser critical PR ではない。`SKILL.md` 本体の「AIレビュー実行（非 parser critical PR）」節に従う。
含まれる → 以下の手順で実行する。

## 実行手順（`/ai-review` SKILL を直接起動）

**`tidd ai-review`（Python）を直接使わない。** `/ai-review` SKILL の各ステップを Claude が直接実行する:

1. **STEP 1-2**: `mcp__github__get_pull_request` / `mcp__github__get_pull_request_diff` で PR 情報・diff 取得
2. **STEP 3**: agy または codex でレビュー実行（primary）。全滅の場合は STEP 3.5 で exit 3 扱いとして STEP 5.5 が人間エスカレーションを発動
3. **STEP 3.5**: parser critical フラグは既に確定済み（`_is_parser_critical=true`）
4. **STEP 4**: レビュー本文を `/tmp/agent-review-<N>.md` に保存・backend 名（agy/codex）を記録
5. **STEP 5**: verdict-extractor subagent で verdict を構造化抽出
6. **STEP 5.5**: `_is_parser_critical=true` かつ STEP 5 の primary verdict が `APPROVE` の場合のみ実施する。**primary verdict が `REQUEST_CHANGES` の場合は STEP 5.5 をスキップし直接 STEP 6 へ進む**（secondary consensus は APPROVE 判定にのみ必要なため）。primary が APPROVE の場合、backend を確認してから secondary consensus を実施:
   - primary が agy/codex かつ APPROVE → ai-reviewer subagent を起動
     - **subagent 起動成功** かつ両方 APPROVE → `~/.cache/ai-dev-handbook/ai-reviewer/pr-<N>/consensus.json` に監査ログ書き込み → STEP 6 へ（PR コメント: `consensus: 2/2 APPROVE`）
     - **subagent 起動成功** かつ一方でも REQUEST_CHANGES → PR コメント + `needs-human-merge` ラベル → exit 2
     - **⚠️ subagent 起動不能（Agent tool 呼び出し失敗・claude-code バグ等）** → **Claude inline fallback は禁止**（同一モデル自作自演で consensus 崩壊）。PR コメント "secondary consensus 不能" + `needs-human-merge` ラベル → **exit 2 で人間エスカレーション**
   - primary が claude-code（全滅 fallback）→ 同一 backend 系統のため人間エスカレーション（exit 2）
7. **STEP 6**: verdict に応じて `--continue-with-verdict` を実行し、レビュー本文を PR に投稿する:
   - **primary verdict が `REQUEST_CHANGES`（STEP 5.5 未実施・#2079）** → `tidd ai-review --continue-with-verdict REQUEST_CHANGES <N>` を実行する。これにより 1 回目のレビュー本文（指摘事項）が PR コメントとして投稿され、監査証跡として残る。**修正に直接進んではならない**（レビュー内容が GitHub 上に残らず消える）。投稿後は issue-next（メイン SKILL.md）のリトライループに従って修正 → push → 再実行する
   - **consensus 通過（2/2 APPROVE）** → `tidd ai-review --continue-with-verdict APPROVE <N>` でマージ

**Issue #2074:** `--continue-with-verdict APPROVE` は `_continue_approve()` 内で `_post_test_statuses()`（`core.py` と共通）を呼び出し、マージ前に ruff-format/ruff-lint/mypy/pytest/jest の commit status を投稿する。parser critical PR は `tidd ai-review` の通常経路（`core.py::main()`）を経由しないが、lint/pytest status が未投稿のままマージされることはない。

## CRITICAL

- **Issue #1410:** parser critical PR で `ai-reviewer` subagent 起動に失敗したら、Claude 自身が inline で ai-reviewer 役を演じてはならない（`.claude/skills/ai-review/SKILL.md` STEP 5.5 の CRITICAL 節参照）。同一プロセス内 primary/secondary 兼任は独立判定にならず、`#1290` で確立した多 backend consensus 設計が崩れる
- リトライは issue-next（メイン SKILL.md）が管理し、REQUEST_CHANGES の場合は修正後に再実行する
