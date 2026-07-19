# Bare-metal セットアップ（chezmoi / Bitwarden / mise なし）

「chezmoi・Bitwarden・agy・mise を使いたくない」ユーザー向けの最小依存経路。手動編集ベースで運用する。

> **5 分で動かしたい場合はまず [`quick-start.md`](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/quick-start.md)。**

## WSL2 推奨・Windows ネイティブは制限あり

本ドキュメントは **WSL2（Ubuntu 22.04 / 24.04）** を前提としている。
環境選択の目安として以下のマトリクスを参照すること。

| 機能 | WSL2（Ubuntu） | Windows ネイティブ | 推奨 |
|------|:-:|:-:|------|
| `tidd ai-review` などの基本コマンド | ✅ | ✅ | |
| direnv（`.envrc` 経由の秘匿情報解決） | ✅ | ❌ | WSL2 |
| mise `[env]` での環境変数設定 | ✅ | ✅ | |
| hook（PreToolUse / PostToolUse） | ✅ | ⚠️ 一部制限 | WSL2 |
| screenshot 自動添付 | ✅ | ⚠️ 手動設定要 | WSL2 |
| `bw-session-check` | ✅ | ✅（`bw.exe` 不在時は env fallback） | |

**Windows ネイティブで動かす場合:** direnv の代わりに mise `[env]` または
PowerShell profile を使う。詳細は [`windows-native.md`](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/windows-native.md) を参照。

### Windows での `bw-session-check` 動作（Issue #1637）

Windows ネイティブでは `bw-session-check` のキャッシュパスが
`%LOCALAPPDATA%\tidd_tools\bw-session` に解決される（ADR 013 Phase 4-b）。

`bw.exe` が PATH にない場合は `exit 0` で終了して env fallback を案内する:

```
WARN: bw CLI が見つかりません。env fallback を使用してください
```

この場合は `SECRETS_BACKEND=env`（デフォルト）のまま環境変数で秘匿情報を管理するか、
[Bitwarden CLI を Windows にインストール](https://bitwarden.com/help/cli/) して
PATH に追加することで bw 経由の解決が使えるようになる。

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

**注:** `mise` を使わずに uv 単体で Python バージョンを解決する。`uv sync` 実行時に必要な Python が自動で
downloads される（pyproject.toml の `requires-python = ">=3.11"` に従う）。

### 2. リポジトリのクローンと Python 依存インストール

```bash
gh repo clone being-gaia-plan/ai-dev-handbook
cd ai-dev-handbook
uv sync --project projects/py/tidd_tools --extra dev
```

**動作確認:**

```bash
uv run --project projects/py/tidd_tools python -m tidd_tools --help
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

- 詳細: [`ai-review-credentials.md`](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/ai-review-credentials.md)

### B. 環境変数のセットアップ（direnv / .envrc）

API キーなどの秘匿情報は `.envrc` で管理します。リポジトリに `.envrc.example` が用意されています。

```bash
cp .envrc.example .envrc
# エディタで .envrc を開き、プレースホルダを実際の値に書き換える
direnv allow
```

`.envrc.example` には 2 パターンが含まれています:

- **方式 A（直書き）**: `export APP_ID="..."` などを直接書く。`SECRETS_BACKEND` は `"env"`（デフォルト）のまま
- **方式 B（Bitwarden 参照）**: `export SECRETS_BACKEND="bw"` に加え、`ai-review-repo-config.toml` で `APP_ID_ITEM` / `INSTALLATION_ID_ITEM` / `GH_TOKEN_ITEM` を設定することで `get_secret()` が Bitwarden から自動取得する（Issue #1630・#1712）。`*_ITEM` 設定なしでは Bitwarden フォールバックしないため注意

> **⚠️ 注意**: `.envrc` は `.gitignore` 済みです。コミットされませんが、画面共有・ログ・AI エージェントへの貼り付けには注意してください。

### C. Bitwarden なしで secrets 管理（Issue #1315 / #1630 で実装）

Bitwarden CLI (`bw`) を使わずに secrets を扱う場合は環境変数直接指定でフォールバックする。
Issue #1630 で実装された共通解決レイヤー `shared/secrets.py` は次の順序で解決する:

```
環境変数 → Bitwarden（SECRETS_BACKEND=bw のときのみ）→ エラー
```

**`SECRETS_BACKEND` 環境変数（Issue #1630）:**

| 値 | 動作 |
|----|------|
| `env`（デフォルト・未設定時も同じ） | 環境変数のみで解決。`bw` は呼ばない |
| `bw` | 環境変数にない場合は Bitwarden CLI にフォールバック |

`SECRETS_BACKEND=env`（デフォルト）では `tidd bw-session-check` は skip して exit 0 になる:

```
SECRETS_BACKEND=env のため bw session check を skip します
```

**`tidd ai-review` を Bitwarden なしで動かす環境変数指定手順:**

以下 4 つの環境変数を **すべて** 事前設定すれば、`tokens.load_ai_review_secrets()` は
`~/.ai-reviewer.sh` の source（Bitwarden 呼び出し）を **skip** して env fallback を採用する:

```bash
# 必須 3 つ
export APP_ID="<GitHub App の App ID>"
export INSTALLATION_ID="<Installation ID>"
export GH_TOKEN="<GitHub Personal Access Token または App installation token>"

# 秘密鍵は content か path のどちらか一方
export PRIVATE_KEY_CONTENT="$(cat /path/to/ai-reviewer-private-key.pem)"
# もしくは
export PRIVATE_KEY_PATH="/path/to/ai-reviewer-private-key.pem"
```

上記が揃っていれば、実行時に stderr へ以下が出力される:

```
==> ai-review: env fallback を検出しました。Bitwarden 呼び出しを skip します（Issue #1315）
```

**他の secrets（agy / Google Cloud 等）の環境変数指定例:**

```bash
# agy 用（Google Cloud Application Credentials）
export GOOGLE_APPLICATION_CREDENTIALS_JSON="$(cat /path/to/service-account.json)"

# codex 用（OpenAI）
export OPENAI_API_KEY="sk-..."
```

**永続化方針:**

- **推奨**: 一時セッションで `export`（シェル終了で消える）
- 永続化する場合は `~/.env` や systemd EnvironmentFile 等の **非 git 管理領域** に限定
- **絶対禁止**: 機密情報を git 管理下のファイルに直書きする

> **⚠️ セキュリティ原則（CLAUDE.md）**: 機密情報を Claude / AI エージェントに直接見せる操作を誘導してはならない。
> `bw get password "..."` 相当を環境変数で置き換える場合も、値の中身は log / diff / 会話に混入させない。
> ファイル配置場所は `~/.env`（`.gitignore` 済み）や systemd EnvironmentFile 等の非 git 管理領域に限定する。

- 詳細: [`ai-review-credentials.md`](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/ai-review-credentials.md) の「Bitwarden fallback」節
- 実装: `projects/py/tidd_tools/src/tidd_tools/shared/secrets.py`（`get_secret()`・`_has_env_value()`）

### C. dotfiles 同期は手動

chezmoi を使わない場合、`~/.bashrc`・`~/.gitconfig`・`~/.claude/settings.json` 等は手動で編集する。
複数マシンで同じ設定を維持したい場合は、以下のいずれか:

- 自作の shell script + git 管理（`bare-metal-dotfiles/` 等）
- rsync / scp で 1 対 1 コピー

**なぜ chezmoi なしでも動くか:** ai-dev-handbook 本体は `~/.bashrc` の内容に依存しない（PATH 追加程度で十分）。
chezmoi は「複数マシンで dotfiles を synchronize したい」場合のみ必要。

## 段階導入の推奨順序

まず 1-3 の必須ステップで動作確認。その後、以下の順で機能を追加できる（どれも必須ではない）:

| 順序 | ツール | 目的 | 前提 |
|---|---|---|---|
| 1 | agy / codex | AI review 高速化・複数 backend consensus | Anthropic API 直接呼び出し禁止（§3-1）のため必須ではない |
| 2 | Bitwarden CLI (`bw`) | secrets を安全に管理 | fallback（環境変数）で代替可 |
| 3 | chezmoi | dotfiles を複数マシン同期 | 1 マシン運用なら不要 |
| 4 | mise | 各種 CLI ツール版管理 | uv で Python 版は解決可能 |

## トラブルシュート

### `uv sync` が失敗する

```bash
# Python バージョン確認
python3 --version  # 3.11 以上必要

# uv 経由で明示的にダウンロード
uv python install 3.11
uv sync --project projects/py/tidd_tools --extra dev --python 3.11
```

### `gh auth login` で SSO 要求される場合

organization SSO が設定されている場合は `gh auth login -h github.com -s repo,workflow` で
scope を明示。

### tidd サブコマンドが `bw` を要求してエラーになる

Issue #1315 の Bitwarden fallback が該当サブコマンドに未対応の可能性。以下で確認:

```bash
grep -rn "bw get password" projects/py/tidd_tools/src/tidd_tools/
```

fallback 未対応の subcommand が見つかったら、環境変数を用意して回避するか Issue を起票する。

### publish skill で `NOTIFY_EMAIL を設定してください` エラーが出る

`/publish` skill を実行する前に `NOTIFY_EMAIL` 環境変数を設定する必要がある。

```bash
export NOTIFY_EMAIL="tan3159@gmail.com"
```

永続化する場合は `~/.envrc`（`.gitignore` 済み）または `~/.bashrc` に追記する:

```bash
# ~/.envrc または ~/.bashrc に追記（メールアドレスは実際の値に置き換える）
export NOTIFY_EMAIL="tan3159@gmail.com"
```

**セキュリティ注意:** `NOTIFY_EMAIL` はメールアドレスのため機密情報ではないが、
組織のグループアドレスを誤設定すると意図しない共有が発生する。設定値を確認してから実行すること。

## 関連

- [Quick Start](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/quick-start.md) — 5 分で動かす最短経路
- [フル環境セットアップ](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/new-environment-setup.md) — chezmoi・mise・Bitwarden 込みのフル構成
- [secrets-management.md](./secrets-management.md) — 方式 A / 方式 B / CI × Win/Linux のマトリクス手順（**秘匿情報管理の全体ガイド**）
- [ai-review-credentials.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/ai-review-credentials.md) — 環境変数一覧・Bitwarden アイテム名の詳細
- [`.envrc.example`](../../.envrc.example) — 環境変数設定テンプレート（方式 A 直書き / 方式 B Bitwarden 参照）
- [ADR 013: Windows first-class support](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/decisions/013-windows-first-class-support.md) — Windows 対応方針
- Issue #1314: 本ドキュメント新設
- Issue #1315: Bitwarden なしフォールバック実装
- Issue #1630: shared/secrets.py 新設（SECRETS_BACKEND 対応）
- Issue #1631: .envrc.example 整備
- Issue #1632: secrets-management.md 新設
