# CI（GitHub Actions）

**public リポジトリ**のため GitHub Actions を採用する（無料・無制限。ai-dev-handbook 本体は CircleCI）。
ローカル `tidd ai-review` のテスト gate に加え、環境差異・push 忘れによる main 破壊を CI で検知する。

## 構成（`.github/workflows/ci.yml`）

| 項目 | 内容 |
|------|------|
| トリガー | `push`（main）/ `pull_request` |
| job | `python`: uv sync → pytest → ruff check → ruff format --check → mypy（`projects/py/trade_scripts`） |
| Python | 3.11（`astral-sh/setup-uv` + cache） |

## public repo セキュリティ方針

- **`permissions: contents: read`** — workflow 全体をデフォルト read-only にする
- **`pull_request_target` 不使用** — fork PR に secrets が露出する主要経路を塞ぐ
- **secrets 依存 job なし** — fork PR でも安全に実行できる（`pull_request` トリガーでは secrets は fork に渡されない）
- **action は full SHA pin** — タグ改ざん（supply chain）対策。コメントでバージョンを併記

## プロジェクト追加時

`projects/py/<project>` を増やしたら `ci.yml` の job を追加する（または matrix 化する）。

## 関連

- `docs/setup/pre-commit.md` — commit 段階のローカル検知（第一防衛線）
- `docs/setup/repo-settings.md` — GitHub 側設定
