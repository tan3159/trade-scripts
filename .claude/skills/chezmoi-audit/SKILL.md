# chezmoi-audit

chezmoi 管理ファイルの棚卸しを AI と行うスキル。
未管理ファイル候補と apt パッケージの書き漏れを検出し、各項目について「追加すべきか除外すべきか」を Claude が提示する。

**前提:** `.chezmoiignore` が整備済みであること（Issue #461）。整備されていない場合は先に `/chezmoi-audit` ではなく `.chezmoiignore` の整備を行うこと。

## 動作概要

1. `tidd chezmoi-audit` を実行して未管理候補一覧を取得する
2. 各未管理ファイルについて「`chezmoi add` すべきか・`.chezmoiignore` に追記すべきか」を Claude が判断してユーザーに提示する
3. ユーザーの指示に従って `chezmoi add <file>` または `.chezmoiignore` への追記を実行する
4. apt パッケージについても同様に「`run_once_install_packages.sh` に追記すべきか」を提示する

## 実行手順

### STEP 1: 棚卸しスクリプトの実行

```bash
tidd chezmoi-audit
```

または個別に:

```bash
# ファイルのみ
tidd chezmoi-audit --files-only

# apt パッケージのみ（#424 の run_once 整備後）
tidd chezmoi-audit --apt-only
```

### STEP 2: 未管理ファイルの判断と処置

スクリプト出力の「未管理ファイル候補」セクションを読み取る。

各ファイルについて以下のいずれかを判断する:

| 判断 | 処置 | コマンド例 |
|------|------|-----------|
| dotfiles として管理すべき（他の環境でも使う） | `chezmoi add` で管理対象に追加 | `chezmoi add ~/.claude/my-settings.json` |
| この環境固有・一時ファイル・機密情報 | `.chezmoiignore` に追記して除外 | `echo ".claude/my-settings.json" >> "$(chezmoi source-path)/.chezmoiignore"` |
| 判断保留 | そのまま（次回の棚卸しで再確認） | — |

**判断の目安:**
- `.bashrc`, `.gitconfig`, `.claude/settings.json` など → `chezmoi add`
- `~/.claude/projects/` 配下のプロジェクト固有ファイル → `.chezmoiignore` に追記
- トークン・APIキー・パスワードを含むファイル → **絶対に `chezmoi add` しない。`.chezmoiignore` に追記する**

### STEP 3: apt パッケージの判断と処置

スクリプト出力の「apt パッケージ棚卸し」セクションを読み取る。

各パッケージについて以下のいずれかを判断する:

| 判断 | 処置 |
|------|------|
| 開発に必要（他の環境でも必要） | chezmoi source の `run_once_install_packages.sh` の `PACKAGES` 配列に追記 |
| この環境固有・一時インストール | そのまま（追記不要） |

`run_once_install_packages.sh` の場所:

```bash
chezmoi source-path  # 例: ~/.local/share/chezmoi
# → ~/.local/share/chezmoi/run_once_install_packages.sh を編集する
```

### STEP 4: 変更を chezmoi に反映

ファイルを追加した場合:

```bash
# chezmoi が管理するファイル一覧を確認する
chezmoi managed

# dotfiles リポジトリに変更をコミットする（dotfiles リポジトリのブランチで作業すること）
cd "$(chezmoi source-path)"
git add -A
git commit -m "chore: chezmoi 棚卸しで <ファイル名> を追加する"
git push
```

## ヌケモレなしの場合

スクリプト出力に「ヌケモレなし」と表示された場合は棚卸し完了。
次回の推奨実行時期: 1ヶ月後（[docs/setup/new-environment-setup.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/new-environment-setup.md) 参照）。

## セキュリティ注意事項

- `.chezmoiignore` に追記すべきファイル: `~/.claude/projects/` 配下、`~/.ssh/`, `~/.gnupg/` 配下の秘密鍵
- `chezmoi add` すべきでないファイル: トークン・APIキー・パスワードを含む設定ファイル
- 詳細: [docs/setup/credentials-security.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/credentials-security.md)

## 依存関係

- `tidd chezmoi-audit`（`tidd_tools.chezmoi_audit` モジュール）— 棚卸しの本体（旧 `scripts/chezmoi-audit.sh` を Issue #1057 で Python 化）
- `.chezmoiignore` — 除外リスト（Issue #461 で整備）
- `run_once_install_packages.sh` — apt パッケージ宣言ファイル
