# STEP 1.7: 既存 PR / 中断レビュー検知（Issue #1232）

> **実行環境（ツール名の読み替え）:** 本スキルのツール名参照は Claude Code 前提で記載している。Codex（`.agents/skills` symlink 経由）で実行する場合は、`Agent(subagent_type="x", ...)` → `spawn_agent(agent_type="x", task_name="x", message=...)` に読み替える（`task_name` のみでは default ロールの agent が起動し `.claude/agents/*.md` 相当のツール制約・output_format 契約が適用されない・Issue #3491。対応表・実測記録: `docs/reference/codex-interop.md`「6-4. spawn_agent の `agent_type` 未指定時は default ロールが起動する」）。`Edit` / `Write` → `apply_patch` に読み替え、`mcp__github__*` は Codex 側の GitHub MCP 設定が済んでいれば同ツール名のまま、未設定なら `gh` CLI で実行する。

`/issue-next` の STEP 1.7 詳細フロー。
`closes #<N>` を含む open PR が存在する場合のみ読む。存在しない場合は STEP 2 へ進む。

STEP 2（実装・issue-implementer 委譲）に進む前に、`closes #<N>` を含むオープン PR が既に存在するかを確認する。
存在する場合は「新しいブランチ + 新しい PR」を作らず、既存 PR のレビュー状態に応じて分岐する。

これは前セッションで PR 作成後・AI review 完了前にプロセスが死亡した場合の孤児 PR
（verdict の無いまま人間マージ待ちに滞留する PR）を自動回収するための分岐である。

## 手順

### 1. オープン PR の検索

`mcp__github__list_pull_requests({owner, repo, state: "open"})` で全 open PR を取得し、
返り値の各 PR の `body` フィールドに `closes #<N>` を含むものを filter する。

filter を適用した後、マッチする PR の `number` フィールドを取得する。

マッチする PR が無い（空文字が返る）場合は STEP 2 へ進む（通常の初回実装フロー）。

### 2. 既存 PR がある場合の AI review キャッシュ判定

AI review のキャッシュディレクトリ（`tidd_tools.shared.paths.cache_dir() / "ai-reviewer" / f"pr-{PR番号}"`。
Linux/WSL・macOS・Windows で `platformdirs` が返す OS 別のキャッシュパスに解決される）の状態を
`tidd_tools` の中断検知ヘルパで判定する。
`tidd_tools.ai_review.orphan.detect_orphan_review()` は以下 3 状態を返す純関数で、
キャッシュディレクトリ内の `verdict` センチネルファイルまたは
`timing.json` のいずれかの行に `"verdict"` フィールドが含まれるかで分類する:

| 状態 | 条件 | 分岐先 |
|------|------|--------|
| `NO_CACHE` | cache dir が存在しない or 中身が空 | 通常の初回レビューフロー（STEP 5 で `tidd ai-review <PR> 1` を同期実行） |
| `COMPLETED` | `verdict` センチネルファイルあり or `timing.json` のいずれかの行に `"verdict"` エントリあり | 現行フローに従う（人間マージ待ちなら報告して終了、REQUEST_CHANGES なら STEP 5 のリトライループ（issue-fixer 委譲）に従い修正） |
| `INTERRUPTED` | 何らかのキャッシュはあるが `verdict` 観測手段（センチネル or timing.json）が無い | **中断検知**。同 PR で `tidd ai-review <PR> <試行回数>` を **同期再実行**して verdict を回収する |

### 3. 中断レビュー再開時のブランチ切り替え

`INTERRUPTED` を検知した場合、既存 PR の `headRefName` を取得して worktree を作り、
その worktree 内で `tidd ai-review` を同期実行する。**新しいブランチは作らない。**

Claude Code セッション内での手順:

- `mcp__github__list_pull_requests({owner, repo, state: "open"})` で PR 一覧を取得
- `body` に `closes #<N>` を含む PR を filter して `number` を取得（`_pr_num`）
- `mcp__github__get_pull_request({owner, repo, pull_number: _pr_num})` で `head.ref` を取得（`_head_ref`）
- worktree 作成と ai-review 実行は shell:

```bash
git fetch origin
git worktree add "../<repo>-issue-<N>-resume" "origin/${_head_ref}"
cd "../<repo>-issue-<N>-resume"
tidd ai-review "$_pr_num" 1
```

その後は STEP 5 の終了コード分岐（0/1/2/3/4）に従う。

## CRITICAL

- **`INTERRUPTED` を検知しても新しい PR を作らない。** 既存 PR に対して `tidd ai-review` を再実行するのが目的である
- **AI review キャッシュディレクトリを手動削除しない。** 削除すると `NO_CACHE` 扱いになり、`tidd ai-review` が **試行1回目扱い**で test-plan から再走することになる（実害はないが冗長）
- **verdict の有無だけを見る。** `test-plan-status` や `bats-status` の値は判定に使わない
