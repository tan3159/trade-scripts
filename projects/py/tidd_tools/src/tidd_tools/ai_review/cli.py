"""ai-review サブコマンド CLI 登録（旧 ai-review.sh の argv パース部）.

``tidd_tools/__main__.py`` の entry_points 経由で discovery されるため、本モジュールには
:class:`argparse._SubParsersAction` を引数に取る ``register`` を実装する。
"""

from __future__ import annotations

import argparse
import sys

from tidd_tools.ai_review.subcommands import (
    consensus_verdict,
    continue_with_verdict,
    post_comment,
    post_primary_review_comment,
)
from tidd_tools.ai_review.tokens import load_ai_review_secrets, load_mise_env
from tidd_tools.shared.cli import add_common_flags


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "ai-review",
        help="PR の AI レビューを実行する（ai-review.sh の Python 版）",
        description=(
            "GitHub PR に対して agy / codex バックエンドでレビューを実行し、結果を投稿する。"
            "旧 scripts/ai-review.sh を 1:1 で移植したエントリポイント。"
        ),
    )
    add_common_flags(parser)
    parser.add_argument(
        "pr_num",
        nargs="?",
        help=(
            "PR 番号（通常レビュー経路では必須。"
            "--post-comment/--continue-with-verdict/--post-primary-review/"
            "--verify-ai-confirm/--consensus-verdict を指定する場合は省略可）"
        ),
    )
    parser.add_argument("attempt", nargs="?", default=1, type=int, help="試行回数（デフォルト 1）")
    parser.add_argument(
        "--post-comment",
        dest="post_comment",
        nargs=2,
        metavar=("PR_NUM", "BODY"),
        default=None,
        help="ボットアカウントで PR にコメントを投稿する（Agent tool フォールバック用）",
    )
    parser.add_argument(
        "--reviewer",
        dest="reviewer",
        default=None,
        metavar="NAME",
        help=(
            "--post-comment のフッターに使う Reviewer 名を上書きする（Issue #2660）。"
            "指定時は STATE_DIR/backend-name を無視してこの値をフッターに使う。"
            "空文字列はエラー。--no-reviewer-footer と同時指定不可。"
        ),
    )
    parser.add_argument(
        "--no-reviewer-footer",
        dest="no_reviewer_footer",
        action="store_true",
        default=False,
        help=(
            "--post-comment でフッターを付加しない（Issue #2660）。"
            "consensus 判定コメントなど特定 backend に紐づかない投稿用。"
            "--reviewer と同時指定不可。"
        ),
    )
    parser.add_argument(
        "--continue-with-verdict",
        dest="continue_with_verdict",
        nargs=2,
        metavar=("VERDICT", "PR_NUM"),
        default=None,
        help="Agent tool フォールバック完了後に VERDICT を渡して後続処理を実行する",
    )
    parser.add_argument(
        "--post-primary-review",
        dest="post_primary_review",
        metavar="PR_NUM",
        default=None,
        help=(
            "primary review コメントを secondary consensus チェックより先に投稿する（Issue #2314）。"
            "parser critical PR の STEP 5.5 実行前に呼ぶ。"
        ),
    )
    parser.add_argument(
        "--verify-ai-confirm",
        dest="verify_ai_confirm",
        metavar="PR_NUM",
        default=None,
        help=(
            "APPROVE 済み PR の [AI確認] 項目を Claude API Tool Calling で自律検証する（#1246）。"
            "全項目が verified なら PR body を [x] 更新して --continue-with-verdict APPROVE を実行する。"
        ),
    )
    parser.add_argument(
        "--stop-before-merge",
        dest="stop_before_merge",
        action="store_true",
        default=False,
        help=(
            "verdict 確定・レビュー本文保存まで実行してマージ前に返す（Issue #2645）。"
            "parser critical PR の secondary consensus 実行前に使用する。"
            "終了コード: APPROVE=10, REQUEST_CHANGES=11, テスト status gate 中断=5, 全バックエンド利用不可=3。"
        ),
    )
    parser.add_argument(
        "--consensus-verdict",
        dest="consensus_verdict",
        nargs=4,
        metavar=("PR_NUM", "SECONDARY_VERDICT", "SECONDARY_ISSUES_JSON", "ATTEMPT"),
        default=None,
        help=(
            "parser critical PR の secondary consensus 判定を行う（Issue #2657）。"
            "primary APPROVE + secondary REQUEST_CHANGES の consensus 不一致を処理する。"
            "終了コード: 0=consensus APPROVE, 1=リトライ継続, 2=エスカレーション。"
        ),
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    # ai-review 用 secrets（APP_ID / INSTALLATION_ID / PRIVATE_KEY_CONTENT / GH_TOKEN）は
    # 環境変数のみから読み込む（Bitwarden フォールバックは廃止・#3182/#3212）。
    # メインフロー（core.main）だけでなく `--post-comment` /
    # `--continue-with-verdict` サブコマンドも同じ secrets を必要とするため、
    # ここで一括して読み込む（core.main 側の重複呼び出しは no-op になる
    # ように tokens.load_ai_review_secrets が idempotent）。
    import os as _os

    if _os.environ.get("AI_REVIEW_SKIP_SECRETS_LOAD") != "1":
        try:
            # repo_root はリポジトリ宣言型設定の解決に使用する。
            from tidd_tools.shared import git_client
            from tidd_tools.shared.errors import GitCommandError

            try:
                repo_root = git_client.rev_parse_show_toplevel()
            except GitCommandError:
                repo_root = None
            load_ai_review_secrets(repo_root=repo_root)
        except Exception as exc:  # noqa: BLE001
            # secrets ロード失敗はフォールバックに任せて続行する
            print(f"WARN: secrets ロード失敗: {exc}", file=sys.stderr)

    if args.post_comment is not None:
        pr_num, body = args.post_comment
        return post_comment(
            str(pr_num),
            str(body),
            reviewer=args.reviewer,
            no_reviewer_footer=args.no_reviewer_footer,
        )
    if args.continue_with_verdict is not None:
        # 非対話シェルでは mise の activate hook が動かないため、
        # --continue-with-verdict のトークン解決前に [env] を読み込む（#4092）。
        load_mise_env()
        verdict, pr_num = args.continue_with_verdict
        return continue_with_verdict(str(verdict), str(pr_num))
    if args.post_primary_review is not None:
        return post_primary_review_comment(str(args.post_primary_review))
    if args.consensus_verdict is not None:
        pr_num, secondary_verdict, secondary_issues_json, attempt_str = args.consensus_verdict
        return consensus_verdict(str(pr_num), str(secondary_verdict), str(secondary_issues_json), int(attempt_str))
    if args.verify_ai_confirm is not None:
        from tidd_tools.ai_review.subcommands import verify_ai_confirm_command

        return verify_ai_confirm_command(str(args.verify_ai_confirm))
    if not args.pr_num:
        print("ERROR: PR番号を引数で指定してください。", file=sys.stderr)
        return 1
    # Issue #3892: core モジュールの import コストを register() 時点（`tidd <任意コマンド>` の
    # 起動全て）から実際にメインフローへ到達する時点まで遅延する。
    from tidd_tools.ai_review.core import main as core_main

    return core_main(str(args.pr_num), int(args.attempt or 1), stop_before_merge=args.stop_before_merge)
