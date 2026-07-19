---
name: create-issue
description: GitHub Issue 本文を Claude Code Agent tool + subagent で生成する（Issue #1302 で Anthropic API 全廃）。バグ・改善点のコンテキストを渡すと必須セクション（## 背景 / ## やること / feat/fix なら ## 振る舞い）を含む Markdown 本文を返し、`gh issue create` に流し込める。
---

# /create-issue

Issue 本文生成 skill。Claude Code の Agent tool で `issue-writer` subagent を起動し、
`.claude/rules/issue-creation.md` のフォーマット規約に沿った Markdown 本文を組み立てる。

**背景:** 従来は `enhance_issue_body()`（`shared/llm_issue_body.py`）が Anthropic API
（Haiku）を直接叩いていたが、Issue #1302 で全廃した。代わりに Claude Code の Agent tool
で `issue-writer` subagent を起動し、Claude Max サブスク枠内で処理する。

## 引数

- `<type>`: `feat` / `fix` / `docs` / `refactor` / `build` / `ci` / `research` のいずれか（必須）
- `<title-short>`: Issue タイトルの短い説明（動詞止め）
- `<context>` or stdin: バグ内容・観測エラー・要件など（自由記述）

例:

```
/create-issue fix "label-pr silent fail 修正"
```

## 手順

### STEP 1: type を検証

`type` が有効な種別か確認する。無効なら以下のエラーを表示して終了する:

```
create-issue: 未知の type '{type}'。有効値: feat/fix/docs/refactor/build/ci/research
```

### STEP 2: priority 分布を取得（相対判定用）

既存 open Issue の `priority` ラベル分布を取得して提示する。これにより issue-writer subagent が
相対評価に基づいた priority を選択できる。

```bash
gh issue list --repo <owner>/<repo> --state open \
  --label "priority: critical" --limit 1000 --json number | jq 'length'
gh issue list --repo <owner>/<repo> --state open \
  --label "priority: high" --limit 1000 --json number | jq 'length'
gh issue list --repo <owner>/<repo> --state open \
  --label "priority: medium" --limit 1000 --json number | jq 'length'
gh issue list --repo <owner>/<repo> --state open \
  --label "priority: low" --limit 1000 --json number | jq 'length'
```

取得に失敗した場合（ネットワーク障害・API レート制限等）はエラーをスキップして STEP 3 に進む。

### STEP 3: Agent tool で issue-writer subagent を起動

以下のプロンプトで Agent tool を呼ぶ。STEP 2 で取得した分布を `priority_distribution` として渡す:

```
Agent(
  subagent_type="issue-writer",
  description="Issue 本文生成",
  prompt=<type / title-short / context / priority_distribution を含むプロンプト>
)
```

`priority_distribution` の例:

```
現在の open Issue の priority 分布:
- critical: 0 件
- high: 12 件
- medium: 29 件
- low: 20 件
合計: 61 件（medium が 48% を占めています）
```

subagent は `.claude/agents/issue-writer.md` の `output_format` に従い次を返す:

```json
{
  "title": "fix: label-pr silent fail 修正",
  "body": "## 背景\n\n...\n\n## やること\n\n- [ ] ...\n\n## 振る舞い\n\nFeature: ...",
  "labels": ["type: fix", "priority: medium"]
}
```

### STEP 4: 静的品質検証

`.claude/rules/issue-creation.md` の静的チェックに相当する検証を行う:

- タイトルが `<type>: ...` 形式か（`🤖` prefix は不要・#2072）
- 本文に `## 背景` が含まれるか
- 本文に `## やること` が含まれるか
- `## やること` セクションに `- [ ]` または `- [x]` チェックボックスがあるか
- `type: feat` の場合は `## 設計の選択肢` セクションが含まれるか
- `type: feat` / `type: fix` の場合は `## 振る舞い` セクションが含まれるか

いずれかが不足していれば subagent に再生成を依頼する（最大 2 リトライ）。

### STEP 5: Issue 起票

生成された title / body / labels で `gh issue create` を実行する:

```bash
gh issue create --title "<title>" --body "<body>" \
  --label "type: <type>" --label "priority: <priority>"
```

`priority:` ラベルは subagent が相対判定した値を使う。取得失敗等で分布が提示されなかった場合は
subagent が `medium` を選択するが、`issue-creation.md` の相対判定基準に従って再考を促す。

### STEP 6: 起票完了報告

作成した Issue の URL を表示する。

## Anthropic SDK 直接呼び出し禁止（#1281 + #1302）

本 skill / subagent の実装では `import anthropic` / `from anthropic import` を使わない。
Claude Code の Agent tool 経由で subagent を起動することで、Claude Max サブスク枠内で処理する。
`.claude/hooks/ban-anthropic-import.py` が違反を機械強制でブロックする。

## 呼び出し方

Claude Code は `.claude/skills/*/SKILL.md` を自動検出するため、ユーザーは `/create-issue` として本 skill を呼べる（`.claude/commands/*.md` を別途用意する必要はない・リポジトリ規約）。呼び出されると Claude Code が Skill tool 経由で本ファイルの STEP 1-6 を順に解釈・実行する。

**exit code のセマンティクス:** 本 skill は Claude Code の Skill tool 経由で LLM が STEP を解釈実行するため、shell script のような「厳密な exit code」を保証する仕組みではない。Gherkin の `exit code 2` は「STDERR にエラーメッセージが表示され、`gh issue create` を実行せず Issue 未起票で終了する」という **観測可能な状態** を指す。STEP 1 の type 検証、STEP 4 の品質検証リトライ上限超過は、この観測可能な終了状態を目指す指針である。

## 呼び出し元パス

- **手動（メインの用途）**: Claude Code セッション内でユーザーが `/create-issue` を直接実行する
- **自動起票フロー (`analyze_loop_errors.py` / `watch_circleci_failures.py`)**: 前者は `.claude/hooks/analyze-loop-on-stop.py` から `subprocess.Popen` で detach された Python サブプロセス、後者は `.circleci/config.yml` の job から起動される CircleCI 上の Python プロセスとして動く。どちらも **Claude Code CLI の LLM 会話ループの外側** で動くため、Agent tool は原理的に呼び出せない（Agent tool は Claude Code CLI 本体のツール呼び出しとしてのみ発火する）。したがって自動起票フローは skill 経由には切り替えず、`shared/llm_issue_body.py` の `enhance_issue_body()` no-op fallback により **常に template body のみ** で起票する（Anthropic API 呼び出しは #1281 / #1302 で廃止済み）。意味的な強化が必要な場合は、起票された Issue を後から Claude Code セッション内で `/issue-review` skill にかけて品質判定する運用でカバーする。

**関連参照:** [docs/reference/create-issue-skill.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/create-issue-skill.md) の「呼び出しフロー」節に本 skill の起動シーケンス、`shared/llm_issue_body.py` の docstring に自動フロー側の fallback 挙動を記載している。

## エラー処理（異常系）

### 無効な type 指定

`type` が `feat / fix / docs / refactor / build / ci / research` 以外の値だった場合、STEP 1 で
exit 2 相当のエラーを表示して終了する。Issue は起票しない。

```
create-issue: 未知の type '{type}'。有効値: feat/fix/docs/refactor/build/ci/research
```

### subagent 呼び出し失敗

Agent tool が subagent 起動に失敗した場合（例: `.claude/agents/issue-writer.md` が存在しない・
プロンプトが長すぎる等）、以下のフォールバックを実行する:

1. STDERR にエラーメッセージを表示
2. 静的テンプレート（`## 背景` / `## やること` の空欄形式）で Issue 本文を生成
3. ユーザーに手動で本文を埋めてもらう旨を表示して終了

### priority 分布取得失敗（STEP 2）

`gh issue list` が一時的に利用不可の場合（ネットワーク障害・API レート制限・認証切れ等）、
以下のメッセージを **stderr** に表示してからエラーをスキップし、STEP 3 に進む（分布提示なし）:

```
create-issue: WARN priority 分布取得に失敗しました: <エラー概要>
```

この場合、issue-writer subagent は `.claude/rules/issue-creation.md` の相対判定基準を参照し、
内部コンテキストから保守的に priority を選択する。Issue 起票は継続する（exit code 0）。

### 品質検証リトライ上限超過

STEP 4 の品質検証で必須セクション欠落が 2 回連続で発生した場合、以下を表示して終了する:

```
create-issue: subagent が必須セクションを含む本文を生成できませんでした。context を具体化して再実行してください。
```

Issue は起票しない。

## 関連

- `.claude/agents/issue-writer.md` — subagent 定義
- `.claude/rules/issue-creation.md` — Issue 品質・フォーマット規約
- `.claude/rules/tool-calling.md` — subagent 前提の Tool Calling 設計指針
- [docs/reference/create-issue-skill.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/create-issue-skill.md) — 詳細ドキュメント
- `tidd_tools.shared.llm_issue_body` モジュール — 互換性スタブ（常に fallback）
