# 秘匿情報の管理方式

API キー・トークン・秘密鍵などの秘匿情報をどの方式で管理するかを選択するためのガイド。

> **IMPORTANT — 実値を書いてよい場所は gitignore 済み local ファイルのみ**
>
> `~/.bashrc`・mise のグローバル config・PowerShell profile など
> **ホームディレクトリ配下のファイルは dotfile 管理ツール（chezmoi 等）経由で
> GitHub に push される可能性があります。** これらへの実値の直書きは漏洩リスクがあるため禁止です。
>
> 許容される実値の配置先:
> - `.envrc`（リポジトリ直下・`.gitignore` 済み）
> - `.mise.toml`（リポジトリ直下・`.gitignore` 済み）
>
> それ以外の場合は **Bitwarden CLI（`bw get password "..."`）参照** を使うこと。
> 詳細は [CLAUDE.md セキュリティ原則](../../CLAUDE.md) を参照。

---

## 方式の選択

| | 方式 A（直書き） | 方式 B（Bitwarden 参照）|
|---|---|---|
| **位置付け** | 簡易・自己責任 | **推奨** |
| `bw` CLI のインストール | 不要 | 必要 |
| `.envrc` への実値の記録 | あり | なし |
| 値がターミナル履歴・ログに残るリスク | あり | ほぼなし |
| Windows ネイティブ（direnv 不可）| mise `[env]` か PowerShell profile を使う | bw fallback で追加設定ゼロ |

**方式 B（bw 経由）を推奨する理由:** `.envrc` に実値を書かないため、誤って共有・ログに混入するリスクが低い。
`tidd ai-review` などのツールが `SECRETS_BACKEND=bw` を検知して Bitwarden から自動取得するため、
一度 `bw unlock` すれば手動 export は不要。

**方式 A を使う判断基準:** bw のインストール・設定が難しい、組織規定で bw が利用できないなど、
`bw` を導入できない場合に使う。`.envrc`（gitignore 済みローカルファイル）への実値の書き込みは
リポジトリに入らないため許容できる（CLAUDE.md セキュリティ原則参照）。

---

## セットアップマトリクス

### 方式 A（直書き）× Linux/WSL

direnv がインストール済みの Linux / WSL2 環境向け。

```bash
# 1. .envrc.example からコピー
cp .envrc.example .envrc

# 2. エディタで .envrc を開いて実際の値を書く（プレースホルダを置き換える）
#    例: export APP_ID="123456"
#        export GH_TOKEN="github_pat_xxx..."
#        export SECRETS_BACKEND="env"   # デフォルト。明示しなくても同じ

# 3. direnv に許可させる
direnv allow

# 4. 確認
echo $APP_ID   # 値が表示されれば OK
```

**direnv がない場合:**

`~/.bashrc` への実値の直書きは dotfile 管理ツール（chezmoi 等）経由で git に push されるリスクがあるため禁止です。
代わりに以下のいずれかを使用してください。

**推奨: Bitwarden 参照形式**

```bash
# ~/.bashrc には bw 参照のみ書く（実値は書かない）
echo 'export GH_TOKEN=$(bw get password "GitHub PAT（ai-reviewer）")' >> ~/.bashrc
echo 'export APP_ID=$(bw get password "GitHub App ID")' >> ~/.bashrc
source ~/.bashrc
```

> bw CLI のインストール・ログイン手順は [方式 B の手順](#方式-b（bw-経由）×-linuxwsl) を参照。

**代替: gitignore 済みの local ファイル（`.envrc`）**

direnv をインストールして `.envrc`（リポジトリ直下・`.gitignore` 済み）に実値を書く方法（本ページ上部の方式 A の標準手順）を使用してください。

---

### 方式 A（直書き）× Windows ネイティブ

Windows ネイティブでは direnv が動作しない。mise `[env]` または PowerShell profile を使う。

#### 選択肢 1: mise `[env]`（mise インストール済みの場合）

リポジトリ直下の `.mise.toml`（ローカル・`.gitignore` 必須）に実値を記載する。

> **mise のグローバル config（ホームディレクトリ配下）への実値の直書きは禁止です。**
> グローバル config は dotfile 管理ツール（chezmoi 等）経由で git に push される可能性があります。
> 実値はリポジトリ直下の `.mise.toml`（`.gitignore` 済みのローカルファイル）にのみ書いてください。

```toml
# .mise.toml（リポジトリ直下・.gitignore 必須）
[env]
APP_ID = "123456"
INSTALLATION_ID = "78901234"
GH_TOKEN = "実際のトークン値"   # ← 実値はここに書く（このファイルは .gitignore 済みのためリポジトリに入らない）
SECRETS_BACKEND = "env"
```

```powershell
# .mise.toml を gitignore に追加（初回のみ・必須）
Add-Content .gitignore ".mise.toml"

# 確認
mise env   # 設定された環境変数が一覧表示される
```

> **注意:** `.mise.toml` に実値を書く前に必ず `.gitignore` に追加してからコミットすること。`.gitignore` への追加を忘れると実値がリポジトリに混入する。

#### 選択肢 2: PowerShell profile（mise なしの場合）

> **PowerShell profile（`$PROFILE`）への実値の直書きは禁止です。**
> PowerShell profile は gitignore された local ファイルではなく、ユーザーのホームディレクトリ配下に永続保存されます。
> dotfile 管理ツール経由で git に push されるリスクがあるため、実値は書かないでください。

代わりに以下のいずれかを使用してください。

**推奨: Bitwarden 参照形式**

```powershell
# PowerShell profile を開く
notepad $PROFILE

# 以下を追記して保存（実値は書かない・bw 参照のみ）
$env:SECRETS_BACKEND = "bw"
# APP_ID・GH_TOKEN 等の実値は bw から自動取得されるため追記不要
```

> bw CLI のインストール・ログイン手順は [方式 B の手順](#方式-b（bw-経由）×-windows-ネイティブ) を参照。

**代替: mise `[env]`（選択肢 1）を使用する**

mise がインストール済みであれば、上記の選択肢 1（リポジトリ直下の `.mise.toml`）を使用してください。

PowerShell を再起動すると自動的に読み込まれる。

---

### 方式 B（bw 経由）× Linux/WSL

Bitwarden CLI がインストール済みの Linux / WSL2 環境向け。**推奨構成。**

```bash
# 1. Bitwarden CLI をインストール（未インストールの場合）
npm install -g @bitwarden/cli   # または mise use -g bitwarden

# 2. bw にログイン
bw login <メールアドレス>

# 3. .envrc に SECRETS_BACKEND のみ設定する（実値は書かない）
cp .envrc.example .envrc
# .envrc を開いて方式 B のブロックのコメントを外す
# → export SECRETS_BACKEND="bw" の行を有効化するだけでよい
direnv allow

# 4. セッション開始時に bw unlock（~/.bashrc か tidd bw-session-check 経由）
export BW_SESSION=$(bw unlock --raw)
# または: tidd bw-session-check（セッションが切れていれば再 unlock を促す）

# 5. 確認
echo $SECRETS_BACKEND   # "bw" が表示される
tidd ai-review --help   # bw から自動取得してトークンが解決される
```

**bw のアイテム名とツールの対応:**

| 環境変数 | Bitwarden アイテム名 | 用途 |
|---------|---------------------|------|
| `APP_ID` | `GitHub App ID` | ai-review GitHub App |
| `INSTALLATION_ID` | `GitHub App Installation ID` | ai-review GitHub App |
| `GH_TOKEN` | `GitHub PAT（ai-reviewer）` | PR 操作 |
| `OPENAI_API_KEY` | `OpenAI API Key` | codex backend |
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` | `Google Cloud SA Key（agy）` | agy backend |
| `GITHUB_MCP_PAT` | `ai-review/github-mcp-pat-tan3159` | `.mcp.json` の github MCP server（`mcp__github__*`） |

詳細な環境変数一覧は [`ai-review-credentials.md`](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/ai-review-credentials.md) を参照。

> **適用記録（Issue #23・2026-07-22）:** `_load_ai_review_secrets`（s-tanaka-dotfiles#43）でリポジトリ owner 名（`tan3159`）から bw アイテム suffix を導出し、`GITHUB_MCP_PAT` / `GH_TOKEN` を export した状態で Claude Code を起動。`mcp__github__get_me` が疎通し、github MCP server（`mcp__github__*`）が利用可能になったことを確認済み。

---

### 方式 B（bw 経由）× Windows ネイティブ

Windows ネイティブでは direnv が使えないが、`shared/secrets.py` の `get_secret()` が
環境変数に値がない場合に `bw get password "<アイテム名>"` を自動的に呼び出す仕組みになっている
（Issue #1630）。**`.envrc` なしで追加設定ゼロで動作する。**

```powershell
# 1. Bitwarden CLI をインストール
winget install Bitwarden.CLI   # または scoop install bitwarden-cli

# 2. bw にログイン
bw login <メールアドレス>

# 3. SECRETS_BACKEND を PowerShell profile に設定（環境変数に実値は書かない）
notepad $PROFILE
# 以下を追記:
#   $env:SECRETS_BACKEND = "bw"

# 4. セッション開始時に bw unlock
$env:BW_SESSION = (bw unlock --raw)

# 5. 確認
# tidd コマンドを実行すると、APP_ID 等の未設定変数は
# get_secret() が自動的に bw から取得する
```

`SECRETS_BACKEND=bw` を設定するだけで、`.envrc` なしに Bitwarden からの自動取得が機能する。
Windows で mise を使っている場合は mise `[env]` に `SECRETS_BACKEND = "bw"` を書いてもよい。

---

### CI（GitHub Secrets 等）

GitHub Actions / CircleCI などの CI 環境向け。実値は CI の Secret 設定 UI で登録し、
ワークフロー YAML では変数名のみ参照する。

#### GitHub Actions


```yaml
# .github/workflows/example.yml
env:
  APP_ID: ${{ secrets.APP_ID }}
  INSTALLATION_ID: ${{ secrets.INSTALLATION_ID }}
  GH_TOKEN: ${{ secrets.GH_TOKEN }}
  SECRETS_BACKEND: env   # CI では env（環境変数）を使う
```


GitHub リポジトリの **Settings > Secrets and variables > Actions** で Secret を登録する。

**登録する Secret 一覧:**

| Secret 名 | 内容 |
|-----------|------|
| `APP_ID` | GitHub App の App ID |
| `INSTALLATION_ID` | GitHub App の Installation ID |
| `GH_TOKEN` | GitHub PAT または App installation token |
| `PRIVATE_KEY_CONTENT` | GitHub App の秘密鍵（PEM 形式の全文） |

#### CircleCI

CircleCI の **Project Settings > Environment Variables** で登録する。詳細は
[`circleci-secrets.md`](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/circleci-secrets.md) を参照。

```yaml
# .circleci/config.yml のジョブ内（自動的に注入される）
steps:
  - run:
      name: Run ai-review
      command: tidd ai-review $PR_NUMBER 1
      environment:
        SECRETS_BACKEND: env
```

`tidd` コマンドは `uv tool install "tidd-tools @ git+..."`（[`copier-workflow-adoption.md` §3](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/copier-workflow-adoption.md)）で導入する。`projects/py/tidd_tools` は ai-dev-handbook 本体のみに存在し、consumer には配布されないため `uv run --project projects/py/tidd_tools` は使えない。

---

## secrets.py の解決ロジック（実装参照）

`tidd_tools/shared/secrets.py` の `get_secret()` は以下の順序で解決する（Issue #1630 の設計）:

```
1. 環境変数に値があれば返す
   → 方式 A（直書き）・CI・方式 B（.envrc 展開済み）すべてここで解決

2. SECRETS_BACKEND=bw かつ bw コマンドが利用可能なら
   bw get password "<アイテム名>" を呼び出して返す
   → 方式 B（bw 経由）の direnv 未経由ケース・Windows ネイティブ

3. どちらも解決できなければ「2 通りの設定方法」を案内してエラー終了
```

**方式 A/B の分岐はコード側では意識しない。** 環境変数に値が入っていれば出どころを問わず使う。

---

## セキュリティ上の注意点

- **`.envrc`** は `.gitignore` 済みのため git にはコミットされない。しかし画面共有・ログ・AI エージェントへの貼り付けには注意すること
- **`~/.bashrc`・PowerShell profile** への実値の直書きは禁止（chezmoi 等の dotfile 管理ツール経由で git に push されるリスクがある）。Bitwarden 参照（`$(bw get password "...")`）形式を使うこと
- **AI エージェント（Claude Code 等）** に実値を直接見せてはならない。`bw get password "<アイテム名>"` のアイテム名のみ共有する（CLAUDE.md セキュリティ原則参照）
- **CI の Secret** は UI 経由で登録する。YAML やコードに実値を書かない

---

## tidd configure で管理方式を対話設定する（Issue #1634）

`SECRETS_BACKEND` の設定方式は `tidd configure` の対話ウィザードで選択できます:

```bash
tidd configure
# → 対話ウィザードが起動し、hook on/off と SECRETS_BACKEND 方式を 4〜6 個の質問で設定します
```

`tidd configure` は `~/.config/tidd_tools/hooks-config.json` に hook の on/off を保存します。
`SECRETS_BACKEND` 自体は環境変数のため、ウィザードで案内されたコマンドを `~/.bashrc` や `.envrc` に追記してください。

---

## 関連ドキュメント

- [`ai-review-credentials.md`](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/ai-review-credentials.md) — 環境変数一覧・Bitwarden アイテム名の詳細
- [`copier-workflow-adoption.md §8`](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/copier-workflow-adoption.md#8-public-リポジトリで採用する場合2220) — public リポジトリで採用する場合の追加考慮（secret scanning・PAT・fork PR 対策）
- [`bare-metal.md`](./bare-metal.md) — chezmoi/bw なしの最小構成セットアップ
- [`windows-native.md`](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/windows-native.md) — Windows ネイティブ環境固有の手順
- [`circleci-secrets.md`](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/setup/circleci-secrets.md) — CircleCI での Secret 管理
- [`.envrc.example`](../../.envrc.example) — 方式 A/B 両方のテンプレート
- Issue #1630: `shared/secrets.py` 新設（SECRETS_BACKEND 設計）
- Issue #1631: `.envrc.example` 整備
- Issue #1634: `tidd configure` 対話ウィザード（hook on/off + SECRETS_BACKEND 案内）
