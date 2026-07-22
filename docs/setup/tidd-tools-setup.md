# tidd_tools 導入手順

Issue #3 で適用した手順の記録と再現手順。配布元 ai-dev-handbook は **private** リポジトリのため、
インストール時に読み取り認証が必要になる。**本リポジトリは public のため、トークン・PAT を
リポジトリ内ファイルや CI secrets に置かない**（ローカル環境の credential 管理のみ）。

## インストール（2026-07-17 適用済み）

```bash
uv tool install "tidd-tools @ git+https://github.com/being-gaia-plan/ai-dev-handbook@v2026.07.17.1#subdirectory=projects/py/tidd_tools"
```

- バージョンは**安定タグ**（`vYYYY.MM.DD[.N]`）で固定する。`@main` 直参照は再現性がないため使わない
- `v2026.07.17.1` には consumer 必須の修正（上流 #2224: uv tool install 環境で repo root 解決が
  `__file__` 起点のためクラッシュする問題の修正）が含まれる。**これより古いタグでは
  `tidd ai-review` が `FileNotFoundError` で動かない**
- 更新時は `--force` を付けて再実行する

## 認証（private 配布元の読み取り）

- git の credential helper に GitHub 認証が設定済みであれば **PAT の新規発行は不要**
  （`gh auth login` 済みアカウントが ai-dev-handbook への read 権限を持つ場合、
  `uv tool install` の git clone がそのまま通る）
- credential helper が無い環境では Fine-grained PAT（ai-dev-handbook / Contents: Read）を発行し、
  Bitwarden 等の secret manager で管理して git credential として渡す
- **トークン値をファイル（.envrc 含む）やリポジトリに書かない**。詳細: `docs/setup/secrets-management.md`

## 動作確認

```bash
tidd --help                              # サブコマンド一覧が表示される
python3 -c "import shutil; print(shutil.which('tidd'))"  # hooks からの PATH 解決確認
```

- `tidd` 依存 hook（`analyze-loop-on-stop.py` 等）は `shutil.which("tidd")` で PATH から解決する。
  `~/.local/bin` が PATH に入っていれば no-op にならず動作する
- 実運用確認済み: PR #11 を `tidd ai-review 11 1` → APPROVE → auto-merge まで完走（2026-07-17）

## pytest-bdd による feat/fix の 2 層テスト構造（Issue #22）

`projects/py/trade_scripts` の dev 依存に `pytest-bdd` を追加済み。feat/fix Issue の
`## 振る舞い` セクションから `.feature` + step_defs skeleton を生成する:

```bash
tidd extract-feature <Issue番号>
```

- **`--out-dir`・`--step-defs-dir` の指定は不要**（trade-scripts は `projects/py/` 配下に
  単一プロジェクトのみのため、上流 #2258 の自動解決で `projects/py/trade_scripts/tests/features/`・
  `tests/step_defs/` に出力される）
- 生成物は `uv run --extra dev pytest` で collect・実行できる（動作確認済み: 2026-07-22、
  Issue #49 で検証用 `.feature` を生成し `pytest --collect-only`・`ruff check`・`mypy` が
  いずれも問題なく通ることを確認済み）
- step_defs skeleton は `pytest.xfail()` で保留状態のまま生成される。実装完了時に `xfail` を
  外して真の実装に置き換えること（`xfail` のままマージ禁止・`.claude/rules/testing-framework.md`）

## ai-review-repo-config.toml による App 切り替え（Issue #56）

public リポジトリでは `tidd ai-review` が Bitwarden 読み込みをスキップする（security: 任意 bash 実行リスク）。
事前に環境変数へシークレットを設定してから実行する:

```bash
APP_ID=$(bw get password "ai-review/app-id-<app-item>") \
INSTALLATION_ID=$(bw get password "ai-review/installation-id-<app-item>") \
PRIVATE_KEY_CONTENT=$(bw get password "ai-review/private-key-<app-item>") \
GH_TOKEN=$(bw get password "ai-review/gh-token-<gh-item>") \
  bash -c 'GH_TOKEN=$(gh auth token -u <owner-account>) tidd ai-review <PR> <試行回数>'
```

`.claude/ai-review-repo-config.toml` の `*_ITEM` 変数はセッション経由でのみ使用され、
public repo での Bitwarden スキップを回避するためには環境変数を直接設定する必要がある。
上流修正追跡: 上流ハンドブック #2415（public repo での env fallback 対応）

便宜上 `~/.bashrc.local` に以下の関数を追加しておくと手順を短縮できる:

```bash
_load_<reponame>_review_secrets() {
  APP_ID_ITEM="ai-review/app-id-<app-item>" \
  INSTALLATION_ID_ITEM="ai-review/installation-id-<app-item>" \
  PRIVATE_KEY_ITEM="ai-review/private-key-<app-item>" \
  _load_ai_review_secrets
}
```

## gh アカウントの使い分け（本環境固有）

`tidd ai-review` は対象リポジトリへの write（レビュー投稿・マージ）に gh の認証を使う。
gh に複数アカウントがログイン済みの場合、trade-scripts のオーナーアカウントのトークンを
コマンド単位で渡す:

```bash
GH_TOKEN=$(gh auth token -u <owner-account>) tidd ai-review <PR> <試行回数>
```

`gh auth switch` はシェル横断でグローバルに効いて他セッションへ影響するため使わない。
