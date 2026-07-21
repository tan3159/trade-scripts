# スマートガードレール（早期エスカレーション・STEP 5 詳細）

`/issue-next` の STEP 5 詳細フロー。`tidd_tools ai-review` が自動判定する早期エスカレーション条件と、
exit code 2 / 3 の区別を扱う。SKILL.md 本文の行数制限（Progressive Disclosure）のため分離（#2312）。

## スマートガードレール判定条件

`tidd_tools ai-review` が自動判定する条件:

| 条件 | 動作 |
|------|------|
| blocking 指摘（CRITICAL/HIGH）がゼロ | リトライせず APPROVE（[MEDIUM]/[LOW]/[DEFERRED] のみも同様、終了コード 0） |
| `[DEFERRED]` の指摘 | Issue を自動作成しない。そのまま APPROVE（終了コード 0） |
| 同じ指摘が2回連続 | エスカレーション（終了コード 2） |
| `[NEEDS_HUMAN_REVIEW]` タグを検知 | エスカレーション（終了コード 2） |

**CRITICAL: MEDIUM 指摘は PR 内で修正しない。** APPROVE を受け取ったら追加コミットせずマージフローに進む。対応が必要なら別 Issue として起票する。

## CRITICAL: exit code 2 と exit code 3 の区別（誤判定防止）

exit 2 = スマートガードレール発動（レビュー判断として人間に委ねる）→ Claude フォールバック禁止
exit 3 = 全バックエンド利用不可 → Claude フォールバック起動

**`tidd_tools ai-review` の終了コードの数値そのもの**（2 か 3 か）で分岐すること。

| ケース | 終了コード | issue-next の動作 |
|------|------|------|
| agy クォータ超過・codex が REQUEST_CHANGES を3回 | exit 2 | 人間にエスカレーション（Claude Agent 起動禁止） |
| agy クォータ超過・codex も同じ指摘を2回連続 | exit 2 | 人間にエスカレーション（Claude Agent 起動禁止） |
| agy クォータ超過・codex コマンド未インストール | exit 3 | Claude Agent フォールバック起動 |

**誤判定実例（PR #870）:** codex が REQUEST_CHANGES を3回返したにもかかわらず「agy クォータ超過 + 試行上限超過 = exit 3 と等価」と誤認して Claude Agent フォールバックを起動し、Claude subagent が APPROVE してしまったケース。
