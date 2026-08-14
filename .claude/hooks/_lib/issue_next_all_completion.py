"""issue-next-all の終端結果を検証する共通契約."""

from __future__ import annotations

from collections.abc import Mapping


class CompletionContractError(ValueError):
    """終端結果から完了状態を証明できない場合のエラー."""


def validate_completion(result: Mapping[str, object]) -> str:
    """completed / park / skip の終端結果を検証して状態を返す."""
    status = result.get("status")
    if status not in {"completed", "park", "skip"}:
        raise CompletionContractError(
            "完了状態を取得できません（タイムアウトまたは応答欠落）"
        )
    if not result.get("issue"):
        raise CompletionContractError("完了状態に Issue 番号がありません")
    if status == "completed":
        if not result.get("pull_request") or result.get("merge_state") != "MERGED":
            raise CompletionContractError("completed には PR 番号と MERGED が必要です")
    elif not result.get("reason"):
        raise CompletionContractError(f"{status} には理由が必要です")
    return str(status)
