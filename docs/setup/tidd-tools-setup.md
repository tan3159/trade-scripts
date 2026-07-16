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

## gh アカウントの使い分け（本環境固有）

`tidd ai-review` は対象リポジトリへの write（レビュー投稿・マージ）に gh の認証を使う。
gh に複数アカウントがログイン済みの場合、trade-scripts のオーナーアカウントのトークンを
コマンド単位で渡す:

```bash
GH_TOKEN=$(gh auth token -u <owner-account>) tidd ai-review <PR> <試行回数>
```

`gh auth switch` はシェル横断でグローバルに効いて他セッションへ影響するため使わない。
