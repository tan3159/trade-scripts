"""Stop hook の Slack 通知ヘルパー（Issue #349・#1175・#2967）.

`.claude/hooks/on-stop.py` に同居していた Slack 通知（fire-and-forget）ロジックを
移設したもの。有効判定（config.json の `on-stop-slack` キー）は on-stop.py 側の
共通ゲーティングヘルパー（`_read_bool_hook_config`）が担うため、本モジュールは
実際の通知送信のみを扱う。

stdlib のみ使用。
"""

from __future__ import annotations

import subprocess


def notify_slack(webhook: str) -> None:
    """Fire-and-forget Slack 通知（Issue #1175）: Popen で投げっぱなしにし結果を待たない.

    curl 側の ``--max-time`` で自身をタイムアウトさせる。親（呼び出し元 hook）は
    wait せず速やかに返る。
    """
    try:
        subprocess.Popen(
            [
                "curl",
                "-s",
                "-X",
                "POST",
                webhook,
                "-H",
                "Content-Type: application/json",
                "-d",
                '{"text":"<!channel> Claude Code がユーザー入力待ちになりました。"}',
                "--max-time",
                "10",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        pass
