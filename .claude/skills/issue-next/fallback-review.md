# agy クォータ上限時の Claude フォールバック（STEP 5 詳細）

`/issue-next` の STEP 5 詳細フロー。`tidd ai-review` が **exit code 3** を返した場合のみ読む。

**CRITICAL: exit code 3 のみが対象。exit 1（REQUEST_CHANGES）や exit 2（エスカレーション）では絶対に起動しない。**

## 目次

- [exit 3 の確認方法](#exit-3-の確認方法)
- [フローの概要](#フローの概要)
- [詳細手順](#詳細手順)
- [fallback 自体が失敗した時の loop-error 記録](#fallback-自体が失敗した時の-loop-error-記録)

## exit 3 の確認方法

`tidd_tools ai-review` の戻り値の数値そのもの（`echo $?`）で判断すること。
「agy がクォータ超過っぽい」「試行上限を超えた」などの状況証拠で exit 3 と判定してはならない。

exit 3 = agy・codex がすべてクォータ超過または利用不可（全バックエンド利用不可）

exit 2 = スマートガードレール発動（最大試行回数超過・同じ指摘が2回連続）→ **エスカレーションであり Claude フォールバック禁止**

## フローの概要

1. Agent tool を `subagent_type: "ai-fallback-reviewer"` で呼び出す
2. subagent の JSON を parse して `format_fallback_verdict()` で変換
3. backend-name を記録する
4. VERDICT に応じてリトライループを継続する（`--continue-with-verdict` がコメント投稿も担う）

## 詳細手順

### 1. ai-fallback-reviewer subagent の起動

```
Agent(
  subagent_type="ai-fallback-reviewer",
  description="PR #<PR番号> のフォールバックレビュー",
  prompt="<mcp__github__get_pull_request の PR 本文> と <mcp__github__get_pull_request_diff の diff>"
)
```

subagent は JSON `{"verdict": "APPROVE|REQUEST_CHANGES", "issues": [...], "rationale": "..."}` を最終コードブロックで返す。

### 2. verdict を agy/codex 互換 markdown に変換

**`tidd_tools.ai_review.fallback_formatter.format_fallback_verdict()` を呼び出して決定的に変換する**（Issue #1697）。LLM に手動書き換えさせない。

```bash
# subagent の JSON を /tmp/agent-review-<PR番号>.json に保存済みとする
uv run --project projects/py/tidd_tools python -c '
import json, sys
from tidd_tools.ai_review.fallback_formatter import format_fallback_verdict
payload = json.load(open(sys.argv[1]))
sys.stdout.write(format_fallback_verdict(payload))
' /tmp/agent-review-<PR番号>.json > /tmp/agent-review-<PR番号>.md
```

verdict が `APPROVE` / `REQUEST_CHANGES` 以外だった場合は `ValueError` が送出される。この場合は subagent の返答が不正なため人間にエスカレーションし、`fallback-failed` として記録する（下記参照）。

変換後の markdown フォーマット:

```
VERDICT: APPROVE または REQUEST_CHANGES

## サマリー
<subagent が返した rationale をそのまま>

## 指摘事項
- [CRITICAL] file:line: 説明
- [HIGH] file:line: 説明
- [MEDIUM] file:line: 説明
- [LOW] file:line: 説明
- [DEFERRED] file:line: 説明
```

### 3. backend-name を記録する

```bash
mkdir -p "${HOME}/.cache/ai-dev-handbook/ai-reviewer/pr-<PR番号>"
printf 'claude-code:claude-sonnet-4-6\n' > "${HOME}/.cache/ai-dev-handbook/ai-reviewer/pr-<PR番号>/backend-name"
```

（注: macOS では `~/Library/Caches/ai-dev-handbook/` に解決される。`_state_dir()` = `tidd_tools.shared.paths.cache_dir() / "ai-reviewer" / f"pr-{PR番号}"` が実 path を返す。Issue #1754）

### 4. VERDICT に応じてリトライループを継続する

`--continue-with-verdict` が内部で PR コメント投稿（`_post_comment_with_app_token()`）と reviewdog inline コメント投稿を行う。コメント投稿は `--continue-with-verdict` に一本化し、`--post-comment` を別途呼び出してはならない（二重投稿になる）。

- APPROVE → `uv run --project projects/py/tidd_tools python -m tidd_tools ai-review --continue-with-verdict APPROVE <PR番号>` → STEP 6
- REQUEST_CHANGES → `uv run --project projects/py/tidd_tools python -m tidd_tools ai-review --continue-with-verdict REQUEST_CHANGES <PR番号>` を実行してコメントを投稿する → 修正して push し、試行回数をインクリメントして再実行
- Agent tool が利用できない場合（subagent 起動失敗等）→ 下記の失敗記録後に人間エスカレーション

## fallback 自体が失敗した時の loop-error 記録（Issue #1750）

**exit 3 (quota-exceeded) は正常な fallback 経路であり anomaly ではない。**
**Agent tool fallback 自体が失敗したケースのみが本当の anomaly。**

以下のいずれかに該当した時に必ず記録する:

1. Agent tool 呼び出しが起動段階で失敗（subagent が起動できない・permission 拒否等）
2. subagent が返す JSON を parse できない（コードブロックが見つからない・JSON syntax error）
3. `format_fallback_verdict()` が `ValueError` を raise した（verdict が APPROVE / REQUEST_CHANGES 以外）
4. `--continue-with-verdict` 内のコメント投稿に失敗した（GitHub API エラー・token 期限切れ等）

```bash
uv run --project projects/py/tidd_tools python -m tidd_tools loop-error-log \
  --pr <PR番号> \
  --step ai-review:fallback-failed \
  --error "<失敗の 1 行サマリー>" \
  --source issue-next
```

`--error` には具体的な失敗理由を短く書く（例: `Agent tool call failed: subagent_type not found`・`ValueError from format_fallback_verdict: invalid verdict='UNKNOWN'`）。

**`--step ai-review:quota-exceeded` は使わない:** このステップ名で記録すると `analyze-loop-errors` が正常な fallback 経路を anomaly として自動 Issue 化してしまう（Issue #1750 の再発）。必ず `ai-review:fallback-failed` を使う。

記録後は `SKILL.md` 本体のエスカレーション処理と同様に人間に委ねる。**Claude 自身が inline で ai-fallback-reviewer 役を演じない**（同一プロセス内 primary/secondary 兼任は独立判定にならないため）。
