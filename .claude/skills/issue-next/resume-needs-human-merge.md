# needs-human-merge PR resume フロー（引数なしモード事前チェック）

> **実行環境（ツール名の読み替え）:** 本スキルのツール名参照は Claude Code 前提で記載している。Codex（`.agents/skills` symlink 経由）で実行する場合は、`Agent(subagent_type="x", ...)` → `spawn_agent(agent_type="x", task_name="x", message=...)` に読み替える（`task_name` のみでは default ロールの agent が起動し `.claude/agents/*.md` 相当のツール制約・output_format 契約が適用されない・Issue #3491。対応表・実測記録: `docs/reference/codex-interop.md`「6-4. spawn_agent の `agent_type` 未指定時は default ロールが起動する」）。`Edit` / `Write` → `apply_patch` に読み替え、`mcp__github__*` は Codex 側の GitHub MCP 設定が済んでいれば同ツール名のまま、未設定なら `gh` CLI で実行する。

`tidd resume-candidates` が PR 番号を stdout に出力した場合（候補あり）、新規 Issue 選定より先に本フローを実行する。

## 目次

- [実行タイミング](#実行タイミング)
- [前提: consensus 不一致 PR の除外](#前提-consensus-不一致-pr-の除外issue-2655)
- [resume フロー](#resume-フロー)
- [設計根拠](#設計根拠)

## 実行タイミング

引数なしモードの STEP 1 冒頭（Issue 選定前）で、PR 数上限チェック（check-pr-conflicts --count-only）の前に実行する。

```bash
candidates=$(tidd resume-candidates)
# exit code は常に 0。stdout が空または「候補なし」で始まる場合はスキップ
```

stdout に `#<数字>` が含まれる場合 → resume フローへ進む。

## 前提: consensus 不一致 PR の除外（Issue #2655）

`tidd resume-candidates` は `needs-human-merge` PR のうち、consensus 不一致で停止した PR を自動的に除外して出力する。
具体的には、`~/.cache/tidd/ai-reviewer/pr-<N>/consensus.json` の `verdict` が `REQUEST_CHANGES` である PR は resume 対象外として stderr にメッセージを出力し、stdout には含めない。

| consensus.json の状態 | resume 候補か |
|---|---|
| ファイルなし（通常の exit 4 停止） | 候補になる |
| `verdict: APPROVE`（tree hash キャッシュ） | 候補になる |
| `verdict: REQUEST_CHANGES`（consensus 不一致） | **候補にならない** |

この除外により、CRITICAL 指摘が未修正のまま自動マージされる経路を機械的に防止する。

## resume フロー

`needs-human-merge` PR は「ai-review が APPROVE を出し、Issue やることの証跡検証を待っているが誰も拾っていない」状態（consensus 不一致 PR は上記の前提で除外済み）。以下を実行する:

### 1. PR 情報の取得

```
mcp__github__get_pull_request({owner, repo, pull_number: <PR番号>})
```

- `closes #N` を本文から抽出して関連 Issue 番号 N を特定する
- CI ステータス（checks）を確認する（FAILURE があれば後述のエスカレーションへ）

### 2. Issue やること消化の証跡検証

```
mcp__github__get_issue({owner, repo, issue_number: N})
```

Issue `## やること` の各チェックボックスを確認する:
- **未消化あり（`- [ ]`）:** `tidd tick-evidence <PR番号>` で evidence を取得し、証跡付き tick + エビデンスコメントを投稿する
- **全消化済み（すべて `- [x]`）:** そのままマージ判断へ

証跡検証で `tidd tick-evidence` が evidence を提供できない項目がある場合は以下の判断フローへ。

### 3. マージ判断

全チェックボックスが `- [x]` になったら:

```bash
gh pr merge <PR番号> --squash --auto
```

マージ成功後:
- `needs-human-merge` ラベルを PR から除去（マージで自動クローズされる）
- Issue N を close する（closes で自動 close しなかった場合）
- STEP 1 へ進んで次の Issue を選定する

### 4. 検証不能・CI 失敗時のエスカレーション

以下のケースではエスカレーションして停止する:

**a. CI FAILURE がある場合:**
```
PR #<N> の CI が FAILURE です。手動で確認が必要です。

A. CI を再実行して GREEN になるまで待つ（推奨）
B. PR をクローズして別 Issue 起票する

判断できなければ → A（PR URL を確認してください）
```

**b. 証跡確認できない項目がある場合:**
```
PR #<N> の Issue #<M> に証跡確認できない項目があります: <項目名>

A. 手動で証跡を確認して tick し、gh pr merge <N> --squash でマージする（推奨）
B. 未検証のままマージする（リスクあり）

判断できなければ → A
```

**c. park（後回し）する場合:**

resume フロー全体を諦めて STEP 1 の通常 Issue 選定へ進む:
```
needs-human-merge PR #<N> の resume をスキップして次の Issue を選定します。
```

## 設計根拠

- `tidd resume-candidates` は `needs-human-merge` ラベルのオープン PR を照会するだけのシンプルな照会。CI には影響しない
- resume フローは引数なしモードの事前チェックとして PR 上限チェック前に挟む（上限を先に確認してしまうと resume 候補 PR がカウントされて上限誤超と判定されるリスクがある）
- 証跡検証は既存の `tidd tick-evidence` を再利用する（新規機能なし）

詳細仕様: `docs/reference/issue-next-loop-operations.md#needs-human-merge-resume`
