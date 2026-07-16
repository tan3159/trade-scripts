# dotfiles（chezmoi）の trade-scripts 展開検討

Issue #10 の調査結果。`~/repos/dotfiles`（`being-gaia-plan/s-tanaka-dotfiles`、chezmoi ソース）の資産が
trade-scripts の TiDD ループでそのまま使えるかを検証した。

## 結論（採用構成）

**dotfiles はマシンレベル資産としてそのまま共用する。trade-scripts 専用の chezmoi 展開は行わない。**

- chezmoi はユーザースコープ（`~/.bashrc`・`~/.claude/` 等）に適用されるため、同一マシンで動く
  trade-scripts にも既に効いている。実際に PR #11〜#19 の TiDD ループ（tidd ai-review → agy →
  auto-merge）は現行 dotfiles 環境で完走済み
- repo 固有ハードコード（後述）は dotfiles 側の問題として dotfiles リポジトリに Issue 起票して解消する
- trade-scripts 側で必要な差分は「リポジトリ local の git config」のみ（导入済み、後述）

## trade-scripts の TiDD ループに必要な環境変数・設定

| 項目 | 供給元 | 状態 |
|------|--------|------|
| `tidd` CLI | `uv tool install "tidd-tools @ git+...ai-dev-handbook@<tag>#subdirectory=projects/py/tidd_tools"` | 済（`docs/setup/tidd-tools-setup.md`） |
| `GH_TOKEN`（write 操作） | `gh auth token -u tan3159` を実行時に付与 | 済（永続 export しない） |
| `agy`（レビューバックエンド） | dotfiles: mise（`antigravity-cli`）+ `run_always_setup_agy_shim.sh` | 済（マシン共通） |
| `GITHUB_MCP_PAT` | dotfiles: `_load_ai_review_secrets()` が bw から取得 | 済（ただし後述の suffix 問題あり） |
| `BW_SESSION` | `bw unlock --raw`（手動） | マシン共通の前提 |
| codex | `~/.local/bin/codex` は stub のため `AI_REVIEW_CODEX_ENABLED=0` | 済 |
| git author | repo local `user.email`（noreply） | 済（本調査で設定） |

## repo 固有ハードコードの洗い出し

| 箇所 | 内容 | trade-scripts への影響 |
|------|------|------------------------|
| `dot_gitconfig.tmpl:6` | `email = s-tanaka@beingcorp.co.jp`（global） | **public リポジトリのコミット author に社用メールが載る**（実際に init コミットで発生） |
| `dot_bashrc.tmpl:197-221` | bw アイテム名 suffix を CWD の `git config user.email` の local-part から導出 | trade-scripts 内で実行すると suffix が `84367286+tan3159` になり bw アイテム解決が破綻 |
| `dot_bashrc.tmpl:154` | `claude_()` が `~/repos/ai-dev-handbook/scripts/bw-session-check.sh` を絶対参照 | handbook clone が無いマシンでは動かない（本マシンでは実害なし） |
| `dot_claude/settings.json.tmpl:34,44` | hook が `~/repos/mn-scripts/scripts/*.sh` を絶対参照 | mn-scripts が無いマシン/コンテキストで hook が失敗する（本マシンでは実害なし） |

## trade-scripts 側で実施した対策

public リポジトリへの社用メール漏えい防止として、repo local に noreply メールを設定した:

```bash
git config user.email "84367286+tan3159@users.noreply.github.com"
```

- worktree は repo config を共有するため、`git worktree add` で作る作業ツリーにも自動で効く
- squash merge のコミットは GitHub が PR author の identity で作るため従来から noreply だった。
  漏えいは local 直接コミット（init コミット）のみ

## 却下案

| 案 | 却下理由 |
|----|---------|
| trade-scripts 専用に dotfiles を fork/分岐する | 二重管理になり chezmoi の一元管理の利点を失う。差分は git config 1 行で足りる |
| copier テンプレートで bashrc/認証設定を配布する | スコープ違い（copier は repo スコープ、bashrc はユーザースコープ）。repo に認証設定を寄せると public 漏えいリスクが増える |
| trade-scripts 用の bw アイテム・PAT を別系統で新設する | agy/tidd はマシン共通で動作済み。系統を増やすと bw アイテムとローテーション対象が倍になる |

## 起票した Issue（dotfiles リポジトリ）

- being-gaia-plan/s-tanaka-dotfiles#35 — bw アイテム名 suffix の CWD 依存解消（`git config --global` 参照）
- being-gaia-plan/s-tanaka-dotfiles#36 — global git email を noreply 既定 + `includeIf "gitdir:"` 限定上書きに変更（priority: high）

ai-dev-handbook 側に必要な変更は今回の調査では見つからなかった（agy/tidd はマシン共通構成で trade-scripts の TiDD ループを完走済み）。
