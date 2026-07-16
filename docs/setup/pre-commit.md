# pre-commit + detect-secrets セットアップ

**public リポジトリ**のため、commit 段階での秘密情報検知を第一防衛線とする
（GitHub 側の secret scanning push protection は最終防衛線）。

## インストール

```bash
uv tool install pre-commit
uv tool install detect-secrets
cd <repo>
pre-commit install
```

`pre-commit install` は clone ごとに 1 回実行する（`.git/hooks/pre-commit` に登録される。
worktree は main リポジトリの hooks を共有するため main 側で 1 回でよい）。

### global `core.hooksPath` と競合する場合

dotfiles 等で global に `core.hooksPath` を設定していると `pre-commit install` が
`Cowardly refusing to install hooks with core.hooksPath set.` で拒否する。回避手順:

```bash
# 1. global 設定を隠して shim を .git/hooks に生成
GIT_CONFIG_GLOBAL=/dev/null pre-commit install
# 2. repo ローカルで hooksPath を共有 hooks dir（絶対パス）に向ける
#    ※ 相対パス `.git/hooks` は worktree（.git がファイル）で解決できないため絶対パス必須
git config --local core.hooksPath "$(git rev-parse --path-format=absolute --git-common-dir)/hooks"
# 3. global hooksPath にあった hook（例: post-merge）が必要なら .git/hooks へコピーして維持
cp ~/.config/git/hooks/post-merge .git/hooks/ 2>/dev/null || true
```

## 構成（`.pre-commit-config.yaml`）

| hook | 役割 |
|------|------|
| pre-commit-hooks（trailing-whitespace / end-of-file-fixer / check-yaml / check-added-large-files） | 基本衛生 |
| Yelp/detect-secrets | 秘密情報検知（`.secrets.baseline` 参照） |
| ruff-format | `projects/py/` 配下の Python フォーマット |

gherkin-lint は `.feature` ファイル導入後に追加する（Issue #7）。

## `.secrets.baseline` の運用

baseline は「既知の偽陽性（ドキュメント上のプレースホルダー例等）」の台帳。
**実値の秘密情報を baseline に登録してはならない。**

新しい偽陽性で commit がブロックされたら、実値でないことを確認したうえで再生成する:

```bash
detect-secrets scan --exclude-files '\.secrets\.baseline$' > .secrets.baseline
git add .secrets.baseline
```

## 履歴フルスキャン（単発）

コミット履歴全体の漏洩確認は gitleaks で行う:

```bash
gh release download -R gitleaks/gitleaks --pattern '*linux_x64.tar.gz' -D /tmp/gitleaks
tar xzf /tmp/gitleaks/gitleaks_*_linux_x64.tar.gz -C /tmp/gitleaks gitleaks
/tmp/gitleaks/gitleaks git <repo> --no-banner --redact
```

2026-07-17 実施: 5 commits scanned / **no leaks found**。

## 動作確認（ダミー秘密情報）

```bash
echo 'aws_access_key_id = "AKIAIOSFODNN7EXAMPLE"' > dummy_secret_test.txt  # pragma: allowlist secret
# （AWS 公式ドキュメントの example キー）
# git add → git commit すると detect-secrets が exit 1 でブロックする
# 2026-07-17 実施: ブロック確認済み（確認後にファイル削除）
```

## 関連

- `docs/setup/secrets-management.md` — Bitwarden CLI による秘密情報管理
- `docs/setup/repo-settings.md` — GitHub 側 secret scanning / push protection 設定
