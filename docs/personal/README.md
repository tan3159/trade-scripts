# docs/personal — 個人設定ディレクトリ

このディレクトリは、各コントリビューターが自分の Claude Code 設定を置くための場所です。

## 運用ルール

### 1. 個人 CLAUDE.md の配置場所

```
docs/personal/<GitHub username>/CLAUDE.md
```

`copier copy` / `copier update` は `docs/personal/user/CLAUDE.md`
（共通ブリッジ）を配布します。複数人で使う場合は `personal_dir_name` を各自の
GitHub username に変更してください。

### 2. root CLAUDE.md はローカル管理

root の `CLAUDE.md` は `.gitignore` で追跡対象外になっています。
各自が自分のローカル環境で以下の 1 行を書いてください:

```
@docs/personal/<あなたの GitHub username>/CLAUDE.md
```

例 (user 名が `alice` の場合):

```
@docs/personal/alice/CLAUDE.md
```

### 3. セットアップ手順（初回）

```bash
# リポジトリ root で実行
echo "@docs/personal/<あなたの GitHub username>/CLAUDE.md" > CLAUDE.md
```

### 4. `docs/personal/<username>/CLAUDE.md` の内容

`docs/personal/<username>/CLAUDE.md` は上流テンプレートが配布する共通ブリッジで、
`.rulesync/rules/overview.md`（行動原則・セキュリティ原則・TiDD ワークフローのコア部）を
import する 1 行のみを持ちます。

**consumer 独自の記述はブリッジに書かず**、repo-specific マーカー
（AGENTS.md の `<!-- BEGIN: repo-specific -->` 〜 `<!-- END: repo-specific -->` 内）に
書いてください。このマーカー内は `copier update` で上書きされません。

### 5. `copier update` 時の挙動

既存の `docs/personal/<user>/CLAUDE.md` は `_skip_if_exists` により `copier update` で
**上書きされません**。既存の personal ファイルを保持したまま、共通ブリッジの配布だけを
受けられます。

### 6. 背景

- 関連 Issue: #2355（root CLAUDE.md ローカル管理）・#3435（Claude Code 側の二重ロード解消）・#3714（本配布）
