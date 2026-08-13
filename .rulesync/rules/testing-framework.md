---
root: false
targets:
  - '*'
---
# テストフレームワーク棲み分けルール

## フレームワーク選択表

GAS コード → **Jest**（`projects/gas/<project>/tests/`）、Python コード → **pytest**（`projects/py/<project>/tests/`）。
`tidd run-project-tests` 一元管理。bats 撤去済み（#1090）。

## feat/fix 振る舞いテストは `.feature`（+step_defs）または `tests/test_*.py` のどちらかが必須（#1962）

feat/fix は 2 層構造がデフォルト: 振る舞い層（`.feature` + step_defs）/ 実装詳細層（`test_*.py`）。
**判定基準:** 外部観測可能な振る舞い（端末出力・ファイル状態・exit code）→ `.feature`、内部ユーティリティ・境界値 → `tests/test_*.py`。
**hook 契約系**（`## やること` が `.claude/hooks/` のみ）は `.feature` 不要・`test_<hookname>_hook.py` のみ（#1855）。

`tidd extract-feature <N>` で `.feature` + `step_defs`（xfail pending）を生成する。

**CRITICAL: feat/fix PR は `.feature`（+step_defs）または `tests/test_*.py` のどちらかが必須（#1962）。**
`tidd test-plan` が未生成を exit 1 でブロック。**完了時に `xfail` を外すこと（xfail のままマージ禁止）。**

## CRITICAL: やってはいけないこと

- GAS に pytest / Python に Jest を使わない
- 間接テスト（フレームワークをラップするだけ）を置かない
- **外部観測可能な振る舞いを `test_*.py` だけで書く** — `.feature` + step_defs で記述すること（#1962）
- step_defs を `xfail` のままマージしない

### REQUIRED: pytest marker `target_<basename>`

新規 pytest ファイルに **必ず** `@pytest.mark.target_<basename>` を付ける（#785）。
例: `@pytest.mark.target_ai_review`。ハイフンはアンダースコアに変換。

## テストを書かなくてよいケース（Phase 1 縮小後・4 項目）

- `run_once_` / `run_always_` / `run_onchange_` スクリプト（副作用・モック困難）
- ドキュメントのみの変更（`docs/`・`*.md`）
- 環境構築スクリプトの副作用部分（ネットワーク依存）
- 外部サービス連携の認証・接続確認（`[手動]` 項目として扱う）

**適用範囲外（feat/fix でもテスト不要）:** `config/` 配下の CSS・YAML・JSON、`docs/` frontmatter のみの変更、`pyproject.toml` のマーカー追加など、`docs/reference/testing-guide.md` 「よくある誤適用パターン」を参照。

## テストファイル保護（`protect-tests.py`）

`protect-tests.py` が `*/tests/` への書き込みをブロック（#833）。
更新時は PR ボディに `<!-- allow-test-update: <理由> -->` を追加してバイパスする（理由必須）。

**詳細ガイド（実装例・protect-tests・判断フロー・Phase 2 計画）:** docs/reference/testing-guide.md
