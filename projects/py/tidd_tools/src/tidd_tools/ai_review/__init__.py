"""ai-review サブコマンドパッケージ（旧 scripts/ai-review.sh / 2,718 行の Python 移植）.

旧 bash 実装の責務を以下のサブモジュールに分割している:

- :mod:`tidd_tools.ai_review.cli`           argparse 引数処理（``register`` エントリ）
- :mod:`tidd_tools.ai_review.core`          メインフロー（``main()``）
- :mod:`tidd_tools.ai_review.jwt`           GitHub App JWT 生成
- :mod:`tidd_tools.ai_review.tokens`        installation token 取得 + gh auth token フォールバック
- :mod:`tidd_tools.ai_review.prompts`       Issue Gherkin 取得 + プロンプト生成
- :mod:`tidd_tools.ai_review.backends`      agy / codex バックエンド + フォールバックチェーン
- :mod:`tidd_tools.ai_review.verdict`       VERDICT パース + 指摘事項抽出・分析
- :mod:`tidd_tools.ai_review.quota`         クォータ超過グローバルキャッシュ
- :mod:`tidd_tools.ai_review.reviewdog`     rdjson 変換 + reviewdog 実行
- :mod:`tidd_tools.ai_review.escalation`    エスカレーションコメント + Slack 通知
- :mod:`tidd_tools.ai_review.timing`        統一日誌 verdict 記録（旧 timing.json 保存は #2936 で撤去）
- :mod:`tidd_tools.ai_review.post_review`   GitHub レビュー投稿 + リトライ
- :mod:`tidd_tools.ai_review.orphan`        AI review 中断検知（Issue #1232）
"""
