# action log と drift チェック（Issue #4029 Epic・#4037・#4038）

`/issue-next` の STEP 2（issue-implementer 委譲）・STEP 5（issue-fixer 委譲）で subagent が
契約（`gh pr create`/push で終端）を超えて追加の状態変更コマンドを実行した場合に、機械的に
検知するための2つの機構をまとめる。

- **action log**（#4037）: `cache/issue-next-state/issue-<N>.json` に「いつ・誰が・何をしたか」を
  追記記録する監査ログ
- **drift チェック**（#4038）: action log に記録された「直近 observed PR 状態」と、
  `gh pr edit`/`merge`/`close` 実行直前の実際の PR 状態を照合し、不一致（drift）があれば
  該当コマンドをブロックする PreToolUse hook

**関連 Issue:** #4029（Epic・背景の詳細）・#4037（action log 追加）・#4038（drift チェック hook 追加）

---

## action log のフィールド定義

`cache/issue-next-state/issue-<N>.json` の `actions` 配列に、状態変更コマンド実行のたびに
1 エントリが追記される（`issue_next_state.py` の `_append_action()` / `append_action()`）。

```json
{
  "at": "2026-08-18T00:01:00Z",
  "actor": "orchestrator",
  "action": "observe-pr",
  "details": {
    "pr_number": 123,
    "state": "OPEN",
    "headRefOid": "aaa111...",
    "updatedAt": "2026-08-18T00:00:00Z"
  }
}
```

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `at` | ISO8601 文字列（UTC・`Z` 終端） | 必須 | エントリ記録時刻 |
| `actor` | 文字列 | 必須 | `"orchestrator"` または `"subagent:<agent_type>"`。現状 `issue_next_state.py` 内のコマンドはすべて `/issue-next` オーケストレータ（main session）からのみ呼び出されるため常に `"orchestrator"` になる（`subagent:<agent_type>` は将来 subagent 側から直接記録する経路を追加する場合の予約値） |
| `action` | 文字列 | 必須 | 実行されたコマンド名（`init`/`consume`/`mark-quality-check-done`/`observe-pr` 等） |
| `details` | dict | 任意 | `action` 固有の付随情報。`observe-pr` のみ使用（下記） |

**`action` 値の一覧:**

| `action` | 記録元 | `details` | 用途 |
|---|---|---|---|
| `init` | `issue_next_state._cmd_init()` | なし | state ファイル初期化 |
| `consume` | `issue_next_state._cmd_consume()` | なし | キュー先頭を current_issue へ進める |
| `mark-quality-check-done` | `issue_next_timing.py`（`append_action()` 経由） | なし | STEP 1.5 品質チェック完了マーク |
| `observe-pr` | `issue_next_state._cmd_observe_pr()` | `{"pr_number": int, "state": str, "headRefOid": str, "updatedAt": str}` | drift チェックの baseline 記録 |

`clear` は state ファイルごと削除するため `actions` も同時に失われる（`## 設計の選択肢` で
別ファイル分離案を不採用としたのと同じ理由でファイルのライフサイクルを一致させている）。

---

## drift チェックの発動ポイント

`require-pr-state-drift-check.py`（PreToolUse hook・default OFF）は `gh pr edit`/`gh pr merge`/
`gh pr close` を含む Bash コマンドをすべて検知対象とする。以下の順で判定する:

1. コマンドから PR 識別子（番号/URL/ブランチ）を抽出する。抽出できない場合は skip（fail-open）
2. `gh pr view <識別子> --json number,state,headRefOid,updatedAt,body` で現在の PR 状態を取得する
3. `body` から `closes #<N>` 等で Issue 番号を特定する。特定できない場合は skip
4. `cache/issue-next-state/issue-<N>.json` の `actions` を新しい順に走査し、
   `action == "observe-pr"` の最新エントリ（baseline）を探す。見つからない場合は skip
5. baseline と現在値を `state`・`headRefOid`・`updatedAt` の 3 フィールドで比較する
6. いずれか 1 つでも異なれば exit 2 でコマンドをブロックし、不一致フィールド名・
   「最後に観測した値」・「現在の値」を stderr に出力する。すべて一致すれば exit 0 で通過する

**baseline はどこで記録されるか:** `tidd issue-next-state observe-pr <N> <PR>` を実行した時点の
`gh pr view` 結果が baseline として記録される。オーケストレータは
`.claude/skills/issue-next/subagent-delegation.md` の STEP 2（issue-implementer 完了報告の
機械検証）・STEP 5（issue-fixer 完了報告の機械検証）で、PR の実在・OPEN 確認と併せてこのコマンドを
実行する。これにより「report 受領時点で観測した状態」が baseline となり、その後 subagent が
契約を超えて追加のコミット・PR 編集・close 等を行った場合（例: STEP 3〜5 の処理中に headRefOid が
変化する）、次に `gh pr edit`/`merge`/`close` が実行される瞬間に drift として検知される。

**段階的導入・fail-open 方針:** baseline が 1 件も記録されていない Issue（`observe-pr` を
一度も呼んでいない・#4038 導入前からの既存 Issue 等）では drift チェックは skip される
（exit 0 + stderr WARN）。導入初期は既存フローを壊さないことを優先し、`observe-pr` の呼び出しを
オーケストレータ側の運用（subagent-delegation.md）に委ねる設計とした。

**hook 自身が行わないこと:** `require-pr-state-drift-check.py` は GitHub API へのラベル付与・
コメント投稿を一切行わない。drift 検知（exit 2）を受けての `🙋 needs-human-input` ラベル付与・
差分コメント投稿・マージ済みコードの検証は、オーケストレータ側（Claude Code セッション）の
既存の park 処理（`.claude/skills/issue-next/subagent-delegation.md`「park 時の処理」）が担う。

---

## 関連ドキュメント

- `docs/reference/hooks.md#require-pr-state-drift-checkpy`（未配布・baseline 登録済み）
- [`.claude/skills/issue-next/subagent-delegation.md`](../../.claude/skills/issue-next/subagent-delegation.md)
- `projects/py/tidd_tools/src/tidd_tools/issue_next_state.py`（`actions`/`observe-pr` の実装）
