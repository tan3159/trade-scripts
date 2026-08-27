# issue-next の起動元別実装委譲設定

`tidd resolve-issue-next-agent` は、実行環境に応じた issue-next の実装担当とモデルを機械的に解決する。

| 起動元 | agent_type | model |
|---|---|---|
| Claude Code | `issue-implementer` | `sonnet` |
| Codex | `issue_implementer` | `luna` |

実装委譲（`impl-delegation` / `impl-backend`）とレビュー設定（`issue-next-review-backend` / `issue-next-review-priority`）は独立して管理する。未対応の起動元は手動 CLI を呼び出さず exit code 2 で終了する。
