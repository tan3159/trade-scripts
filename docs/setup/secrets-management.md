# 秘匿情報の管理方式

API キー・トークン・秘密鍵などの秘匿情報を安全に管理するためのガイド。

> **IMPORTANT — 実値を書いてよい場所は gitignore 済み local ファイルのみ**
>
> `~/.bashrc`・mise のグローバル config・PowerShell profile など
> **ホームディレクトリ配下のファイルは dotfile 管理ツール（chezmoi 等）経由で
> GitHub に push される可能性があります。** これらへの実値の直書きは漏洩リスクがあるため禁止です。
>
> 許容される実値の配置先:
> - `.mise.toml`（リポジトリ直下・`.gitignore` 済み・**全 OS 共通**）
> - `.envrc`（リポジトリ直下・`.gitignore` 済み・旧方式。移行手順は「[既存ユーザー向け移行手順](#既存ユーザー向け移行手順envrc--misetoml)」参照）
>
> **シークレットは環境変数のみで解決します**（Bitwarden 等の外部シークレットマネージャ経由の解決は使いません）。
> 詳細は root `CLAUDE.md` のセキュリティ原則を参照。

---

## 方式: env（環境変数）一本化

シークレットは **環境変数** でのみ解決します。実値の配置先は gitignore 済み local
ファイル（`.mise.toml`。既存 `.envrc` は移行推奨）に限定します。

---

## セットアップ手順

### 全 OS 共通: `.mise.toml`（mise `[env]`）

Linux / WSL / macOS / Windows ネイティブの **全 OS で同じ手順**を使う。
direnv（`.envrc`）は旧方式のため新規セットアップでは使わない
（移行手順は「[既存ユーザー向け移行手順](#既存ユーザー向け移行手順envrc--misetoml)」参照）。

```bash
# 1. .mise.toml.example からコピー
cp .mise.toml.example .mise.toml

# 2. エディタで .mise.toml を開いて実際の値を書く（プレースホルダを置き換える）
#    例: APP_ID = "123456"
#        GH_TOKEN = "github_pat_xxx..."

# 3. mise に設定ファイルを信頼させる（direnv allow に相当・必須）
mise trust

# 4. 確認（値の内容を表示せず、非空かどうかだけをチェック）
#    `mise env` は実値を標準出力に出すため使わないこと（credential leakage）
[ -n "$APP_ID" ] && echo "APP_ID: set" || echo "APP_ID: unset"
[ -n "$GH_TOKEN" ] && echo "GH_TOKEN: set" || echo "GH_TOKEN: unset"
```

> **注意:** `.mise.toml` に実値を書く前に必ず `.gitignore` に追加してからコミットすること。
> `.gitignore` への追加を忘れると実値がリポジトリに混入する。

**`~/.bashrc`・mise のグローバル config（ホームディレクトリ配下）・PowerShell profile
（`$PROFILE`）への実値の直書きは禁止です。** これらは dotfile 管理ツール（chezmoi 等）経由で
git に push される可能性があります。実値は必ずリポジトリ直下の `.mise.toml`
（`.gitignore` 済みのローカルファイル）にのみ書いてください。

---

### 上流リポジトリ用 PAT（copier 配布先 consumer 向け）

consumer は自分のリポジトリに加え、**上流リポジトリ（テンプレート配布元・多くの場合別 org）** も操作する。
`copier update` の状態確認・配布物の実装確認・テンプレート由来の不具合報告・consumer で必要になった
ルールの上流化要求などがそれにあたる。上流には consumer 自身の PAT ではアクセスできないため
（`GraphQL: Could not resolve to a Repository` で失敗）、**上流用 PAT** を別途発行して使う。

#### 発行手順（必要スコープ）

1. GitHub にログイン → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
2. 以下の設定を行う:

| 項目 | 設定 |
|------|------|
| Token name | `upstream_<consumer-project>` |
| Expiration | 最長 1 年 |
| Repository access | **Only select repositories** → 上流リポジトリを選択 |
| Repository permissions → Contents | **Read-only**（`copier update` の状態確認・配布物の実装確認用） |
| Repository permissions → Issues | **Read and write**（不具合報告・要望の起票用） |
| Repository permissions → Pull requests | **Read and write**（上流への PR 作成用） |
| Repository permissions → Metadata | **Read-only**（自動付与） |

3. **Generate token** → 表示された PAT（`github_pat_...`）をコピーする（表示は 1 回限り）

#### 設定手順（上流クローン側の `.mise.toml`）

上流用 PAT は **上流リポジトリのクローン先** の `.mise.toml`（gitignore 済み）に環境変数として定義する。
consumer 自身のリポジトリの `.mise.toml` には書かない。

```bash
# 上流リポジトリのクローン先ディレクトリで実行
cp .mise.toml.example .mise.toml
# エディタで .mise.toml を開き [env] に上流用 PAT を定義する
#   GH_TOKEN = "github_pat_xxx..."
mise trust
```

**AI エージェントはこの値を Read しない。** 上流を操作するときは上流クローン先ディレクトリで
`mise exec -- gh ...` を実行し、`.mise.toml` の `[env]` 定義を環境として適用する
（詳細: `.claude/rules/workflow.md`「GitHub 操作の使い分け」）。変数名を確認するときも
`mise env --json | jq 'keys'` で **キーのみ** を出力し、値を表示しない。

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

CircleCI の **Project Settings > Environment Variables** で登録する。

```yaml
# .circleci/config.yml のジョブ内（自動的に注入される）
steps:
  - run:
      name: Run ai-review
      command: uvx --from "git+https://github.com/<owner>/<repo>@<ref>#subdirectory=projects/py/tidd_tools" tidd ai-review $PR_NUMBER 1
```

`tidd` コマンドは uvx ゼロインストール実行方式（`copier-workflow-adoption.md` §3 §3・#3087）で毎回リモートから解決して実行する。`uv tool install` によるマシンごとの永続インストールは行わない。`projects/py/tidd_tools` は 上流リポジトリ本体のみに存在し、consumer には配布されないため `uv run --project projects/py/tidd_tools` は使えない。

---

## secrets.py の解決ロジック（実装参照）

`tidd_tools/shared/secrets.py` の `get_secret(name)` は以下の順序で解決する（Issue #1630 の設計）:

```
1. 環境変数 name に実質的な値（非空・非空白）があればそれを返す
2. なければ RuntimeError を raise する
```

Bitwarden 等の外部シークレットマネージャ経由のフォールバックはありません。環境変数の
出どころ（`.mise.toml` / CI Secret / シェルの一時 `export`）は問いません。

---

## 既存ユーザー向け移行手順（.envrc → .mise.toml）

`.envrc`（direnv）で実値を管理していた既存ユーザーは、以下の手順で `.mise.toml` へ移行する。

```bash
# 1. .envrc に定義している変数名を確認する（値は標準出力に出さない）
grep -oE '^export [A-Z_]+=' .envrc | sed -E 's/^export ([A-Z_]+)=.*/\1/'

# 2. .mise.toml.example をコピーして [env] に実値を移す
cp .mise.toml.example .mise.toml
#    エディタで開き、手順 1 で確認した変数名に対応する行に実値を記載する
#    例: APP_ID = "123456"
#        GH_TOKEN = "github_pat_xxx..."

# 3. mise に設定ファイルを信頼させる（必須）
mise trust

# 4. 確認（値を表示せず非空かどうかだけをチェック）
[ -n "$GH_TOKEN" ] && echo "GH_TOKEN: set" || echo "GH_TOKEN: unset"

# 5. 移行が確認できたら旧 .envrc を削除する
rm .envrc
```

**注意点:**
- **`NOTIFY_EMAIL` → `PUBLISH_DEFAULT_SCOPE` のリネーム（Issue #3678）:** 旧名 `NOTIFY_EMAIL`
  は実態（publish 成果物の Google Drive 共有先グループメール）と名前が乖合していたため廃止し、
  `PUBLISH_DEFAULT_SCOPE` に一本化した。既存設定者は `.mise.toml`（および旧 `.envrc`）の
  変数名を `PUBLISH_DEFAULT_SCOPE` に書き換えること（値はそのまま流用できる）。
- `.envrc` の `export` 行は bash 構文だが、`.mise.toml` の `[env]` は TOML 構文。
  `export FOO="bar"` → `FOO = "bar"` の形式に変換する。
- **`~` / `$HOME` の展開はされない**。`PRIVATE_KEY_PATH` 等のパスは必ず絶対パスで書くこと。
- 複数行の値（例: GitHub App の PEM）はトリプルクォート `"""..."""` に置き換える。

---

## セキュリティ上の注意点

- **`.mise.toml`**（旧方式の `.envrc` 含む）は `.gitignore` 済みのため git にはコミットされない。しかし画面共有・ログ・AI エージェントへの貼り付けには注意すること
- **`~/.bashrc`・mise グローバル config・PowerShell profile** への実値の直書きは禁止（dotfile 管理ツール経由で git に push されるリスクがある）
- **AI エージェント（Claude Code 等）** に実値を直接見せてはならない。Claude Code の `settings.json` permission deny で `.env`/`.env.*`/`.envrc`/`.mise.toml` への Read/Write/Edit を禁止する
- **CI の Secret** は UI 経由で登録する。YAML やコードに実値を書かない

---

## 関連ドキュメント

- `copier-workflow-adoption.md §8` — public リポジトリで採用する場合の追加考慮（secret scanning・PAT・fork PR 対策）
- [`bare-metal.md`](./bare-metal.md) — chezmoi なしの最小構成セットアップ
- [`.mise.toml.example`](../../.mise.toml.example) — 環境変数設定テンプレート
- Issue #1630: `shared/secrets.py` 新設
- Issue #1631: `.envrc.example` 整備
