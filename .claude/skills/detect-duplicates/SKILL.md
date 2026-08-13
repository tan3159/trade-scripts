---
name: detect-duplicates
description: open Issue を全スキャンして意味的重複疑いペアを検出し duplicate-suspect ラベル + コメントを付与する（Issue #1306）。日次 schedule または手動 slash command として発火する。
---

```
mcp__github__list_issues({owner, repo, state: "open", per_page: 100})
# 100件を超える場合は page パラメータで巡回する（最大 500 件）
# 各 Issue から {number, title, labels: [label.name, ...], body: body[0:500]} を抽出する
```

- `body` は先頭 500 文字に限定してトークンを節約する
- ページ巡回で最大 500 件取得（通常は十分な上限）

**Issue 数が 100+ の場合（Issue #1466 対応）:** `tidd detect-duplicates-batch` サブコマンドで priority + created_at 順に並び替え、30 Issue/batch で分割してから subagent に渡す。各 batch の実行時間は `~/.cache/detect-duplicates-perf/batch-<N>.json` に自動記録される。

```bash
# Issue 数が多いときの batch 分割 + perf 記録（Issue #1466）
tidd detect-duplicates-batch --batch-size 30 --dry-run
```

`--dry-run` を付けると各 batch の issue 番号を stderr にログ出力するだけで実質処理をスキップする（perf 記録は常に行う）。skill 側の subagent 起動 loop は本 CLI の scope 外であり、SKILL.md 側で STEP 2 以降を batch ごとに繰り返す運用に切り替える（batch 分割ロジックの詳細は `tidd_tools.detect_duplicates_batch` モジュールを参照）。

**MCP tool 失敗時のエラーハンドリング:** MCP call が失敗した場合は skill 全体を停止する:

```
issues = mcp__github__list_issues({owner, repo, state: "open", per_page: 100})
# エラー時は「Issue 一覧取得に失敗しました」と出力して exit 1
```

結果を `issues` として保持する。Issue 数が 0 件（`[]`）なら以下を出力して exit 0:

```
重複疑い Issue は検出されませんでした
```

subagent 起動は行わない。

### STEP 2: Agent tool で duplicate-detector subagent を起動

全 issues リストを **全 subagent に渡す**（クロスパーティション重複を見逃さないため）。
各 subagent は「自分のアンカー担当範囲の Issue（lower 番号）を含むペア」のみを探す。

**分割基準:**
- Issue が 50 件以下: 1 つの subagent で全件スキャン
- 51 件以上: Issue 番号昇順でソートして均等に 3 分割し、3 subagent を並列起動

```
Agent(  # Claude Code: Agent tool。Codex: spawn_agent(agent_type="duplicate_detector", task_name="duplicate_detector", message=...) に読み替え
  subagent_type="duplicate-detector",
  description="重複疑いペア検出（anchor: Issue #X〜#Y 担当）",
  prompt=<以下の形式で全 issues + anchor 範囲を渡す>
)
```

**subagent へのプロンプト例（並列起動時）:**

```
以下の GitHub Issues リストから意味的に類似した（重複疑いのある）ペアを検出してください。

あなたの担当: ペアの中で番号が小さい方（anchor）が [<start>〜<end>] の範囲のペアのみを返してください。
（全件比較するが、anchor 番号でフィルタして重複報告を防ぐ）

precision より recall を優先してください（見逃しを減らす）。
タイトル・ラベル・本文冒頭 500 文字を参考に判断してください。

Issues（全件）:
<issues の JSON>

output_format に従い JSON を返してください。
```

### STEP 3: 結果を集約

各 subagent の結果（JSON）から `pairs` を収集・マージする。
同一ペアの重複を除去する（`(min(a,b), max(a,b))` をキーにして dedup）。

**JSON parse エラー時のエラーハンドリング:** subagent の出力は末尾に ` ```json ... ``` ` の
markdown コードブロックを含む。**コードブロック抽出 → JSON 検証** の 2 段階で処理し、
どちらかが失敗すれば skill 全体を停止する。**部分結果を用いた副作用（ラベル・コメント）は
一切発生させない**:

```bash
# STEP 3-a: markdown コードブロックから JSON 部分を抽出する（複数 subagent 分をループ）
# 末尾の ```json ... ``` を優先し、なければ ``` ... ``` の最後のブロックを試す
extract_json_block() {
  local raw="$1"
  # 末尾の ```json ... ``` を抽出（sed で最終ブロックを取り出す）
  local block
  block=$(printf '%s' "$raw" | sed -n '/```json/,/```/p' | sed '1d;$d' | tail -n +1)
  if [ -z "$block" ]; then
    # フォールバック: ```...``` の最後のブロックを試す
    block=$(printf '%s' "$raw" | sed -n '/```/,/```/p' | sed '1d;$d' | tail -n +1)
  fi
  printf '%s' "$block"
}

json_body=$(extract_json_block "$subagent_output")
if [ -z "$json_body" ]; then
  printf 'duplicate-detector JSON parse error: subagent 出力にコードブロックが見つかりません\n' >&2
  exit 1
fi

# STEP 3-b: jq で構造検証（pairs キーの存在）
if ! parsed=$(printf '%s' "$json_body" | jq -e '.pairs' 2>/dev/null); then
  printf 'duplicate-detector JSON parse error: subagent が壊れた JSON を返しました\n' >&2
  exit 1
fi
```

**入力バリデーション（コマンドインジェクション対策）:** subagent が返した `pairs[].a` /
`pairs[].b` は整数として **検証必須**。非数値 / 記号を含む値は STEP 4 で `mcp__github__update_issue` / `mcp__github__add_issue_comment` の
引数として渡す前に reject する:

```bash
if ! [[ "$a" =~ ^[0-9]+$ ]] || ! [[ "$b" =~ ^[0-9]+$ ]]; then
  printf 'duplicate-detector rejected non-integer issue number: a=%q b=%q\n' "$a" "$b" >&2
  exit 1
fi
```

reason 文字列は STEP 4 で `--body-file` 経由（シェル未経由）で扱うため追加の検証は不要
（既に Write tool の JSON パラメータで隔離される）。

重複疑いペアが 0 件ならスキップして終了する:
```
重複疑い Issue は検出されませんでした
```

### STEP 4: duplicate-suspect ラベルと「重複疑い」コメントを付与

集約したペア `[(a, b, reason), ...]` に対してそれぞれ実行する。

**前提:** STEP 3 で `<a>` / `<b>` が既に整数バリデーション済み（`^[0-9]+$` にマッチ）。
バリデーション未通過の値は STEP 3 で reject 済みのため本 STEP には到達しない。

**ラベル付与（整数バリデーション済み値のみ）:**
```
# 既存 labels を取得してから追記する（mcp__github__update_issue は labels を全置換するため）
issue_a = mcp__github__get_issue({owner, repo, issue_number: <a>})
mcp__github__update_issue({owner, repo, issue_number: <a>, labels: [<issue_a の既存 labels> + "duplicate-suspect"]})

issue_b = mcp__github__get_issue({owner, repo, issue_number: <b>})
mcp__github__update_issue({owner, repo, issue_number: <b>, labels: [<issue_b の既存 labels> + "duplicate-suspect"]})
```

**コメント投稿（`reason` は Issue 本文由来の非信頼入力のため `mcp__github__add_issue_comment` の body パラメータとして渡す）:**

MCP tool の `body` パラメータは JSON フィールドとして内部処理されシェルを経由しない。
`<a>`・`<b>`・`<reason>` を実際の値で置換して MCP tool を呼び出す。

```
mcp__github__add_issue_comment({owner, repo, issue_number: <a>, body: "重複疑い: #<b>（理由: <reason>）\n/detect-duplicates skill が検出。着手時に相互確認して片方をクローズするか、重複でなければ `duplicate-suspect` ラベルを除去してください。"})

mcp__github__add_issue_comment({owner, repo, issue_number: <b>, body: "重複疑い: #<a>（理由: <reason>）\n/detect-duplicates skill が検出。着手時に相互確認して片方をクローズするか、重複でなければ `duplicate-suspect` ラベルを除去してください。"})
```

### STEP 5: 結果を報告

```
/detect-duplicates 完了:
- スキャン: <N> 件
- 重複疑いペア: <M> 組
- ラベル付与 + コメント済み: <pair 一覧>
```

## 実行頻度・schedule 連携

Issue #1282 (schedule PoC) の結果に応じて以下のいずれかで運用する:

- **PoC 成功**: `tidd schedule` または Claude Code schedule で `cron: 0 9 * * *`（毎朝 9:00 JST）自動発火
- **PoC 失敗**: 週次 or 着手前に手動 `/detect-duplicates` を実行する

## 注意事項

- `duplicate-suspect` ラベルは **疑い** であり確定ではない。人間が着手時に精査する（Phase 2: #1307）
- 誤検知で付いたラベルは手動で外してよい
- **再検出について**: ラベルを除去しても、次回 skill 発火時に意味的類似が続く場合は再検出・再コメントされる。意味的に類似した Issue は誤検知ではないため除去しなくてよい。着手時に相方 Issue と確認して重複かどうかを判断する
