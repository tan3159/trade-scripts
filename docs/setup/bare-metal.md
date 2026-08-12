# Bare-metal セットアップ（chezmoi / mise なし）

「chezmoi・agy・mise を使いたくない」ユーザー向けの最小依存経路。手動編集ベースで運用する。

> **5 分で動かしたい場合はまず `quick-start.md`。**

## WSL2 推奨・Windows ネイティブは制限あり

本ドキュメントは **WSL2（Ubuntu 22.04 / 24.04）** を前提としている。
環境選択の目安として以下のマトリクスを参照すること。

| 機能 | WSL2（Ubuntu） | Windows ネイティブ | 推奨 |
|------|:-:|:-:|------|
| `tidd ai-review` などの基本コマンド | ✅ | ✅ | |
| direnv（`.envrc` 経由の秘匿情報解決） | ✅ | ❌ | 旧方式（#3621） |
| mise `[env]` での環境変数設定 | ✅ | ✅ | |
| hook（PreToolUse / PostToolUse） | ✅ | ⚠️ 一部制限 | WSL2 |
| screenshot 自動添付 | ✅ | ⚠️ 手動設定要 | WSL2 |

**Windows ネイティブで動かす場合:** 環境変数設定は全 OS 共通で `.mise.toml`（mise `[env]`）を使う。
詳細は `windows-native.md` を参照。

## 前提

- **WSL2（推奨）**: Ubuntu 22.04 / 24.04 推奨
  - Windows ネイティブでも `tidd ai-review` 等の基本コマンドは動作する（上記マトリクス参照）。
    ただしシェル前提の hook・skill が Windows PowerShell では動かない場合があるため WSL2 を推奨する
- Claude Code CLI がインストール済み
- GitHub CLI (`gh`) がインストール済み
- `git` が使える

## 必須ステップ

### 1. uv のインストール（Python 環境管理）

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"  # 現行シェルへ反映
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc  # 次回以降のシェルへ反映
```

**注:** `mise` を使わずに uv 単体で Python バージョンを解決する。`uv`/`uvx` 実行時に必要な Python が自動で
downloads される。

### 2. tidd の実行（uvx ゼロインストール実行方式）

このドキュメントの対象は **copier で生成した consumer 自身のリポジトリ**であり、`projects/py/tidd_tools` は存在しない。`tidd` コマンドはマシンごとの永続インストール（`uv tool install`）を行わず、`uvx --from <git spec> tidd <subcommand>` で毎回リモートから解決して実行する（`copier-workflow-adoption.md` §3 と同じ方式・`uvx` は `uv` に同梱されているため追加インストール不要）。

`<owner>/<repo>` と `<ref>` は `.copier-answers.yml` の `_src_path` / `_commit` が示す上流リポジトリの値。tidd を必要とする hook はこの spec を `_src_path` から自動解決するため、手動実行時のみ埋める。

```bash
uvx --from "git+https://github.com/<owner>/<repo>@<ref>#subdirectory=projects/py/tidd_tools" tidd --help
```

日付タグでバージョン固定する場合（推奨）は `@main` を `@v<YYYY.MM.DD>` に置き換える。

毎回 `--from` の spec 全体を明示する必要があるため、頻用する場合はシェル alias を設定すると便利:

```bash
alias tidd='uvx --from "git+https://github.com/<owner>/<repo>@<ref>#subdirectory=projects/py/tidd_tools" tidd'
```

**動作確認:**

```bash
uvx --from "git+https://github.com/<owner>/<repo>@<ref>#subdirectory=projects/py/tidd_tools" tidd --help
```

サブコマンド一覧が表示されれば OK。

### 3. GitHub 認証

```bash
gh auth login
```

**期待:** `gh auth status` で `Logged in to github.com as <username>` が表示される。

これで **`tidd create-issue`・`tidd ai-review`・`tidd test-plan`・Claude Code 単体運用** ができる。

## 推奨ステップ（必須ではない）

### A. AI review backend（agy / codex）

`agy`（Gemini）または `codex`（OpenAI）を PR レビュー時のプライマリバックエンドにしたい場合。
**なくても** Claude Code 単体で `/ai-review` skill が動く（Wave 3 #1425 で inline fallback 禁止化されているが、
`ai-reviewer.md` subagent フォールバックは有効）。

- 詳細: `ai-review-credentials.md`

### B. 環境変数のセットアップ（`.mise.toml`）

API キーなどの秘匿情報は `.mise.toml` で管理します。リポジトリに `.mise.toml.example` が用意されています。

```bash
cp .mise.toml.example .mise.toml
# エディタで .mise.toml を開き、プレースホルダを実際の値に書き換える
mise trust
```

> **⚠️ 注意**: `.mise.toml` は `.gitignore` 済みです。コミットされませんが、画面共有・ログ・AI エージェントへの貼り付けには注意してください。

### C. secrets 管理方式の設定（Issue #1315 / #1630 / #3212）

`shared/secrets.py` の `get_secret()` は以下の順序で解決する:

```
環境変数に値があれば返す → なければエラー
```

Bitwarden 等の外部シークレットマネージャ経由のフォールバックは存在しない（#3212）。

**環境変数指定手順（`.mise.toml` の `[env]` に追記・全 OS 共通）:**

以下の環境変数を **すべて** 事前設定する:

```toml
[env]
# 必須 3 つ
APP_ID = "<GitHub App の App ID>"
INSTALLATION_ID = "<Installation ID>"
GH_TOKEN = "<GitHub Personal Access Token または App installation token>"

# 秘密鍵は content か path のどちらか一方
# PRIVATE_KEY_CONTENT = """-----BEGIN RSA PRIVATE KEY-----
# （PEM 本文をそのまま貼る。$(cat ...) は使わない）
# -----END RSA PRIVATE KEY-----"""
# もしくは
# PRIVATE_KEY_PATH = "/path/to/ai-reviewer-private-key.pem"
```

**他の secrets（agy / Google Cloud 等）の環境変数指定例:**

```toml
[env]
# agy 用（Google Cloud Application Credentials）
# GOOGLE_APPLICATION_CREDENTIALS_JSON = '{"type":"service_account",...}'

# codex 用（OpenAI）
# OPENAI_API_KEY = "sk-..."
```

**永続化方針:**

- **推奨**: gitignore 済みの `.mise.toml`（mise `[env]`・全 OS 共通）に実値を書く
- **絶対禁止**: `~/.bashrc`・mise のグローバル config・PowerShell profile 等、dotfile 管理ツール経由で同期される可能性のあるファイルへの実値の直書き

> **⚠️ セキュリティ原則（CLAUDE.md）**: 機密情報を Claude / AI エージェントに直接見せる操作を誘導してはならない。

- 詳細: `ai-review-credentials.md`
- 実装: `projects/py/tidd_tools/src/tidd_tools/shared/secrets.py`（`get_secret()`。上流リポジトリ本体のみに存在し consumer には配布されない）

### D. dotfiles 同期は手動

chezmoi を使わない場合、`~/.bashrc`・`~/.gitconfig`・`~/.claude/settings.json` 等は手動で編集する。
複数マシンで同じ設定を維持したい場合は、以下のいずれか:

- 自作の shell script + git 管理（`bare-metal-dotfiles/` 等）
- rsync / scp で 1 対 1 コピー

**なぜ chezmoi なしでも動くか:** 上流リポジトリ本体は `~/.bashrc` の内容に依存しない（PATH 追加程度で十分）。
chezmoi は「複数マシンで dotfiles を synchronize したい」場合のみ必要。

## 段階導入の推奨順序

まず 1-3 の必須ステップで動作確認。その後、以下の順で機能を追加できる（どれも必須ではない）:

| 順序 | ツール | 目的 | 前提 |
|---|---|---|---|
| 1 | agy / codex | AI review 高速化・複数 backend consensus | Anthropic API 直接呼び出し禁止（§3-1）のため必須ではない |
| 2 | chezmoi | dotfiles を複数マシン同期 | 1 マシン運用なら不要 |
| 3 | mise | 各種 CLI ツール版管理 | uv で Python 版は解決可能 |

## トラブルシュート

### `uvx --from ... tidd` が失敗する

```bash
# Python バージョン確認
python3 --version  # 3.11 以上必要

# uv 経由で明示的にダウンロード
uv python install 3.11
uvx --from "git+https://github.com/<owner>/<repo>@<ref>#subdirectory=projects/py/tidd_tools" --python 3.11 tidd --help
```

### `gh auth login` で SSO 要求される場合

organization SSO が設定されている場合は `gh auth login -h github.com -s repo,workflow` で
scope を明示。

### tidd サブコマンドが「環境変数を設定してください」エラーになる

必要な環境変数（`APP_ID`・`GH_TOKEN` 等）が `.mise.toml` に未設定、または
`mise trust`/シェル再起動を忘れている可能性がある。`echo $<変数名>` で値が読めるか確認する。

### publish skill で `PUBLISH_DEFAULT_SCOPE を設定してください` エラーが出る

`/publish` skill を実行する前に `PUBLISH_DEFAULT_SCOPE` 環境変数を設定する必要がある。

```bash
export PUBLISH_DEFAULT_SCOPE="tan3159@gmail.com"
```

永続化する場合は `.mise.toml`（リポジトリ直下・`.gitignore` 済み）に追記する:

```toml
# .mise.toml の [env] に追記（メールアドレスは実際の値に置き換える）
PUBLISH_DEFAULT_SCOPE = "tan3159@gmail.com"
```

**セキュリティ注意:** `PUBLISH_DEFAULT_SCOPE` は公開先グループメールのため機密情報ではないが、
組織のグループアドレスを誤設定すると意図しない共有が発生する。設定値を確認してから実行すること。

## 関連

- Quick Start — 5 分で動かす最短経路
- フル環境セットアップ — chezmoi・mise 込みのフル構成
- [secrets-management.md](./secrets-management.md) — env 方式のセットアップ手順（**秘匿情報管理の全体ガイド**）
- ai-review-credentials.md — 環境変数一覧の詳細
- [`.mise.toml.example`](../../.mise.toml.example) — 環境変数設定テンプレート
- ADR 013: Windows first-class support — Windows 対応方針
- Issue #1314: 本ドキュメント新設
- Issue #1630: shared/secrets.py 新設
- Issue #1631: .envrc.example 整備
- Issue #1632: secrets-management.md 新設
- Issue #3212: get_secret() の env 方式一本化
