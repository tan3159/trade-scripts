# hooks 有効化の記録

Issue #5 で適用した hook 有効化の記録。copier copy 直後のデフォルトは安全系 3 hook
（`require-issue` / `block-dangerous-git` / `ban-claude-p`）のみ有効だが、本リポジトリは
単独運用のため配布元相当まで有効化した。

設定ファイルは **ユーザーグローバル** の `~/.config/tidd_tools/hooks-config.json`
（リポジトリには置かない）。同一マシンの他リポジトリのセッションにも効く点に注意。

## 有効化済み hook（2026-07-17）

### CLI 管理（`tidd hooks-config show` で確認可能・全 20 個 on）

`require-issue` / `require-main-sync` / `protect-tests` / `require-red-first` /
`block-dangerous-git` / `ban-shell-files` / `ban-anthropic-import` / `ban-claude-p` /
`ban-hardcoded-repo` / `ban-parents-n` / `validate-issue` / `require-yaru-consistency` /
`detect-ai-confirm-misuse` / `label-pr` / `auto-ruff-format` / `session-start-cache` /
`on-stop` / `analyze-loop-on-stop` / `yaru-auto-tick` / `ai-review-timing`

### CLI 非管理（hooks-config.json に手動追記で on・5 個）

| hook | 有効化理由 | 現状の発動可否 |
|------|-----------|--------------|
| `require-merge-ci-status` | CI ❌ 状態でのマージ防止（CI 導入 Issue #8 の前提） | 発動する（settings.json 登録済み） |
| `notify-copier-staleness` | 上流テンプレート追従の staleness 通知（copier update 運用の前提） | 発動する（同上） |
| `require-issue-id-in-pr-title` | PR タイトル規約 `<type>(<scope>): #N` の機械強制 | **未発動**（下記注意参照） |
| `require-mypy` | Python 型チェックゲート（projects/py 雛形 Issue #6 の前提） | **未発動**（同） |
| `require-ruff-format` | Python フォーマットゲート（同上） | **未発動**（同） |

**注意（既知の上流問題）:** `require-issue-id-in-pr-title` / `require-mypy` / `require-ruff-format` は
hook スクリプト自体は配布済みだが、配布された `.claude/settings.json` に起動登録が無いため
**現状 Claude Code セッションでは発動しない**（上流 ai-dev-handbook#2229 で登録追加を依頼済み）。
config 上は on にしてあるので、上流修正が copier update で反映され次第そのまま有効になる。
`.claude/settings.json` は copier 管理ファイルのため直接編集はしない。

### off のまま（3 個・理由つき）

| hook | 理由 |
|------|------|
| `detect-rule-bloat` | `.claude/rules/` は copier 配布物で consumer は直接編集しない運用のため対象外 |
| `notify-template-sync` | `templates/` ディレクトリを持つ配布元専用の hook。本リポジトリに `templates/` は無い |
| `validate-skill` | スクリプト `validate-skill.py` が上流 ai-dev-handbook#2221（ban-hardcoded-repo 自己検知）のためコミット不能でリポジトリに含まれていない。settings.json には登録があるため fresh clone では要注意（#2221 にコメント済み） |

## 動作確認（2026-07-17 実施）

hook 検証の公式手段である stdin JSON pipe 契約テスト形式で確認:

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"gh issue create --title x --body y"}}' \
  | python3 .claude/hooks/validate-issue.py; echo $?
```

- `validate-issue`: 必須セクション欠落の `gh issue create` → **exit 2 でブロック**（stderr に不備一覧）
- `require-issue-id-in-pr-title`: Issue ID なしタイトルの `gh pr create` → **exit 2 でブロック**
  （スクリプト単体の動作確認。前述のとおり settings.json 未登録のためセッション内では未発動）
- 無関係コマンド（`ls` 等）→ exit 0 で素通り
- 実運用でも `validate-issue` / `require-issue` / `ban-hardcoded-repo` / `block-dangerous-git` の
  ブロックが Issue #1〜#3 の作業中に実際に発動している

## 再現手順

```bash
tidd hooks-config init --all-on   # CLI 管理分
# CLI 非管理分は ~/.config/tidd_tools/hooks-config.json に "hook名": true を追記
tidd hooks-config show            # 確認
```
