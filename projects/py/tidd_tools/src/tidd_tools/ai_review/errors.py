"""ai-review 専用例外."""

from __future__ import annotations

from tidd_tools.shared.errors import ToolError


class AiReviewError(ToolError):
    """ai-review 系で送出する例外のベース."""


class InvalidJwtKeyError(AiReviewError):
    """JWT 署名キー（PEM）の読み込み・パースに失敗したときに送出する."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"invalid JWT private key: {reason}")
        self.reason = reason
