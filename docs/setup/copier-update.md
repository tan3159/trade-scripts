# 上流テンプレート反映（copier update）の運用

本リポジトリは ai-dev-handbook の copier テンプレート（`templates/workflow`）の consumer。
`.copier-answers.yml` は **git URL 参照 + タグ固定**（`_src_path: git+https://github.com/being-gaia-plan/ai-dev-handbook.git`・`_commit: v<YYYY.MM.DD>[.<n>]`）で管理する（Issue #4・upstream #2222）。

## いつ update するか

- **通知駆動**: SessionStart hook（`notify-copier-staleness.py`）が `.copier-answers.yml` の `_commit` と
  上流最新タグを比較し、drift があればセッション開始時に stderr で通知する。通知を見たら update する
- 上流に取り込みたい修正が入ったと分かっているときは通知を待たず実行してよい

## どうやって update するか

```bash
cd <repo>  # クリーンな作業ツリーで実行する（dirty だと copier が拒否する）
tidd copier-update --dry-run   # 変更内容を先に確認
tidd copier-update             # 適用（内部で copier update --defaults --trust）
```

**注意:**

- **`GH_TOKEN` を付けずに実行する。** 上流 ai-dev-handbook は private のため、
  `GH_TOKEN=$(gh auth token -u tan3159)` を付けると tan3159 のトークンが優先されて
  `Repository not found` で失敗する（アクセス権のあるデフォルトアカウントの認証を使う）
- untracked ファイルがあると `Destination repository is dirty` で拒否される。commit または `git stash -u` してから実行する
- update 後は diff を確認し、Issue を立てて PR フローでコミットする（NO TICKET NO WORK）

## 手動で特定タグに合わせる場合

```bash
copier update --defaults --trust --vcs-ref v<YYYY.MM.DD>
```

## トラブルシューティング

| 症状 | 原因・対処 |
|------|-----------|
| `Cannot update because cannot obtain old template references` | `.copier-answers.yml` に `_commit` が無い。git URL 参照で `copier copy --overwrite` をやり直す |
| `Repository not found` | `GH_TOKEN` が上流にアクセス権のないアカウントを指している。外して実行 |
| `Destination repository is dirty` | 作業ツリーを commit / stash してから実行 |
| downgrade エラー | `_commit` が最新タグより新しい（dev commit）。`--vcs-ref` で明示するか上流の新タグを待つ |

## 関連

- `docs/setup/tidd-tools-setup.md` — tidd_tools 本体の導入・タグ更新
- 上流: `docs/setup/copier-workflow-adoption.md`（ai-dev-handbook）
