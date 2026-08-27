"""tidd_tools.shared の共通例外型.

カスタム例外はサブコマンド側で `except ToolError:` のように
ベース型でまとめて捕捉できる構造にする。
"""

from __future__ import annotations


class ToolError(RuntimeError):
    """`tidd_tools.shared` 系の例外ベース."""


class SubprocessTimeoutError(ToolError):
    """`subprocess_runner.run` のタイムアウト時に送出する."""

    def __init__(self, command: list[str], timeout: float) -> None:
        super().__init__(
            f"subprocess timeout after {timeout:.1f}s: {' '.join(command)}",
        )
        self.command = command
        self.timeout = timeout


class GhCommandError(ToolError):
    """`gh_client` 経由の gh CLI 呼び出しが非ゼロ終了したときに送出する.

    注: `Exception.args` は標準 ``__str__`` がフォーマットに使う予約属性のため、
    呼び出し引数は ``command_args`` に格納する。
    """

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"gh {' '.join(args)} failed (exit={returncode}): {stderr.strip()}",
        )
        self.command_args = args
        self.returncode = returncode
        self.stderr = stderr


class DiffTooLargeError(GhCommandError):
    """PR diff が GitHub API の diff media type 上限（20000 行）を超えたときに送出する（Issue #3743）.

    ``gh pr diff`` は 20000 行を超える diff に対して ``HTTP 406: the diff exceeded the
    maximum number of lines`` で失敗する。これはクォータ枯渇（時間経過で回復する一時的
    障害）や恒久的な環境破損とは異なる「PR 構造上の制約」のため、呼び出し元（ai-review 等）が
    原因を区別できるよう ``GhCommandError`` のサブクラスとして送出する。

    ``__init__`` は ``GhCommandError`` を継承する（``args`` / ``returncode`` / ``stderr``）。
    """


class GitCommandError(ToolError):
    """`git_client` 経由の git CLI 呼び出しが非ゼロ終了したときに送出する.

    注: `Exception.args` は標準 ``__str__`` がフォーマットに使う予約属性のため、
    呼び出し引数は ``command_args`` に格納する。
    """

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"git {' '.join(args)} failed (exit={returncode}): {stderr.strip()}",
        )
        self.command_args = args
        self.returncode = returncode
        self.stderr = stderr
