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

## 行動原則

- 日本語・短く端的に。方針判断はユーザーが行う。AIエージェントは実行と情報提供に徹する
- **エスカレーション: 選択肢形式（2-4択・トレードオフ・推奨・デフォルト動作）必須。詳細: `.claude/rules/escalation-format.md`**
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
**GitHub 操作:** セッション内は `mcp__github__*` 優先。tidd_tools/CI/cron は `gh`（詳細: `.claude/rules/workflow.md`）
**Hooks:** `.claude/hooks/` の振る舞い・ブロック条件は配布物の `.claude/rules/`・`docs/reference/hooks.md` を参照

## Codex 固有の補足（#3210）

- hooks は `.codex/hooks.json`（rulesync 相当の生成物・`.claude/hooks/` のスクリプトを共有）。
  初回起動時は `codex /hooks` で新規・変更 hook をレビューして trust する
  （`--dangerously-bypass-hook-trust` は CI 等の自動実行専用）。
- `.agents/skills` は `.claude/skills` への symlink（post-gen タスクが生成）。

# 判断ジャーナル規約

エスカレーション（選択肢形式）にユーザーが回答したら、AI はそのセッション内で
docs/decisions/YYYY-MM-DD-<slug>.md に決定を自動記録する。
**人間への指示不要。AI が自動追記する。**

---

## いつ記録するか（3 条件）

1. **エスカレーション回答時** — どの選択肢を選んだか・条件・理由
2. **壁打ちで採用 / 不採用 / 条件付き採用を決定したとき**
3. **同種の質問に方針を示したとき** — 以後 AI が同じ質問を繰り返さないために記録

ファイル命名: docs/decisions/YYYY-MM-DD-<slug>.md（1 決定 1 ファイル）

---

## 記録ファイルの構成

`# 決定の要約` + ヘッダ（決定日・記録者・参照）> `## 論点` > `## 提示した選択肢`（表）> `## ユーザーの決定` > `## 理由・背景` > `## 今後 AI が取るべき行動`

テンプレート全文・ファイル命名・コミット方法・記録不要ケース: docs/reference/decision-journal-guide.md

---

## 関連

- `.claude/rules/escalation-format.md` — 回答を受けたら本規約に従って記録する

# エスカレーション形式ルール

AI が人間に判断を仰ぐときは **選択肢形式** で提示する。
オープンな質問（「どうしますか？」「どう進めましょう？」）は禁止。

---

## 必須 4 要素

1) **2-4 択・記号付き** — A/B/C/D を先頭に付ける。1 択はエスカレーション不要。5 択以上は絞る。
2) **トレードオフ 1 行** — 各選択肢に「なぜそれをすると何が起きるか」を 1 行で添える。
3) **推奨明示** — 推奨案に `(推奨)` を付ける。不明な場合は「推奨なし（要件次第）」と書く。
4) **デフォルト動作** — 末尾に `判断できなければ -> <安全側の 1 文>` を必ず書く。

---

## テンプレート

```
<状況の 1 文説明>

A. <選択肢 1> — <トレードオフ>(推奨)
B. <選択肢 2> — <トレードオフ>

判断できなければ -> <安全側のデフォルト動作>
```

---

## 関連

- `.claude/rules/decision-journal.md` — 回答を受けたら `docs/decisions/` に自動記録する

詳細（良い例・悪い例・適用ポイント・needs-human-input セクション書式）: docs/reference/escalation-format-guide.md

# 実装制約ルール（やりすぎ防止）

---

## CRITICAL: Issue の「やること」スコープを越えない

`## やること` に**書かれていない**修正・追加は勝手に実装しない。
**判断基準:** 「この変更は `## やること` のどのチェックボックスを満たすか」と自問し、対応がなければ実装しない。
別問題を発見したら別 Issue 起票。スコープ拡大は実装前にユーザーに確認。

---

## やってはいけない 6 パターン（気づいたら別 Issue 起票）

1. **頼んでいないエラーハンドリング** — `## 振る舞い` の異常系 Scenario で要求されているものだけ実装
2. **頼んでいないログ・デバッグ出力** — デバッグ目的が明示されていない限り追加しない
3. **頼んでいない入力バリデーション** — `## 振る舞い` で要求されているバリデーションだけ実装
4. **早すぎる抽象化** — 3 回出現するまで関数・クラスに切り出さない（Rule of Three）
5. **自明なコメント・docstring** — 「why」が非自明な場合のみコメントを書く
6. **ついで実装** — 「この変更を巻き戻したら Issue が完了しないか？」が Yes なら入れる、No なら別 Issue

---

## 例外: やってよい先回り

1. 既存コードとの整合性調整（既存パターン踏襲）
2. テスト先行で書いたテストが要求する実装
3. 明らかなセキュリティ脆弱性の指摘（修正は別 Issue 化を提案）

---

## ツール・ライブラリのエラーが解消しないときは最新の公式ドキュメントを確認する

ツール・ライブラリのエラーが試行錯誤しても解消しないとき、学習時点の設定方法・API が変わっている可能性がある。最新の公式ドキュメント（リリースノート・マイグレーションガイド含む）を確認してから再試行する。

詳細（各パターン詳細例・コード例・違反時対応）: docs/reference/implementation-constraints-guide.md

# Issue作成ルール

hookとAgy（`/issue-review`）の両方から参照される単一の真実源。
合格例・source 分類詳細・priority 相対判定・Gherkin 品質基準詳細は
docs/reference/issue-creation-guide.md を参照。

---

## 全Issueに必須のチェック項目

### フォーマット

| 項目 | 判断基準 |
|------|---------|
| セクション | `## 背景` と `## やること` の両方が存在するか |
| タイトル形式 | AI作成の場合: `🤖 <type>: <説明（動詞止め）>`（🤖プレフィックス必須） |
| やること形式 | 各行が `- [ ]` または `- [x]` 形式（平箇条書き `- タスク名` は不可） |

### Pain（背景の記述品質）

`## 背景` に「〇〇ができないせいで△△が起きている」レベルで Pain が書かれているか。
合格例・不合格例は docs/reference/issue-creation-guide.md 参照。

### ラベル

| 項目 | 判断基準 |
|------|---------|
| `type:` ラベル | `type: feat` / `fix` / `docs` / `refactor` / `ci` / `build` / `research` のいずれか |
| `priority:` ラベル | `priority: critical` / `high` / `medium` / `low` のいずれか |
| `source:` ラベル | **🤖 + `type: fix` の場合のみ**: `source: ci` / `rework` / `human-report` / `new-bug` / `spec-change` のいずれか（人間起票は対象外）。5 分類定義・優先順位は docs/reference/issue-creation-guide.md 参照 |

priority 相対判定（`/create-issue` skill が分布提示・`issue-writer` subagent が相対評価で選択）の基準は
docs/reference/issue-creation-guide.md 参照。

### 粒度・依存関係・ドキュメント更新・方針整合性

| 項目 | 判断基準 |
|------|---------|
| 粒度 | 1つのPRで完結するか（複数の関心事が混在していないか） |
| 依存関係 | 依存Issueの状態を確認してコメントに記載（合否に影響しない）。GitHub ネイティブの `blocked-by` 設定は未解決の依存を自動選定から除外する（#3640） |
| ドキュメント更新 | 実装に伴う `docs/`・`CLAUDE.md` 等の更新が `## やること` に含まれているか（意味チェック） |
| 方針整合性 | `docs/decisions/`・`docs/conventions.md`・`CLAUDE.md` の方針と矛盾していないか |

---

## feat系Issueの追加チェック

`type: feat` ラベルのIssueは以下を追加確認する。

`## 設計の選択肢` セクションに採用案・不採用案（最低1つ）が存在するか。

---

## feat/fix系Issueの追加チェック

`type: feat` または `type: fix` ラベルのIssueは以下を追加確認する。

### hook 契約系 Issue の除外（Issue #1855）

**`## やること` の変更対象が `.claude/hooks/` のみの Issue は `## 振る舞い` 不要。**
代わりに `test_<hookname>_hook.py` 契約テストを書く。

**除外条件（両方を満たすこと）:**
1. `## やること` 内のファイルパス参照が `.claude/hooks/` のみ（他パスが混在しない）
2. `## やること` にファイルパスの記述が存在する

### Gherkin品質基準（検証可能性ゲート）

| 項目 | 判断基準 |
|------|---------|
| セクション存在 | `## 振る舞い` セクションが存在するか |
| Scenario数 | Scenario が1つ以上あるか |
| 正常系・異常系 | 正常系・異常系の**両方**が含まれているか（異常系なしは不合格） |
| Then句 | 具体的な値・観測可能な状態（exit code・出力文字列・ファイル存在等）を示しているか |

詳細（合格例・不合格例・critical モジュール境界値ルール・positive list）は
docs/reference/issue-creation-guide.md 参照。

---

## 期待する出力例（任意セクション）

`## 期待する出力例` は任意セクション（書かなくてもペナルティなし・`validate-issue.py` は存在を強制しない）。
実装前に分かっている出力テキスト（Markdown 表・JSON・ログ行など）をそのまま貼ると、実装 AI がその内容を初回スナップショット/テスト期待値として固定してから実装するため期待通りかを diff で確認しやすくなる。
合格例・hook の動作詳細は
docs/reference/issue-creation-guide.md 参照。

---

## チェック結果の判定

**合格条件:** 全チェックをパス。feat系は設計の選択肢チェックも必須。

**コメント形式:** `## Issue品質チェック結果` + ✅/❌ + 不合格項目リスト。
テンプレートは docs/reference/issue-creation-guide.md 参照。

---

## hookの動作（Claude作成時）

`PreToolUse` hookが `gh issue create` コマンドを捕捉し、以下を機械的にチェックする:

1. `## 背景`・`## やること` セクション存在
2. タイトルが `🤖 <type>: <説明>` 形式か（🤖プレフィックス必須）
3. `## やること` 各行が `- [ ]` または `- [x]` 形式か
4. feat系: `## 設計の選択肢` セクション存在
5. feat/fix系: `## 振る舞い` セクション存在（hook契約系 Issue は除外）

不備があればコマンドをブロックし、Claudeが修正して再実行する。
実装詳細は docs/reference/hooks.md#validate-issuepy 参照。

---

## 関連ドキュメント

- docs/reference/issue-creation-guide.md — 詳細ガイド（合格例・source分類・Gherkin品質）
- `docs/conventions.md` — Issue・PR・コミット規約
- docs/reference/hooks.md — Hooks リファレンス
- [`workflow.md`](./workflow.md) — ワークフロー規約

# レビューバックエンド規約

agy / codex / Claude フォールバックの選択基準・exit code 対応・マージ gate。

**関連:** [`.claude/rules/workflow.md`](./workflow.md)
**詳細:** docs/reference/review-backends-guide.md

---

## スキル使い分け

| スキル | 用途 |
|--------|------|
| `/review` | **PR コードレビュー専用**（`agy /review <PR番号>`） |
| `/issue-review` | **Issue 品質チェック専用**（`agy /issue-review <N>`） |

**IMPORTANT: Issue 品質チェックは `agy /issue-review <N>` を使うこと。`/review` は PR 専用であり Issue に対して使ってはならない。**

---

## tidd ai-review exit code 対応表

| code | 意味 | 対応 |
|------|------|------|
| 0 | APPROVE・全消化済み | 自動マージ |
| 1 | REQUEST_CHANGES | 修正して push → 再実行 |
| 2 | エスカレーション | 人間に委ねる |
| 3 | 全バックエンド利用不可 | `ai-fallback-reviewer` subagent を起動（guide 参照）|
| 4 | 人間マージ待ち | `[手動]` 未完了 または Issue やること未消化 → 人間マージ |
| 5 | テスト status gate による中断（#1982） | テスト修正して push → 再実行 |
| 6 | parser critical PR を --stop-before-merge 無しで呼んだ（#3630） | `--stop-before-merge` を付けて再実行 |

**#1982:** commit status に `pytest/*`・`jest/*` の FAILURE があれば呼び出し前に exit 5 で中断（`AI_REVIEW_SKIP_TEST_STATUS_GATE=1` で無効化）。

**#3630:** parser critical PR を `--stop-before-merge` 無しで呼ぶと exit 6 中断（`AI_REVIEW_SKIP_PARSER_CRITICAL_GATE=1`）。

**#3636:** リトライ 2 回目以降の同一 SHA 再レビュー gate（exit 2・`AI_REVIEW_SKIP_NO_NEW_COMMIT_GATE=1`）。前回レビュー以降に新コミットが無いまま attempt >= 2 で再実行すると exit 2 で中断。詳細: review-backends-guide.md。

---

## Issue やること全消化 gate（#1756）

APPROVE 後に `closes #N` の Issue `## やること` に prefix なし未消化項目が残れば **exit 4 + `needs-human-merge` ラベル付与**。全消化なら auto-merge。`PR_AUTO_ADMIN_MERGE` は廃止済み。

---

## yaru auto-tick（#1534）

`AI_REVIEW_YARU_AUTO_TICK=1`（本番）または `dry-run`（監査ログのみ）で evidence-based 自動 tick を有効化する。導入手順詳細は review-backends-guide.md 参照。

---

## scope-diff チェック（#2597）

APPROVE パスで Issue やること・振る舞いと PR diff を突き合わせ、スコープ超過・未消化の可能性を非ブロッキング PR コメントで指摘する。escape hatch: `AI_REVIEW_SKIP_SCOPE_DIFF=1`。
詳細: docs/reference/review-backends-guide.md

---

## custom backend（OpenAI 互換 API・#3116）

`AI_REVIEW_BACKEND=custom` 固定実行時のみ有効。config.json の `custom-backend` + `ai-review-custom`（デフォルト false・opt-in）で設定する。auto フォールバックには非対応。詳細: review-backends-guide.md。

**#3117:** auto チェーン順序は `ai-review-chain` で変更可。詳細は同上。

---

## 関連ドキュメント

- docs/reference/review-backends-guide.md — 詳細（手順・research）
- docs/setup/codex-setup.md — codex セットアップ
- docs/setup/ai-review-credentials.md — 認証情報

# Test Plan チェックリスト規約

> **注記:** 以下の機械強制（`require-red-first` / `protect-tests` 等）は `config.json` で対応 hook を enable した利用者にのみ適用される。`copier copy` 直後の consumer はすべて default OFF のため、規約記述は参照目的。

PR の `## Test plan` セクションの記述ルール、TDD/BDD ワークフロー、テストフレームワーク選択を定義する。
Gherkin テンプレート・旧パターン比較・追加テスト観点ルールは docs/reference/test-plan-guide.md 参照。

**関連:** [`.claude/rules/workflow.md`](./workflow.md)・[`.claude/rules/testing-framework.md`](./testing-framework.md)

---

## テストフレームワーク選択

**IMPORTANT: テストを作成する前に `.claude/rules/testing-framework.md` でフレームワークを確認すること。**

| 対象 | フレームワーク | 配置場所 |
|------|--------------|---------|
| `projects/gas/*/` | Jest | `projects/gas/<project>/tests/` |
| `projects/py/*/` | pytest | `projects/py/<project>/tests/` |

`projects/` 配下は `tidd run-project-tests` が一元管理する。詳細: `.claude/rules/testing-framework.md`

---

## pytest-bdd による Executable Specification（#1283）

`tidd extract-feature <N>` で `.feature` と `step_defs` skeleton を同時生成する。

**feat/fix PR は `.feature`（+step_defs）または `tests/test_*.py` のどちらかが必須（#1962）。**
`projects/py/*/src/`・`projects/gas/*`（tests 除く）を変更する feat/fix PR に適用。
判定基準: 外部観測可能な振る舞い → `.feature`、内部ユーティリティ・境界値 → `tests/test_*.py`。
`.claude/hooks/` のみの変更（hook 契約系 Issue）は除外（`## 振る舞い` 不要・契約テストで代替）。適用範囲外（config/・docs frontmatter 等）は [`testing-framework.md`](./testing-framework.md) 参照。
`tidd test-plan` が未生成を検知して exit 1 でブロックする。

詳細: docs/reference/pytest-bdd-workflow.md

---

## TDD/BDD ワークフロー（feat/fix 必須）

1. Issue の `## 振る舞い` Gherkin を読む
2. テストを書く（pytest / Jest）
3. テストが RED であることを確認する
4. 実装する
5. 作ったテストファイルのみ GREEN であることを確認する（全件実行不要）
6. PR を作成する

**commit 順序は `require-red-first.py` が機械強制する（PR 作成時にブロック）。**
分割不能な場合は PR ボディに `<!-- allow-single-commit: <理由> -->` で bypass できる。
詳細: docs/reference/hooks.md#require-red-firstpy

テストの公式ゲートは `tidd ai-review` 内の `tidd test-plan`。
CircleCI `nightly-tests` の詳細経緯は docs/reference/test-plan-guide.md 参照。

`tests/` 直下のファイルをコミットした後は `protect-tests.py` により変更不可になる。
テストを書いてコミットする前に内容を十分確認すること。

---

## Test plan チェックリストの記述ルール

**Scenario は Issue の `## 振る舞い` セクションに書く（PR ボディへのコピーは不要）。**
bats・Jest・pytest の項目は Test plan に書かなくてよい（ファイル変更検出で自動実行）。
`## Test plan` 外の checklist（やること消化状況等）は test-plan-check の対象外。

### Test plan 項目の種類

| 種別 | 書き方 | 動作 |
|------|--------|------|
| **AI確認** | `- [ ] [AI確認] workflow.md に記載が追加されていること` | APPROVE 後に Claude が検証して `- [x]` に更新 |
| **AI確認-post-merge** | `- [ ] [AI確認-post-merge] nightly-tests が GREEN になること` | auto-merge を妨げない。マージ後に cron が検証 |
| **手動** | `- [ ] [手動] ブラウザで確認する` | APPROVE 後に exit 4 で人間に委ねる |
| **未カバー**（禁止） | `- [ ] chezmoi status で差分が出ない` | `tidd test-plan` が exit 1 でブロック |

**テスト自動実行の仕組み:**

| 変更ファイル | 自動実行 | GitHub Commit Status |
|------------|---------|---------------------|
| `projects/gas/<project>/` | `npx jest` | `jest/<project>` |
| `projects/py/<project>/` | `pytest` | `pytest/<project>` |
| `docs/`・`.md` のみ | スキップ | 投稿なし |

GitHub Commit Status に ❌（failure/error）がある場合、`tidd ai-review` は APPROVE レビューを投稿するが自動マージを exit 4 でブロックする（Issue #831）。

**原則: AI がテスト可能なものは AI がテストする。人間はどうしても AI にできないものだけやる。**

---

## hook 動作検証（Issue #1294 Phase 1 以降）

**hook の動作検証は `pytest + stdin JSON pipe 契約テスト` で行う。**

```python
def _run_hook(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_hook_path())],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
```

新しい hook を作成するとき: `test_<hookname>_hook.py` に正常系・異常系の契約テストを書く。
旧パターン（廃止済み bash pipe）との比較は docs/reference/test-plan-guide.md 参照。

---

## バグ修正ルール

- バグ修正時は必ず `tests/regressions/test_fix_<N>.py` にバグ再現テストを追加してから実装する
- `tests/` 直下の既存テストファイルは変更不可（`protect-tests.py`）。`regressions/` はロック対象外

---

## 関連ドキュメント

- docs/reference/test-plan-guide.md — 詳細ガイド（Gherkin テンプレート・旧パターン比較・追加観点ルール）
- [`.claude/rules/testing-framework.md`](./testing-framework.md) — フレームワーク棲み分け
- docs/reference/pytest-bdd-workflow.md — pytest-bdd 詳細
- docs/reference/hooks.md — 全 hook リファレンス

# テストフレームワーク棲み分けルール

## フレームワーク選択表

GAS コード → **Jest**（`projects/gas/<project>/tests/`）、Python コード → **pytest**（`projects/py/<project>/tests/`）。
`tidd run-project-tests` 一元管理。bats 撤去済み（#1090）。

## feat/fix 振る舞いテストは `.feature`（+step_defs）または `tests/test_*.py` のどちらかが必須（#1962）

feat/fix は 2 層構造がデフォルト: 振る舞い層（`.feature` + step_defs）/ 実装詳細層（`test_*.py`）。
**判定基準:** 外部観測可能な振る舞い（端末出力・ファイル状態・exit code）→ `.feature`、内部ユーティリティ・境界値 → `tests/test_*.py`。
**hook 契約系**（`## やること` が `.claude/hooks/` のみ）は `.feature` 不要・`test_<hookname>_hook.py` のみ（#1855）。

`tidd extract-feature <N>` で `.feature` + `step_defs`（xfail pending）を生成する。

**CRITICAL: feat/fix PR は `.feature`（+step_defs）または `tests/test_*.py` のどちらかが必須（#1962）。**
`tidd test-plan` が未生成を exit 1 でブロック。**完了時に `xfail` を外すこと（xfail のままマージ禁止）。**

## CRITICAL: やってはいけないこと

- GAS に pytest / Python に Jest を使わない
- 間接テスト（フレームワークをラップするだけ）を置かない
- **外部観測可能な振る舞いを `test_*.py` だけで書く** — `.feature` + step_defs で記述すること（#1962）
- step_defs を `xfail` のままマージしない

### REQUIRED: pytest marker `target_<basename>`

新規 pytest ファイルに **必ず** `@pytest.mark.target_<basename>` を付ける（#785）。
例: `@pytest.mark.target_ai_review`。ハイフンはアンダースコアに変換。

## テストを書かなくてよいケース（Phase 1 縮小後・4 項目）

- `run_once_` / `run_always_` / `run_onchange_` スクリプト（副作用・モック困難）
- ドキュメントのみの変更（`docs/`・`*.md`）
- 環境構築スクリプトの副作用部分（ネットワーク依存）
- 外部サービス連携の認証・接続確認（`[手動]` 項目として扱う）

**適用範囲外（feat/fix でもテスト不要）:** `config/` 配下の CSS・YAML・JSON、`docs/` frontmatter のみの変更、`pyproject.toml` のマーカー追加など、`docs/reference/testing-guide.md` 「よくある誤適用パターン」を参照。

## テストファイル保護（`protect-tests.py`）

`protect-tests.py` が `*/tests/` への書き込みをブロック（#833）。
更新時は PR ボディに `<!-- allow-test-update: <理由> -->` を追加してバイパスする（理由必須）。

**詳細ガイド（実装例・protect-tests・判断フロー・Phase 2 計画）:** docs/reference/testing-guide.md

# Tool Calling 設計指針

> **注記:** 以下の機械強制（`ban-anthropic-import` 等）は `config.json` で対応 hook を enable した利用者にのみ適用される。`copier copy` 直後の consumer はすべて default OFF のため、規約記述は参照目的。

**CRITICAL（#1281）:** Anthropic SDK / `anthropic` の直接インポートを全廃。Tool Calling は Agent tool / subagent / skill 経由で実装する。`.claude/hooks/ban-anthropic-import.py` が機械強制でブロックする。

---

## hook vs subagent の使い分け

| 処理の性質 | 手段 |
|-----------|------|
| キーワード存在・パターンマッチ・ファイル存在確認 | hook（静的ルール） |
| 意味理解・文脈判断・複数ステップ推論 | Agent tool / subagent（動的判断） |

**典型例:** `require-issue.py`・`validate-issue.py` → hook。Pain 品質評価・Gherkin 検証 → subagent。

---

## プロンプトインジェクション防御（CRITICAL）

外部データ（Issue / PR 本文）を subagent に渡す場合:

- `tools:` を **`Read`・`Grep`・`Glob` のみ** に限定する（`Bash`・`Write`・`Edit` は禁止）
- 入力テキストから動的にコマンドを生成しない（tool 一覧は `.claude/agents/*.md` の frontmatter で固定）
- 本文は `tidd_tools.sanitize.sanitize_untrusted_text()` を通してから prompt に埋め込む（HTML コメント・不可視 Unicode・`alt=` 属性・HTML エンティティを除去。#1845）

---

## subagent 並列化

**並列化の 4 基準（すべてを満たすとき並列化する）:**

1. 各処理の入力が他の処理の出力に依存しない
2. 実行順が変わっても最終結果に影響しない
3. 複数 subagent の結果を集約して次に進むポイントが定義できる
4. 直列実行の合計時間 > 並列実行 + 集約時間

**並列呼び出し:** 同一メッセージ内に複数の Agent tool ブロックを配置すると並列実行される。

**並列禁止 5 パターン:** PR 作成前の quality check / verdict 集約 / merge→next issue / hook→tool 実行 / 同一ファイル write 連鎖

---

**詳細（agy 役割分担・structured output・テンプレート・効果測定）:** docs/reference/subagent-design-guide.md

# ワークフロー規約

> **注記:** 機械強制 hook は `config.json` で enable した利用者のみ適用（`copier copy` 直後は default OFF）。

**詳細（exit code 対応・環境初期化・関連 Issue）:** `docs/reference/workflow-guide.md`

## 実装前：既存ソリューションを探す

1. Technology Radar 2. `docs/research/` 3. `anthropics/claude-plugins-official` 4. コミュニティリポジトリ 5. GitHub 検索（詳細: `docs/research/technology-radar-guide.md`）

**ライブラリ採用時:** 新規パッケージ採用時は `.claude/rules/dependency-allowlist.yaml` へ追加する PR を同梱する（`check-dependency-allowlist.py` hook が未登録をブロック・#2561）。

---

## GitHub 操作の使い分け

Claude Code セッション内: **`mcp__github__*`** / tidd_tools・CI・cron: `gh`（詳細: `docs/research/github-mcp-server.md`）

### consumer が持つ 2 種類の PAT

consumer（copier 配布先）は操作対象で **2 種類の PAT** を使い分ける（既定トークンは上流に届かない）:
- consumer 自身のリポジトリ → consumer 自身の PAT（既定トークン）
- 上流リポジトリ → 上流用 PAT

上流用 PAT は値を読まずに使う: クローン先で `mise exec -- gh ...`。変数名は `mise env --json | jq 'keys'` で **キーのみ**。

---

## TiDD ワークフロー

**NO TICKET NO WORK。Issue なしのコミットは禁止。**

### 着手前

1. `gh issue view <N>` → `git fetch origin`
2. `git worktree add -b <type>/issue-N-slug ../<repo>-issue-N-slug origin/main`（末尾 `origin/main` 必須）
3. **`.venv` 初期化は `SessionStart` hook が自動実行する**（#2596・無効時の手動コマンドは guide 参照）
4. worktree ディレクトリで作業（`git checkout -b` 等は hook がブロック）

### 実装中

- 調査・判断の経緯は Issue コメントに残す。コミットに `closes #N` を含める
- 別問題を発見したら即 Issue 起票（非対話的に `gh issue create`・機密情報は含めない）
- **エラーに遭遇したら無視せず原因を調査する** — 一時的か恒常的かを判断し、「散発するから仕方ない」で片付けない。恒常的なエラーは `gh issue list` で重複確認の上、再発防止 Issue を起票する
- **脆弱性を発見したら Issue 化せずユーザーに直接相談する**
- **`## 期待する出力例` がある場合**: 内容を改変せず初回スナップショット/テスト期待値として固定する（未記載時は従来フロー）
- **config.json に `impl-delegation: true` かつ `impl-backend` が設定されている場合**: RED/GREEN 各ステップ + refactor/docs 編集ステップで `tidd propose-step` を使いコード提案を外部 backend（agy/codex/custom）へ委譲できる（`impl-delegation` 無効時またはいずれか未設定時は従来どおり自分で実装。詳細: `docs/reference/propose-step-guide.md`・#3118・#3131・#3132）

### 完了時

1. `gh pr create` 前に `tidd pre-flight` を実行し GREEN（exit 0）を確認する
   - src/hooks 変更時は docs 更新必須（不要なら `<!-- no-doc-update: <理由> -->`）
2. PR 本文に `closes #N` 必須。タイトル: `<type>(<scope>): #N 説明`（hook 強制）
3. **同期（前景）実行必須** — `run_in_background`/`nohup`/`&`/`ScheduleWakeup` 禁止:
   `tidd ai-review <PR> 1`
4. exit code ごとの対応・マージ後クリーンアップ（#3398: `sweep-merged-branches` hook が自動実行）・verify-post-merge 登録: `docs/reference/workflow-guide.md` 参照

---

## その他の制約 / PR 分割

- `.sh` / `.bats` 新規作成禁止（#1090）— hook がブロック
- テスト: `testing-framework.md`・`test-plan-checklist.md`
- **1 Issue 1 PR。** 500 行超: 分割検討。1000 行超: 必ず分割（`tidd pre-flight` が機械強制・#3081）。詳細: `docs/reference/pr-splitting-guide.md`

---

## セッション運用

- **1 Issue 1 セッション** — マージ後は `/clear` して次 Issue へ
- **ai-review 前に会話を軽く保つ**（TTL 5 分超過で全会話非キャッシュ再読）
- **subagent prompt は自己完結**
- **`issue-next` 連続自走は `/loop` へ**（詳細: `docs/reference/issue-next-loop-operations.md`）
