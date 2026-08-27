# subagent 委譲: 機械検証・park 処理（Issue #2452）

> **実行環境（ツール名の読み替え）:** 本スキルのツール名参照は Claude Code 前提で記載している。Codex（`.agents/skills` symlink 経由）で実行する場合は、`Agent(subagent_type="x", ...)` → `spawn_agent(agent_type="x", task_name="x", message=...)` に読み替える（`task_name` のみでは default ロールの agent が起動し `.claude/agents/*.md` 相当のツール制約・output_format 契約が適用されない・Issue #3491。対応表・実測記録: ai-dev-handbook 本体の docs/reference/ 配下・`codex-interop.md`（consumer 未配布）「6-4. spawn_agent の `agent_type` 未指定時は default ロールが起動する」）。`Edit` / `Write` → `apply_patch` に読み替える。GitHub 操作は Claude Code・Codex いずれも `gh` CLI を使う（`mcp__github__*` は廃止済み・Issue #3773）。

`/issue-next` の STEP 2（issue-implementer 委譲）・STEP 5 リトライループ（issue-fixer 委譲）で
共通する完了報告の機械検証手順と park 処理を定義する。

## 目次

- [Codex: wait_agent タイムアウト時の注意（Issue #3436）](#codex-wait_agent-タイムアウト時の注意issue-3436)
- [STEP 2: issue-implementer 完了報告の機械検証](#step-2-issue-implementer-完了報告の機械検証)
- [報告なしで委譲が終わった場合の作業回収（Issue #2668）](#報告なしで委譲が終わった場合の作業回収issue-2668)
- [STEP 5: issue-fixer 完了報告の機械検証](#step-5-issue-fixer-完了報告の機械検証)
- [skip 時の処理（ファイル競合検出・Issue #2452）](#skip-時の処理ファイル競合検出issue-2452)
- [diff-size gate park の追加検証（Issue #3992）](#diff-size-gate-park-の追加検証issue-3992)
- [park 時の処理](#park-時の処理)

---

## Codex: wait_agent タイムアウト時の注意（Issue #3436）

Codex で issue-implementer / issue-fixer を `spawn_agent` で起動した場合、完了は `wait_agent` で
待つ。**タイムアウトした場合、自分自身の canonical task name（例: `issue_next` / `issue_next_all`）
へ `followup_task` を送ってはならない。** 自分自身宛の followup は自分自身への応答待ちに帰着し、
進捗が完全停止する自己待ちデッドロックに陥る（#3436 で `/issue-next-all` → `/issue-next` →
issue-implementer の多階層委譲時に実際に約1時間発生した）。

タイムアウト時は以下の順で対応する:

1. `list_agents` で issue-implementer / issue-fixer の生存を確認する
2. 生存していれば `wait_agent` を再試行する（完了報告を待つのは自身の責務であり、上位へは委譲しない）
3. 応答不能・エージェント消失が確認できた場合は、人間の割り込みを待たず自身の責務として
   「報告なしで委譲が終わった場合の作業回収」（下記）へ進む

---

## STEP 2: issue-implementer 完了報告の機械検証

正常終了報告（`PR: #<N>` / `branch: <name>` / `worktree: <path>`）を受けたら、報告された PR 番号を
以下 3 点で検証する。**いずれか 1 つでも不一致なら park 扱い**にする:

1. **PR 実在・OPEN:** `gh pr view <報告PR番号> --json state,body,commits` が成功し、
   かつ `state` が `OPEN` であること（closed/merged 番号の誤報告を弾く）
2. **closes 記載:** 返り値の `body` または commits の中に `closes #<N>` が含まれること
3. **TDD 未実施の疑い:** `uv run --project projects/py/tidd_tools tidd tdd-check <報告PR番号>` を実行し exit code を見る。exit 0 なら疑いなし、
   exit 1 なら疑いあり（`require-red-first.py` と同一ロジックを `.claude/hooks/_lib/tdd_order_check.py`
   から共有・#2895）。exit 2（PR/git 取得失敗）は機械検証の実行エラーとして park 扱いにする

**契約違反判定（#2542）:** 検証 1 で `state` が `MERGED` の場合は誤報告ではなく **subagent の契約逸脱**
（責務は `gh pr create` で終端・agent 定義の禁止事項違反）として扱う。以下の機械検証を実行する:

```bash
state=$(gh pr view <報告PR番号> --json state -q .state)
if [ "$state" = "MERGED" ]; then
  echo "契約違反: subagent 報告時点で PR #<報告PR番号> が MERGED です（責務は gh pr create で終端・#2542）" >&2
fi
```

exit 1（契約違反）の場合は park 処理に加えて:

- Issue に「issue-implementer が契約を逸脱して PR #X をマージした（#2542）」とコメント記録する
- **マージ済みコードを親セッションが直接検証する**（`gh pr diff <PR番号>` を確認し、変更ファイルに
  `tidd_tools/ai_review/` が含まれる parser critical PR なら
  `parser-critical-pr.md` に従い事後の異バックエンド合議レビューを実施する。欠陥が見つかれば Issue 起票）

**この 1 回のチェックだけでは、報告受領後の STEP 3〜5 の処理中に発生する逸脱（追加コミット・
PR本文の書き換え・park-and-continue の無断実行等）を検知できない（Issue #4029 の実インシデント）。**
そのため検証をすべて満たしたら、STEP 3 へ進む前に drift チェックの baseline を記録する（#4038）:

```bash
uv run --project projects/py/tidd_tools tidd issue-next-state observe-pr <N> <報告PR番号>
```

以降、`gh pr edit`/`gh pr merge`/`gh pr close`（自分自身が実行する場合・subagent が実行しようと
した場合の両方）は `require-pr-state-drift-check.py`（PreToolUse hook・default OFF）が、この
baseline と実行直前の実際の PR 状態（`state`/`headRefOid`/`updatedAt`）を照合する。不一致
（drift）があれば該当コマンドは exit 2 でブロックされる。詳細:
`docs/reference/action-log-and-drift-check.md`。

検証をすべて満たしたら STEP 3 へ進む。

---

## 報告なしで委譲が終わった場合の作業回収（Issue #2668・回収対象の Issue 限定 #2705）

issue-implementer が **正常終了・park・skip のいずれの報告も返さずに終わった場合**（人間の割り込み・タイムアウト・クラッシュ等）は、以下の手順で未 push のコミット済み作業を検出し回収する。

**CRITICAL（#2705）:** 回収対象は **委譲した Issue 番号 `<N>` に対応する worktree のみ**に限定する。
このリポジトリでは `/issue-next` を複数ターミナルで意図的に並行稼働させる運用が常態化しており、
`git worktree list` を無条件に全走査すると **他ターミナルが作業中の別 Issue の worktree** を誤って
検出し、push + PR 作成まで実行してしまう（他ターミナルの進行中フローへの割り込み）。

### 1. 回収対象 worktree を検出し、他セッションが作業中でないか確認する（liveness チェック統合・#2705）

回収対象の条件は「ブランチ名が `*/issue-<N>-*` に一致し、origin/main より進んだコミットがあり、かつ
リモートに push されていない」worktree である。push 済みのブランチはすでに STEP 2 の機械検証対象
（PR 作成済みのはず）なので回収不要。他 Issue の worktree は対象外のため検出対象にも push 対象にもならない。

**CRITICAL（#2705）:** 該当 worktree が見つかっても、**liveness チェックを通過するまで**
`"回収対象"` の行は出力しない。他セッションが同じ Issue `<N>` を作業中の場合は「回収対象のように
見える worktree」を誤って掴んでいるだけなので、先に liveness を確認してから出力内容を分岐する。

```bash
# 委譲した Issue <N> のブランチのみを対象に走査する（他 Issue の worktree は対象外・#2705）
git fetch origin
git worktree list --porcelain | grep "^worktree " | awk '{print $2}' | while read wt; do
  branch=$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null) || continue
  case "$branch" in
    */issue-<N>-*) ;;
    *) continue ;;  # 委譲した Issue 番号のブランチと一致しない worktree はスキップ
  esac
  # origin/main より進んだコミット数
  ahead=$(git -C "$wt" rev-list --count origin/main..HEAD 2>/dev/null) || continue
  [ "$ahead" -eq 0 ] && continue
  # リモートブランチが存在する場合は push 済みとみなし回収対象から除外する
  if git -C "$wt" rev-parse --verify "origin/$branch" >/dev/null 2>&1; then
    unpushed=$(git -C "$wt" rev-list --count "origin/$branch..HEAD" 2>/dev/null) || unpushed=0
    [ "$unpushed" -eq 0 ] && continue  # push 済み = 回収不要
  fi
  # liveness チェックを通過してから "回収対象" を出力する（#2705）
  if uv run --project projects/py/tidd_tools tidd issue-next-state check-liveness <N>; then
    echo "回収対象: $wt (branch: $branch, commits ahead of origin/main: $ahead)"
  else
    echo "他セッションが作業中のため回収を見送る: $wt (branch: $branch)"
  fi
done
```

3 パターンの出力に応じて分岐する:

- **`"回収対象"` の行が出力された（liveness チェック exit 0・TTL 超過・作業中でない）:**
  worktree を削除せず手順 2（pre-flight）に進む
- **`"他セッションが作業中のため回収を見送る"` の行が出力された（liveness チェック exit 1・TTL 内・
  他セッションが作業中）:** 回収せず何もしないで終了する。worktree は削除せず、
  push・PR 作成・park ラベル付与のいずれも実行しない（下記「コミットなし時の park 処理」は適用しない）
- **どちらの行も出力されなかった（Issue `<N>` に対応する worktree が存在しない、コミットが 0 件、
  または全て push 済み）:** 「回収対象のコミットなし」を stdout に出力し park 扱いにする（下記
  「コミットなし時の park 処理」参照）。他 Issue の worktree（例: 検出条件に一致しない `*/issue-200-*`
  等）に対しては push・PR 作成のいずれも実行しない

### 2. pre-flight を実行する

```bash
cd <対象 worktree>
uv run --project projects/py/tidd_tools tidd pre-flight
```

- **exit 0（GREEN）:** 手順 3 へ進む
- **exit 1（RED）:** pre-flight が失敗しているため park 扱いにする（下記「pre-flight 失敗時の park 処理」参照）

### 3. push して PR を作成する

```bash
cd <対象 worktree>
git push origin <branch>
```
```bash
gh pr create \
  --title "<type>(<scope>): #<N> <タイトル>" \
  --body "closes #<N>\n\n## 背景\nこのPRはissue-implementer subagentが報告なしで終了したため、コミット済み作業を親セッションが回収して作成した（Issue #2668）。" \
  --head "<branch>" --base main
```

PR が作成できたら正常終了報告（`PR: #<PR番号>` / `branch: <branch>` / `worktree: <worktree パス>`）として扱い、STEP 2 の機械検証（PR 実在・OPEN / closes 記載 / TDD 未実施疑い）を実行する。検証を満たしたら STEP 3 へ進む。

### コミットなし時の park 処理

Issue `<N>` に対応する worktree が存在しない場合、またはコミットが 0 件（もしくは全て push 済み）の
場合は、park 扱いとする:

1. 「回収対象のコミットなし」を stdout に出力する
2. Issue `<N>` に `🙋 needs-human-input` ラベルを付与する
3. Issue `<N>` に以下のコメントを投稿する:
   ```
   issue-implementer subagent が報告なしで終了しました。worktree に未 push のコミットも存在しなかったため、park 扱いとします（Issue #2668）。
   手動で再着手するか `/issue-next <N>` を再実行してください。
   ```
4. 通常の「park 時の処理」セクションに従い、引数なし・単一番号・バッチモード別の処理を実行する

### pre-flight 失敗時の park 処理

pre-flight が exit 1 の場合は、worktree を削除せずに park 扱いとする:

1. Issue `<N>` に `🙋 needs-human-input` ラベルを付与する
2. Issue `<N>` に以下のコメントを投稿する:
   ```
   issue-implementer subagent が報告なしで終了しました。コミット済み作業（<commit SHA>）を回収しましたが、pre-flight が失敗したため PR を作成できませんでした（Issue #2668）。
   worktree: <worktree パス>
   branch: <branch 名>
   手動で pre-flight を確認して修正後、push・PR 作成してください。
   ```
3. 通常の「park 時の処理」セクションに従い、引数なし・単一番号・バッチモード別の処理を実行する

---

## skip 時の処理（ファイル競合検出・Issue #2452）

issue-implementer が `skip: <理由>` / `issue: #<N>` を報告した場合（他 OPEN PR とのファイル競合・並行 PR
上限検出。旧 SKILL.md STEP 3 の `uv run --project projects/py/tidd_tools tidd check-pr-conflicts` 相当）は park と異なり人間判断不要のため、
機械検証もラベル付与も行わない（subagent が Issue コメント投稿・worktree 削除まで完了済み）。

**CRITICAL: skip 後の state クリーンアップは親セッションの責務（Issue #3449）。** subagent は worktree 削除・Issue コメント投稿までしか完了しておらず、
STEP 1 で `issue-next-state init <N>` が作成した `cache/issue-next-state/issue-<N>.json` と連動する `🔧 in-progress` ラベル（#2804）は残り続ける。
残留すると `next-unattended` の選定ロジックが `🔧 in-progress` 付き Issue を恒久的に除外し続け（`check-in-progress-label` も exit 1 を返し続ける・
`--exclude` はセッション内メモリのみで永続化されない）、skip した Issue が以後選定されなくなる。親セッションは skip 報告を受けたら、委譲した Issue #N の
state クリーンアップ（`issue-next-state clear <N>` による state ファイル削除 + `🔧 in-progress` ラベル除去）を **引数なし・単一番号指定・バッチモードの
いずれの分岐でも**実行する:

- **引数なし** → `issue-next-state clear <N>`（state ファイル削除 + `🔧 in-progress` ラベル除去）を実行してから「Issue #N はファイル競合のためスキップしました（<理由>）」と記録し STEP 1 に戻る
- **単一番号指定** → `issue-next-state clear <N>`（state ファイル削除 + `🔧 in-progress` ラベル除去）を実行してから「Issue #N はファイル競合のためスキップしました（<理由>）」と報告して終了する
- **バッチモード** → `issue-next-state clear <N>`（skip した Issue #N の `🔧 in-progress` ラベル除去）を実行してから、上記を記録し `issue-next-state consume <anchor>` で次を取り出し STEP 2 へ進む（batch の state ファイルは anchor 単位 `issue-<anchor>.json` で管理されるため、anchor 自身が skip した場合は `clear <N>` でキューごと消えてしまう。その場合は `clear` せず `consume <anchor>` のラベル移動（#2817）に委ね、anchor の state ファイルはバッチ完了時（SKILL.md STEP 1 の item 5）に `issue-next-state clear <anchor>` で削除する）

---

## STEP 5: issue-fixer 完了報告の機械検証

正常終了報告（`pushed: <SHA>`）を受けたら、まず PR が MERGED になっていないことを確認する
（**契約違反判定・#2542**: issue-fixer の責務は push で終端。報告受領時点で PR が MERGED なら
STEP 2 と同じ契約違反として扱い、マージ済みコードを親セッションが直接検証する）:

```bash
state=$(gh pr view <PR番号> --json state -q .state)
if [ "$state" = "MERGED" ]; then
  echo "契約違反: subagent 報告時点で PR #<PR番号> が MERGED です（責務は push で終端・#2542）" >&2
fi
```

MERGED でなければ drift チェックの baseline を更新する（#4038。詳細は STEP 2 と同じ
`docs/reference/action-log-and-drift-check.md` を参照）:

```bash
uv run --project projects/py/tidd_tools tidd issue-next-state observe-pr <N> <PR番号>
```

次に push された SHA を検証する:

```bash
git fetch origin <branch>
git rev-parse origin/<branch>
```

報告された SHA と一致しない場合は park 扱いにする。一致したら親セッション側の worktree を最新化する:

```bash
cd <worktree パス> && git pull origin <branch> --ff-only
```

検証できたら STEP 5 のリトライループへ進む（再実行前の新コミット確認は不要・#3636 で gate 化済み）。

---

## diff-size gate park の追加検証（Issue #3992・#3994）

issue-implementer の park 報告理由に「diff-size」（`uv run --project projects/py/tidd_tools tidd pre-flight` の 1000 行超ゲート・#3081）が
含まれる場合、下記「park 時の処理」の通常フローに入る前に、オーケストレータ自身が分割可能性を
独立に検証する。issue-implementer は `allow-xxl` / `allow-large-pr` マーカーを自己判断で付与
することを禁止されており（`.claude/agents/issue-implementer.md`・`block-subagent-size-marker.py`
hook が機械ブロック）、park コメントに書かれた「分割不能」の理由をそのまま採用しない
（実例: #3992 で「4 シナリオが相互依存し分割不能」と自己申告されたが、実際は関数単位で
きれいに分離できた）。マーカーの置き場所は PR ボディに統一されている（Issue #3994）。

### 検証手順

1. 対象 worktree で `git diff origin/main...HEAD --stat` を実行し、変更ファイルの境界を洗い出す
2. Issue の `## やること` / `## 振る舞い`（Gherkin Scenario）を読み、ai-dev-handbook 本体の docs/reference/ 配下・`pr-splitting-guide.md`（consumer 未配布）の
   「分割してはいけないケース（例外リスト）」6 項目のいずれかに該当するかを **オーケストレータ自身が**
   diff・Scenario の内容から判定する（issue-implementer の park コメントの理由をそのまま信用しない）
3. **6 項目のいずれにも明確に該当しない、または判定できない** → 「分割可能（または判定不能）」として
   扱い、下記「park 時の処理」の通常フロー（human へのエスカレーション）へ進む。オーケストレータ自身は
   PR の再設計・分割実装を行わない
4. **6 項目のいずれかに明確に該当する** → 「分割不能」と判定し、下記の手順でマーカーを追加して続行する

### 分割不能と判定した場合の続行手順

issue-implementer は RED→GREEN のコミットを完了済み（diff-size は commit 後の pre-flight で
検出されるため）。マーカーの置き場所は PR ボディに統一されており（Issue #3994）、
`uv run --project projects/py/tidd_tools tidd pre-flight` はもうコミットメッセージのマーカーを受理しない。PR がまだ存在せず
PR ボディにマーカーを書けないため、オーケストレータ自身が worktree に入り、緊急スキップ
環境変数で一時的に pre-flight を通過させる:

```bash
cd <worktree>
PRE_FLIGHT_SKIP_DIFF_SIZE=1 uv run --project projects/py/tidd_tools tidd pre-flight   # exit 0 を確認
```

pre-flight が GREEN になったら、issue-implementer.md の手順 9〜11（evidence-based tick・
Test plan 自己検証・`gh pr create`）をオーケストレータ自身が実行する。`gh pr create --body` に
`<!-- allow-xxl: <検証した具体的理由（例外リストのどの項目に該当するか）> -->` を含めること
（PR ボディが pre-flight の再実行時と ai-review の size/XXL gate・`size_gate.py` の両方が
参照する単一の置き場所）。`block-subagent-size-marker.py` hook は `agent_type` が
`issue-next` / `issue-next-all` の `gh pr create` 呼び出しを許可リスト対象としているため、
このマーカー付き PR 作成はブロックされない。Issue に付与済みの `🙋 needs-human-input`
ラベルは除去し、検証内容（該当した例外リスト項目・判断根拠）を Issue コメントに記録する。

---

## park 時の処理

STEP 2・STEP 5 いずれの park 報告（機械検証不一致・または subagent 自身の park 報告）も同じ処理で扱う:

- Issue に `🙋 needs-human-input` ラベルを付与する（subagent 自身の park 報告時は付与済みのため不要）
- 機械検証不一致の場合のみ Issue に理由コメントを投稿する（例:「issue-implementer/issue-fixer の完了報告
  （PR #X）が機械検証で確認できませんでした: <理由>」）。subagent 自身の park 報告はコメント済みのため
  二重投稿しない
- **引数なし** → 「Issue #N は park しました（<理由>）」と記録し STEP 1 に戻る
- **単一番号指定・バッチモード** → 選択肢形式で報告して停止する:

```
Issue #N の処理が park 状態です（<理由>）。

A. Issue/PR コメントを人間が確認してから再着手する — 詳細はコメントに記載済み（推奨）
B. Issue #N をスキップして次の Issue に着手する — 今すぐ着手を続けたい場合

判断できなければ → A（Issue #N には needs-human-input ラベルを付与済み）
```

バッチモードの場合は「残りのキュー [#M1, #M2, ...] は処理されませんでした」を追記する。

**`is-unattended <N>` が exit 0 のとき（#3633）:** 上記の停止手順は使わず `unattended-park-and-continue.md`「STEP2/STEP5: issue-implementer / issue-fixer の park 報告時の拡張（ブロッキングバグ自動起票）」を実行する（対象 Issue スコープ外のブロッキングバグと判断できる場合は fix Issue を先に処理してから復帰、それ以外は park-and-continue で次 Issue へ継続する）。
