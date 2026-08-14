# Test Plan チェックリスト規約

> **注記:** 以下の機械強制（`require-red-first` / `protect-tests` 等）は `config.json` で対応 hook を enable した利用者にのみ適用される（`copier copy` 直後は default OFF）。

PR の `## Test plan` 記述ルール・TDD/BDD ワークフロー・テストフレームワーク選択を定義する。

---

## pytest-bdd による Executable Specification

`tidd extract-feature <N>` で `.feature` と `step_defs` skeleton を生成する。

**feat/fix PR は `.feature`（+step_defs）または `tests/test_*.py` のどちらかが必須（#1962）。**
`projects/py/*/src/`・`projects/gas/*`（tests 除く）に適用。`.claude/hooks/` のみは除外。適用範囲外（config/・docs frontmatter 等）は `testing-framework.md` 参照。
`tidd test-plan` が未生成を exit 1 でブロック。
**BDD vs pytest:** 外部観測可能な振る舞い → `.feature`、内部ユーティリティ・境界値 → `tests/test_*.py`。

---

## TDD/BDD ワークフロー（feat/fix 必須）

Gherkin 読む → テスト書く → RED 確認 → 実装 → GREEN 確認 → PR 作成。
`require-red-first.py` が commit 順序を機械強制。分割不能: `<!-- allow-single-commit: <理由> -->`。
`tests/` コミット後は `protect-tests.py` により変更不可。

---

## Test plan 項目の種類（2 分類・#2026）

Scenario は Issue `## 振る舞い` に書く（PR ボディコピー不要）。Jest・pytest 項目は省略可（自動実行）。

**`[手動]` prefix は廃止（後方互換のみ・新規記述禁止・`[AI確認-post-merge]` を使う）。**

| 種別 | 書き方（例） | 動作 |
|------|------------|------|
| **AI確認** | `- [ ] [AI確認] workflow.md に記載が追加されていること` | APPROVE 後 Claude が検証 → `- [x]` |
| **AI確認-post-merge** | `- [ ] [AI確認-post-merge] nightly-tests が GREEN になること` | マージ後 cron 検証・auto-merge を妨げない |
| **未カバー**（禁止） | `- [ ] chezmoi status で差分が出ない` | `tidd test-plan` が exit 1 でブロック |

GitHub Commit Status に ❌ がある場合 auto-merge を exit 4 でブロック（#831）。

**自動転記（#2026・自己修復 #2082）:** Issue やることの `[手動]`/`[AI確認]` は PR 作成時・merge gate 救済時に PR Test plan へ転記される。`[AI確認-post-merge]` 直書きも同様。

---

## hook 動作検証 / バグ修正

**hook:** `pytest + stdin JSON pipe 契約テスト`。新 hook 作成時: `test_<hookname>_hook.py` に正常系・異常系。
**バグ修正:** `tests/regressions/test_fix_<N>.py` は `.feature` 既カバー分と重複させない（#2973）。ロック対象外。
**regression テスト命名規則（#3418）:** 新規追加の `tests/regressions/` バグ修正テストは `test_fix_<N>_<slug>.py`（slug: `[a-z0-9_]+`・例: `test_fix_3401_bypass_audit_path.py`）。pre-flight が新規のみ機械強制（既存は対象外）。

---

## 関連ドキュメント

- `docs/reference/test-plan-guide.md`
- `testing-framework.md`
- `pytest-bdd-workflow.md`
- `hooks.md`
