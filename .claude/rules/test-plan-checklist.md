# Test Plan チェックリスト規約

> **注記:** 以下の機械強制（`require-red-first` / `protect-tests` 等）は `hooks-config.json` で対応 hook を enable した利用者にのみ適用される。`copier copy` 直後の consumer はすべて default OFF のため、規約記述は参照目的。

PR の `## Test plan` セクションの記述ルール、TDD/BDD ワークフロー、テストフレームワーク選択を定義する。
Gherkin テンプレート・旧パターン比較・追加テスト観点ルールは [docs/reference/test-plan-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/test-plan-guide.md) 参照。

**関連:** [`.claude/rules/workflow.md`](./workflow.md)・[`.claude/rules/testing-framework.md`](./testing-framework.md)

---

## テストフレームワーク選択

**IMPORTANT: テストを作成する前に `.claude/rules/testing-framework.md` でフレームワークを確認すること。**

| 対象 | フレームワーク | 配置場所 |
|------|--------------|---------|
| `projects/gas/*/` | Jest | `projects/gas/<project>/tests/` |
| `projects/py/*/` | pytest | `projects/py/<project>/tests/` |

`projects/` 配下は `tidd run-project-tests` が一元管理する。詳細: `.claude/rules/testing-framework.md`

---

## pytest-bdd による Executable Specification（#1283）

`tidd extract-feature <N>` で `.feature` と `step_defs` skeleton を同時生成する。

**feat/fix PR は `.feature` と `step_defs` skeleton の両方が必須（Issue #1464・#1550）。**
`projects/py/*/src/`・`projects/gas/*`（tests 除く）を変更する feat/fix PR に適用。
`.claude/hooks/` のみの変更（hook 契約系 Issue）は除外（`## 振る舞い` 不要・契約テストで代替）。
`tidd test-plan` が未生成を検知して exit 1 でブロックする。

詳細: [docs/reference/pytest-bdd-workflow.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/pytest-bdd-workflow.md)

---

## TDD/BDD ワークフロー（feat/fix 必須）

1. Issue の `## 振る舞い` Gherkin を読む
2. テストを書く（pytest / Jest）
3. テストが RED であることを確認する
4. 実装する
5. 作ったテストファイルのみ GREEN であることを確認する（全件実行不要）
6. PR を作成する

**commit 順序は `require-red-first.py` が機械強制する（PR 作成時にブロック）。**
分割不能な場合は PR ボディに `<!-- allow-single-commit: <理由> -->` で bypass できる。
詳細: [docs/reference/hooks.md#require-red-firstpy](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/hooks.md#require-red-firstpy)

テストの公式ゲートは `tidd ai-review` 内の `tidd test-plan`。
CircleCI `nightly-tests` の詳細経緯は [docs/reference/test-plan-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/test-plan-guide.md) 参照。

`tests/` 直下のファイルをコミットした後は `protect-tests.py` により変更不可になる。
テストを書いてコミットする前に内容を十分確認すること。

---

## Test plan チェックリストの記述ルール

**Scenario は Issue の `## 振る舞い` セクションに書く（PR ボディへのコピーは不要）。**
bats・Jest・pytest の項目は Test plan に書かなくてよい（ファイル変更検出で自動実行）。
`## Test plan` 外の checklist（やること消化状況等）は test-plan-check の対象外。

### Test plan 項目の種類

| 種別 | 書き方 | 動作 |
|------|--------|------|
| **AI確認** | `- [ ] [AI確認] workflow.md に記載が追加されていること` | APPROVE 後に Claude が検証して `- [x]` に更新 |
| **AI確認-post-merge** | `- [ ] [AI確認-post-merge] nightly-tests が GREEN になること` | auto-merge を妨げない。マージ後に cron が検証 |
| **手動** | `- [ ] [手動] ブラウザで確認する` | APPROVE 後に exit 4 で人間に委ねる |
| **未カバー**（禁止） | `- [ ] chezmoi status で差分が出ない` | `tidd test-plan` が exit 1 でブロック |

**テスト自動実行の仕組み:**

| 変更ファイル | 自動実行 | GitHub Commit Status |
|------------|---------|---------------------|
| `projects/gas/<project>/` | `npx jest` | `jest/<project>` |
| `projects/py/<project>/` | `pytest` | `pytest/<project>` |
| `docs/`・`.md` のみ | スキップ | 投稿なし |

GitHub Commit Status に ❌（failure/error）がある場合、`tidd ai-review` は APPROVE レビューを投稿するが自動マージを exit 4 でブロックする（Issue #831）。

**原則: AI がテスト可能なものは AI がテストする。人間はどうしても AI にできないものだけやる。**

---

## hook 動作検証（Issue #1294 Phase 1 以降）

**hook の動作検証は `pytest + stdin JSON pipe 契約テスト` で行う。**

```python
def _run_hook(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_hook_path())],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
```

新しい hook を作成するとき: `test_<hookname>_hook.py` に正常系・異常系の契約テストを書く。
旧パターン（廃止済み bash pipe）との比較は [docs/reference/test-plan-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/test-plan-guide.md) 参照。

---

## バグ修正ルール

- バグ修正時は必ず `tests/regressions/test_fix_<N>.py` にバグ再現テストを追加してから実装する
- `tests/` 直下の既存テストファイルは変更不可（`protect-tests.py`）。`regressions/` はロック対象外

---

## 関連ドキュメント

- [docs/reference/test-plan-guide.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/test-plan-guide.md) — 詳細ガイド（Gherkin テンプレート・旧パターン比較・追加観点ルール）
- [`.claude/rules/testing-framework.md`](./testing-framework.md) — フレームワーク棲み分け
- [docs/reference/pytest-bdd-workflow.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/pytest-bdd-workflow.md) — pytest-bdd 詳細
- [docs/reference/hooks.md](https://github.com/being-gaia-plan/ai-dev-handbook/blob/main/docs/reference/hooks.md) — 全 hook リファレンス
