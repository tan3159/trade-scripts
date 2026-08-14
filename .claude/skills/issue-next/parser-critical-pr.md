# parser critical PR のレビューフロー（STEP 5 詳細）

> **実行環境（ツール名の読み替え）:** 本スキルのツール名参照は Claude Code 前提で記載している。Codex（`.agents/skills` symlink 経由）で実行する場合は、`Agent(subagent_type="x", ...)` → `spawn_agent(agent_type="x", task_name="x", message=...)` に読み替える（`task_name` のみでは default ロールの agent が起動し `.claude/agents/*.md` 相当のツール制約・output_format 契約が適用されない・Issue #3491。対応表・実測記録: `docs/reference/codex-interop.md`「6-4. spawn_agent の `agent_type` 未指定時は default ロールが起動する」）。`Edit` / `Write` → `apply_patch` に読み替える。GitHub 操作は Claude Code・Codex いずれも `gh` CLI を使う（`mcp__github__*` は廃止済み・Issue #3773）。

**旧フロー（`/ai-review` SKILL を直接起動して手書きプロンプトを組み立てる）は廃止**（Issue #2645）。必ず `tidd ai-review --stop-before-merge` を使うこと。

## 判定手順

`tidd ai-review <PR番号> <試行回数>` を通常どおり実行する。exit 6 が返ったら parser critical PR なので、`--stop-before-merge` を付けて再実行する（Issue #3630）。

`tidd ai-review` 本体が起動時に PR の変更ファイル一覧を取得して parser critical 判定を行い、該当する場合は `--stop-before-merge` 無しの通常経路で exit 6 を返す。`~/.cache/tidd/ai-reviewer/pr-<N>/parser-critical` フラグファイルが作成されているかでも判定できる（exit 6 でなくてもフラグの有無で parser critical か判断可能）。

exit 6 以外が返ったら parser critical PR ではない。`SKILL.md` 本体の「AIレビュー実行（非 parser critical PR）」節に従う。
exit 6 が返ったら → 以下の手順で実行する。

## 実行手順（`tidd ai-review --stop-before-merge` を使用・Issue #2645）

**タイミング計測（Issue #2644・#3516）:** `step5-airview-start` / `step5-airview-end` は `tidd ai-review`（`--stop-before-merge` を含む）が backend レビュー実行前後に自己記録するため、手打ち mark は不要（#3553）。verdict の `started_at`/`ended_at` も本体が実測する。

1. **STEP 1**: `gh pr view` で PR 情報取得（parser critical フラグは確定済み。diff は取得しない・#3646）
2. **STEP 2**: `uv run --project projects/py/tidd_tools tidd ai-review --stop-before-merge <PR番号>` を実行する
   - exit 10（APPROVE）: レビュー本文が `$AGENT_REVIEW_DIR/agent-review-<PR>.md`（未設定時 `/tmp/agent-review-<PR>.md`）に保存済み。STEP 3 へ進む
   - exit 11（REQUEST_CHANGES）: 直接 STEP 6 へ進む（`--continue-with-verdict REQUEST_CHANGES <PR番号>` を実行）
   - exit 3（全バックエンド利用不可）: STEP 5.5 が人間エスカレーションを発動（backend は claude-code 全滅相当として扱う）
   - exit 1（size/XXL gate・test-plan gate 等）: レビュー到達前の gate 失敗。人間エスカレーションではなく issue-next のリトライループに従って修正 → push → 再実行する
   - exit 5（テスト status gate・#1982）: `pytest/*` / `jest/*` の commit status が FAILURE/ERROR のためレビュー到達前に中断（`--stop-before-merge` 実行時も同じ gate を通る・#3628）。人間エスカレーションではなく issue-next のリトライループに従って修正 → push → 再実行する
   - exit 2（エスカレーション）: `handle_escalation()` が PR コメント投稿とラベル付与を済ませている。追加コメントを投稿せず人間に委ねる
   - exit 6（parser critical gate・Issue #3630）: `--stop-before-merge` が付いていない場合に parser critical 判定で返る。STEP 2 のコマンド（`--stop-before-merge` 付き）を実行し直す
   - その他: エラー。人間にエスカレーション
3. **STEP 3**: レビュー本文を読み込む（`cat $AGENT_REVIEW_DIR/agent-review-<PR番号>.md`）
   **Note（Issue #2659）:** ファイル先頭行に `<!-- review-sha: <sha> -->` メタデータが付与されている。`VERDICT:` 行以降が実レビュー本文のため verdict-extractor への入力はそのまま渡してよい（HTML コメント行は VERDICT: より前に出現する）。
4. **STEP 4（=旧 STEP 5）**: verdict-extractor subagent で verdict を構造化抽出
5. **STEP 5.5**: `_is_parser_critical=true` かつ STEP 4 の primary verdict が `APPROVE` の場合のみ実施する。**primary verdict が `REQUEST_CHANGES` の場合は STEP 5.5 をスキップし直接 STEP 6 へ進む**（secondary consensus は APPROVE 判定にのみ必要なため）。primary が APPROVE の場合、backend を確認してから secondary consensus を実施:
   - backend 名は `~/.cache/tidd/ai-reviewer/pr-<PR番号>/backend-name` から読む（`tidd ai-review --stop-before-merge` が書き込む）
   - primary が agy/codex かつ APPROVE → ai-reviewer subagent を起動
     - **subagent 起動成功** かつ両方 APPROVE → `~/.cache/tidd/ai-reviewer/pr-<N>/consensus.json` に監査ログ書き込み → STEP 6 へ（consensus コメントは `tidd ai-review --post-comment <N> "consensus: 2/2 APPROVE" --no-reviewer-footer` で投稿・Issue #2660）
     - **subagent 起動成功** かつ secondary が REQUEST_CHANGES → secondary レビュー本文を `tidd ai-review --post-comment <N> "<secondary本文>" --reviewer "ai-reviewer subagent"` で投稿（Issue #2660）してから、`tidd ai-review --consensus-verdict <N> REQUEST_CHANGES <SECONDARY_ISSUES_JSON> <ATTEMPT>` で判定する（Issue #2657）:
       - exit 0: consensus 通過 → STEP 6 へ（通常は secondary APPROVE 時のみ、ここでは到達しない）
       - exit 1: 1 回目の不一致 or 指摘内容が変化 → `needs-human-merge` ラベル付与なしでリトライ継続
       - exit 2: 同一指摘 2 回連続 or バックストップ上限 or issues 空 → `needs-human-merge` ラベル付与済み → exit 2
     - **⚠️ subagent 起動不能（Agent tool 呼び出し失敗・claude-code バグ等）** → **Claude inline fallback は禁止**（同一モデル自作自演で consensus 崩壊）。PR コメント "secondary consensus 不能" + `needs-human-merge` ラベル → **exit 2 で人間エスカレーション**
   - primary が claude-code（全滅 fallback・exit 3 相当）→ 同一 backend 系統のため人間エスカレーション（exit 2）
6. **STEP 6**: verdict に応じて `--continue-with-verdict` を実行し、レビュー本文を PR に投稿する:
   - **primary verdict が `REQUEST_CHANGES`（STEP 5.5 未実施・#2079）** → `tidd ai-review --continue-with-verdict REQUEST_CHANGES <N>` を実行する。これにより 1 回目のレビュー本文（指摘事項）が PR コメントとして投稿され、監査証跡として残る。**修正に直接進んではならない**（レビュー内容が GitHub 上に残らず消える）。投稿後は issue-next（メイン SKILL.md）のリトライループに従って修正 → push → 再実行する
   - **consensus 通過（2/2 APPROVE）** → `tidd ai-review --continue-with-verdict APPROVE <N>` でマージ

**STEP 6 完了後（`--continue-with-verdict` 実行後・終了コードに関わらず）:** `step5-airview-end` は `tidd ai-review` が自己記録済み（`--stop-before-merge` 実行時に start/end 記録済み・#3553）のため、手動 mark は不要。

**Issue #2074:** `--continue-with-verdict APPROVE` は `_continue_approve()` 内で `_post_test_statuses()`（`core.py` と共通）を呼び出し、マージ前に ruff-format/ruff-lint/mypy/pytest/jest の commit status を投稿する。`tidd ai-review --stop-before-merge` は `core.py::main()` を実行するため、test-plan（lint/pytest）が通過していることが保証される。

## CRITICAL

- **Issue #1410:** parser critical PR で `ai-reviewer` subagent 起動に失敗したら、Claude 自身が inline で ai-reviewer 役を演じてはならない（`.claude/skills/ai-review/SKILL.md` STEP 3.5 の CRITICAL 節参照）。同一プロセス内 primary/secondary 兼任は独立判定にならず、`#1290` で確立した多 backend consensus 設計が崩れる
- リトライは issue-next（メイン SKILL.md）が管理し、REQUEST_CHANGES の場合は修正後に再実行する
- **旧フロー（`/ai-review` SKILL を直接起動して手書きプロンプトを組み立てる）は廃止**（Issue #2645）。必ず `tidd ai-review --stop-before-merge` を使うこと
