# trade_scripts

トレード用スクリプトの Python プロジェクト。

## 構成

```
projects/py/trade_scripts/
├── pyproject.toml        # uv プロジェクト定義（requires-python >= 3.11）
├── src/trade_scripts/    # 実装（src レイアウト）
└── tests/                # pytest（`@pytest.mark.target_<basename>` marker 必須）
```

- テスト規約: `.claude/rules/testing-framework.md`（Python → pytest）
- テストは `tidd run-project-tests` が変更ファイルから本プロジェクトを検出して自動実行し、
  ai-review が Commit Status（`pytest/trade_scripts`）を投稿する
- `pythonpath = ["src"]`（pyproject の pytest 設定）により未インストールでも bare `pytest` で動く

## 開発

```bash
cd projects/py/trade_scripts
uv sync --extra dev   # project ローカル .venv に dev 依存を導入
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
```

## モジュール追加の流れ（TiDD）

1. Issue を立てる（`## 振る舞い` Gherkin 必須・feat/fix の場合）
2. `src/trade_scripts/<module>.py` + `tests/test_<module>.py` をテスト先行で追加
3. pyproject の `[tool.pytest.ini_options] markers` に `target_<module>` を登録
