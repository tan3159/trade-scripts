"""tidd_tools の共通レイヤ.

Phase 1 (#1050) + Phase 2-A (#1053) で確立した共通基盤。後続 Phase の
サブコマンド（issue-next-state / loop-error-log / check-pr-conflicts /
analyze-performance / ai-review 等）が共有して使う前提。

| モジュール | 責務 |
|-----------|------|
| `cli` | `--verbose / --dry-run / --json` の共通フラグ追加 |
| `errors` | カスタム例外（`ToolError` ベース + `GhCommandError` / `GitCommandError` / `SubprocessTimeoutError`） |
| `gh_client` | gh CLI ラッパー（pr_view / pr_diff_files / pr_edit_body / issue_create 等） |
| `git_client` | git CLI ラッパー（rev_parse / diff / log 等） |
| `logging_setup` | `--verbose` 段階に応じた logging 設定 |
| `paths` | OS 別ディレクトリ解決（`platformdirs` ベース） |
| `recursion` | `_TIDD_TOOLS_RECURSION_GUARD` の判定と子プロセスへの注入 |
| `subprocess_runner` | `shell=False` 強制・タイムアウト統一の subprocess ラッパー |

Phase 1 で導入した `tidd_tools.shared.gh` モジュールは Phase 2-A で
`gh_client` に統合された。互換性のため `test_plan.py` 内で
`from tidd_tools.shared import gh_client as gh` というエイリアスを使う。
"""
