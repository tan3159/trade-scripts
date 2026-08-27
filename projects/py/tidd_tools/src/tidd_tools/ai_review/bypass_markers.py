"""PR ボディ中の allow-* バイパスマーカー検出（sanitize 除去前・Issue #4147）.

`sanitize_untrusted_text()`（Issue #1845）は HTML コメントをプロンプトインジェクション
対策として丸ごと除去する。しかし `<!-- allow-xxx: <理由> -->` 系の正規バイパスマーカー
（`require-issue-id-in-pr-title.py` の `allow-no-issue-id` 等）も同じ HTML コメント構文で
あるため、sanitize 後の PR body だけを AI レビュアーに渡すと「マーカーが存在しない」と
常に誤認されてしまう（Issue #4147・実例: PR #4133）。

本モジュールは sanitize **前** の生の PR body からマーカーの有無（真偽値相当の一覧）のみを
判定してレビュアー向けの文言を組み立てる。マーカーの理由文（自由記述・非信頼テキスト）は
一切レビュアーへ渡さない（プロンプトインジェクション対策の維持）。
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

# レビュープロンプトへ「存在の有無」のみを伝える対象マーカー一覧（Issue #4147）。
# 各 hook の docstring に記載された正規バイパスマーカーと揃える
# （require-no-issue-id-in-pr-title.py・require-closes-in-pr-body.py・require-red-first.py・
# protect-tests.py・detect-ai-confirm-misuse.py・validate-issue.py・
# block-subagent-size-marker.py 参照）。
#
# 注意（Issue #4152 レビュー指摘）: `no-doc-update` は `pre_flight.py` が **コミット
# メッセージ**から読むマーカーであり、本モジュールが走査する PR body には現れない。
# ここに含めると、コミットメッセージ側で正当に付与された `no-doc-update` があっても
# 本セクションは常に「検出されたバイパスマーカー: なし」と表示してしまい、AI レビュアーに
# valid bypass が無いと誤伝達する（false negative）。そのため対象外とする。
BYPASS_MARKER_NAMES: tuple[str, ...] = (
    "allow-no-issue-id",
    "allow-no-closes",
    "allow-single-commit",
    "allow-test-update",
    "allow-test-skip",
    "allow-secret-pattern",
    "allow-ai-confirm-keyword",
    "allow-xxl",
    "allow-large-pr",  # allow-xxl の旧名（後方互換・Issue #3994）
)


def _load_override_markers_lib(repo_root: Path) -> types.ModuleType:
    """`.claude/hooks/_lib/override_markers.py` を動的 import する.

    `tidd_tools.tdd_check._load_lib` と同型（Issue #2895 踏襲）。マーカーの
    有効書式判定（理由必須・`-->` を跨がない等）を hook 側と単一ソース化するため、
    ここで判定ロジックを再実装しない。
    """
    lib_dir = repo_root / ".claude" / "hooks" / "_lib"
    if not lib_dir.is_dir():
        raise FileNotFoundError(f"_lib ディレクトリが見つかりません: {lib_dir}")
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    return importlib.import_module("override_markers")


def detect_bypass_markers(pr_body: str, *, repo_root: Path | None = None) -> list[str]:
    """sanitize 前の PR body から存在する正規バイパスマーカー名の一覧を返す.

    マーカーの理由文（自由記述）は一切含めない。存在チェックのみ行う。
    `.claude/hooks/_lib/override_markers.py` が見つからない環境（配布先での
    未セットアップ等）では空リストを返す（フォールセーフ）。
    """
    if not pr_body:
        return []
    root = repo_root or Path.cwd()
    try:
        lib = _load_override_markers_lib(root)
    except (FileNotFoundError, ImportError):
        return []
    found: list[str] = []
    for name in BYPASS_MARKER_NAMES:
        if lib.has_override_marker(pr_body, name):
            found.append(name)
    return found


def build_bypass_marker_section(pr_body: str, *, repo_root: Path | None = None) -> str:
    """レビュープロンプトへ埋め込む「マーカー存在」セクションを構築する.

    マーカー名のみを伝え、理由文（`<!-- allow-xxx: <理由> -->` の `<理由>` 部分）は
    渡さない（プロンプトインジェクション対策を維持・Issue #1845 の意図を継続する）。
    """
    found = detect_bypass_markers(pr_body, repo_root=repo_root)
    header = (
        "\n## PR バイパスマーカーの検出結果（Issue #4147）\n"
        "以下は PR ボディの sanitize 前テキストを機械的に走査した結果です"
        "（マーカーの理由文は含みません）。「マーカーがない」という指摘をする前に必ず確認してください。\n"
    )
    if not found:
        return f"{header}検出されたバイパスマーカー: なし\n"
    lines = "\n".join(f"- {name}: 存在する" for name in found)
    return f"{header}{lines}\n"
