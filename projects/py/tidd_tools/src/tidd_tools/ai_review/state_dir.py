"""STATE_DIR 解決の共通ヘルパ（#2529）.

``_state_dir(pr_num)`` の重複実装を 1 箇所に集約し、
``STATE_DIR`` env var の basename が解決対象 PR 番号と一致するかどうかを検証する。

3 分岐ルール
-----------
``STATE_DIR`` の basename:

``pr-<pr_num>``（一致）
    そのまま採用（本番の正常系）。

``pr-<M>``（``M != pr_num``）
    :class:`StateDirMismatchError` を送出する（他 PR のキャッシュを汚す唯一の経路）。

``pr-*`` にマッチしない
    そのまま採用（テスト・手動 override）。

``STATE_DIR`` 未設定
    デフォルトの ``~/.cache/tidd/ai-reviewer/pr-<pr_num>`` を返す。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from tidd_tools.shared.paths import cache_dir as _cache_dir

_PR_BASENAME_RE = re.compile(r"^pr-(\d+)$")


class StateDirMismatchError(ValueError):
    """``STATE_DIR`` の basename が解決対象 PR 番号と一致しない場合に送出する."""


def resolve_state_dir(pr_num: str) -> Path:
    """``STATE_DIR`` を解決して ``Path`` として返す.

    Parameters
    ----------
    pr_num:
        対象 PR 番号（文字列）。

    Returns
    -------
    Path
        解決済みのディレクトリパス。

    Raises
    ------
    StateDirMismatchError
        ``STATE_DIR`` が ``pr-<M>``（``M != pr_num``）形式の場合。
    """
    raw = os.environ.get("STATE_DIR")
    if not raw:
        return _cache_dir() / "ai-reviewer" / f"pr-{pr_num}"

    path = Path(raw)
    basename = path.name
    m = _PR_BASENAME_RE.match(basename)
    if m is not None:
        found_num = m.group(1)
        if found_num != pr_num:
            raise StateDirMismatchError(
                f"STATE_DIR のベース名 '{basename}' は PR #{found_num} を指しているが、"
                f"解決対象は PR #{pr_num} です。"
                f"テスト実行中に親プロセスの STATE_DIR を継承した可能性があります。"
                f"（STATE_DIR={raw!r}、期待: pr-{pr_num}）"
            )
    # basename が pr-* にマッチしない場合（テスト・手動 override）はそのまま採用
    return path


# ── backend-unavailable 証跡フラグ（#3629）─────────────────────────────
#
# exit 3（全バックエンド利用不可）の証跡を ``pr-<N>/backend-unavailable``
# に残し、Claude フォールバックレビュー（ai-fallback-reviewer）の起動を
# hook（.claude/hooks/block-unauthorized-fallback-review.py）で機械強制する。
# escalated フラグと同流儀（作成・削除は失敗しても握りつぶす）。

_BACKEND_UNAVAILABLE_FLAG = "backend-unavailable"


def backend_unavailable_flag_path(state_dir: Path) -> Path:
    """``backend-unavailable`` フラグファイルのパスを返す."""
    return state_dir / _BACKEND_UNAVAILABLE_FLAG


def write_backend_unavailable_flag(state_dir: Path, reason: str) -> None:
    """exit 3 の証跡フラグを作成する（内容は判定理由の 1 行テキスト）."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        backend_unavailable_flag_path(state_dir).write_text(reason + "\n", encoding="utf-8")
    except OSError:
        pass


def remove_backend_unavailable_flag(state_dir: Path) -> None:
    """stale な backend-unavailable フラグを削除する（冪等）."""
    flag = backend_unavailable_flag_path(state_dir)
    try:
        if flag.is_file():
            flag.unlink()
    except OSError:
        pass
