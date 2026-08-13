# STEP 6: main sync 失敗時の復旧手順（Issue #2670）

`git pull origin main --ff-only` の終了コードを必ず確認する。出力を `tail` 等に通して流し見しない。
exit 0 なら次へ進む（`step6-cleanup-done` は `tidd cleanup-merged-branch` が自己記録するため手動 mark 不要・#3556）。
exit 0 以外の場合は必ず「main sync 失敗: <理由>」を出力してから以下のとおり対処する
（black-hole させて次 Issue へ進んではならない）:

- **未追跡ファイル衝突**（`error: The following untracked working tree files would be overwritten by merge` を含む）の場合: エラー出力に列挙された衝突ファイルパスをすべて出力してから、削除せず退避して再試行する:

```bash
mkdir -p /tmp/main-sync-conflict-issue-<N>
mv <衝突ファイルパス...> /tmp/main-sync-conflict-issue-<N>/
git pull origin main --ff-only
```

  再試行が exit 0 になったら `git rev-parse HEAD` と `git rev-parse origin/main` が一致することを確認して次へ進む（`step6-cleanup-done` は `tidd cleanup-merged-branch` が自己記録するため手動 mark 不要・#3556）。退避したファイルは削除せず `/tmp/main-sync-conflict-issue-<N>/` に残す。
- **未追跡ファイル衝突以外の失敗**（コンフリクト・ネットワーク断など）の場合: 自動修復せず、後続の所要時間サマリ出力・Issue チェックボックス更新のあとで「main sync 失敗: ローカル main が origin/main と不一致（`<エラーメッセージ>`）」を Issue にコメント投稿してから次の処理へ進む。
