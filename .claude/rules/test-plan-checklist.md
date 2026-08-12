# Test Plan チェックリスト規約

> **注記:** 機械強制hook は `config.json` で enable した利用者のみ適用。

PR `## Test plan` 記述・TDD/BDD・フレームワーク選択を定義。詳細: docs/reference/test-plan-guide.md

**関連:** [`workflow.md`](./workflow.md)

---

## テストフレームワーク選択

**IMPORTANT: テスト作成前に `testing-framework.md` を確認する。**

| 対象 | フレームワーク | 配置場所 |
|------|--------------|---------|
| `projects/gas/*/` | Jest | `projects/gas/<project>/tests/` |
| `projects/py/*/` | pytest | `projects/py/<project>/tests/` |

`projects/` 配下は `tidd run-project-tests` が一元管理する。

---

## pytest-bdd による Executable Specification（#1283）

`tidd extract-feature <N>` で `.feature`・`step_defs` を同時生成する。

**feat/fix PR は `.feature`（+step_defs）または `test_*.py` が必須（#1962）。**
`projects/py/*/src/`・`projects/gas/*` 変更 PR に適用。
判定: 外部観測可能な振る舞い → `.feature`、内部 → `test_*.py`。
`.claude/hooks/` のみの変更は除外。適用範囲外は `testing-framework.md`。
`tidd test-plan` が未生成を検知して exit 1 でブロックする。

詳細: docs/reference/pytest-bdd-workflow.md

---

## TDD/BDD ワークフロー（feat/fix 必須）

1. Issue の `## 振る舞い` Gherkin を読む
2. テストを書く（pytest / Jest）
3. テストが RED であることを確認する
4. 実装する
5. 作ったテストファイルのみ GREEN であることを確認する（全件実行不要）
6. PR を作成する

**commit 順序は `require-red-first.py` が機械強制する。**
分割不能なら `<!-- allow-single-commit: <理由> -->` で bypass 可。
詳細: docs/reference/hooks.md#require-red-firstpy

公式ゲートは `tidd ai-review` 内の `tidd test-plan`。
CircleCI `nightly-tests` は docs/reference/test-plan-guide.md 参照。

`tests/` 直下はコミット後 `protect-tests.py` により変更不可になる。
コミット前に内容を十分確認すること。

---

## Test plan チェックリストの記述ルール

**Scenario は Issue の `## 振る舞い` に書く。**
Jest・pytest の項目は Test plan 不要（ファイル変更検出で自動実行）。
`## Test plan` 外の checklist は test-plan-check の対象外。

### Test plan 項目の種類

| 種別 | 書き方 | 動作 |
|------|--------|------|
| **AI確認** | `- [ ] [AI確認] workflow.md 記載追加` | APPROVE 後 Claude が検証し `- [x]` に更新 |
| **AI確認-post-merge** | `- [ ] [AI確認-post-merge] nightly-tests GREEN` | auto-merge 非妨害・cron が事後検証 |
| **手動** | `- [ ] [手動] ブラウザで確認` | APPROVE 後 exit 4 で人間へ |
| **未カバー**（禁止） | `- [ ] chezmoi status 差分なし` | `test-plan` が exit 1 でブロック |

**テスト自動実行の仕組み:**

| 変更ファイル | 自動実行 | GitHub Commit Status |
|------------|---------|---------------------|
| `projects/gas/<project>/` | `npx jest` | `jest/<project>` |
| `projects/py/<project>/` | `pytest` | `pytest/<project>` |
| `docs/`・`.md` のみ | スキップ | 投稿なし |

Commit Status に ❌ があれば APPROVE 後も自動マージを exit 4 でブロック。

**原則: AI がテスト可能なものは AI がテストする。**

---

## hook 動作検証（Issue #1294 Phase 1 以降）

**`pytest + stdin JSON pipe` 契約テストで検証。** 新規 hook は `test_<hookname>_hook.py` に書く。
実装例・旧パターン比較は docs/reference/test-plan-guide.md 参照。

---

## バグ修正ルール

- バグ修正は `test_fix_<N>.py` に再現テストを先に追加する
- `tests/` 直下は変更不可。`regressions/` は対象外

---

## 関連ドキュメント

- docs/reference/test-plan-guide.md — 詳細ガイド
- `testing-framework.md` — フレームワーク棲み分け
- docs/reference/pytest-bdd-workflow.md — pytest-bdd 詳細
- docs/reference/hooks.md — 全 hook リファレンス
