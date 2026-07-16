# set-reviewer

PRレビューバックエンドを切り替えるスキル。
選択内容は `~/.claude/ai-reviewer` ファイルに保存され、次回以降の `tidd_tools ai-review` 実行に引き継がれる。

**引数:** `auto` / `agy` / `agy-sonnet` / `codex`

| 値 | 動作 |
| --- | --- |
| `auto` | フォールバックチェーン全体（agy(gemini) → agy(sonnet) → codex → claude → 終了コード 3）。デフォルト |
| `agy` | agy(gemini) 固定（フォールバックなし） |
| `agy-sonnet` | agy(sonnet) 固定（フォールバックなし） |
| `codex` | codex 固定（失敗時は claude へフォールバック） |

無効化されたバックエンドは選択できない（`AI_REVIEW_AGY_ENABLED=0` / `AI_REVIEW_CODEX_ENABLED=0` による無効化）。

## 動作

1. 引数のバックエンド名を検証する
2. 無効化されたバックエンドが指定された場合はエラーにする
3. `.claude/ai-reviewer` ファイルに書き込む
4. 現在の設定を確認して報告する

## 手順

### STEP 1: 引数の検証

引数が `auto` / `agy` / `agy-sonnet` / `codex` 以外の場合は以下のエラーを表示して終了する:

```text
エラー: 有効なバックエンド: auto / agy / agy-sonnet / codex
現在の設定は変更されていません。
```

引数が未指定の場合は現在の設定を表示する:

```bash
REVIEWER_FILE="${HOME}/.claude/ai-reviewer"
if [[ -f "$REVIEWER_FILE" ]]; then
  current=$(cat "$REVIEWER_FILE")
  echo "現在のバックエンド: ${current}"
else
  echo "現在のバックエンド: auto（デフォルト。~/.claude/ai-reviewer は未作成）"
fi
```

無効化されたバックエンドが指定された場合（例: `AI_REVIEW_AGY_ENABLED=0` 環境下で `agy` を指定）:

```bash
# AI_REVIEW_AGY_ENABLED=0 が設定されている場合
if [[ "${AI_REVIEW_AGY_ENABLED:-1}" == "0" ]] && [[ "$backend" == "agy" || "$backend" == "agy-sonnet" ]]; then
  echo "エラー: ${backend} は現在の環境では無効です（AI_REVIEW_AGY_ENABLED=0）"
  echo "現在の設定は変更されていません。"
  exit 1
fi
# AI_REVIEW_CODEX_ENABLED=0 が設定されている場合
if [[ "${AI_REVIEW_CODEX_ENABLED:-1}" == "0" ]] && [[ "$backend" == "codex" ]]; then
  echo "エラー: ${backend} は現在の環境では無効です（AI_REVIEW_CODEX_ENABLED=0）"
  echo "現在の設定は変更されていません。"
  exit 1
fi
```

### STEP 2: ファイルへの書き込み

```bash
REVIEWER_FILE="${HOME}/.claude/ai-reviewer"
mkdir -p "${HOME}/.claude"
printf '%s\n' "<バックエンド名>" > "$REVIEWER_FILE"
echo "レビューバックエンドを <バックエンド名> に設定しました。"
echo "設定ファイル: ${REVIEWER_FILE}"
```

### STEP 3: 確認メッセージの表示

設定完了後、以下を表示する:

```text
レビューバックエンドを <バックエンド名> に設定しました。
次回 uv run --project projects/py/tidd_tools python -m tidd_tools ai-review を実行すると <バックエンド名> でレビューが行われます。
（AI_REVIEW_BACKEND 環境変数が設定されている場合はそちらが優先されます）
```

## 補足

- `~/.claude/ai-reviewer` はユーザーレベルの設定ファイル（Git 管理対象外）
- `AI_REVIEW_BACKEND` 環境変数が設定されている場合は環境変数が優先される
- 設定の確認は `cat ~/.claude/ai-reviewer` または引数なしで `/set-reviewer` を実行
- ステータスラインに現在のバックエンドが `[reviewer: <名前>]` 形式で表示される
- `AI_REVIEW_AGY_ENABLED=0` / `AI_REVIEW_CODEX_ENABLED=0` で特定バックエンドを無効化できる
