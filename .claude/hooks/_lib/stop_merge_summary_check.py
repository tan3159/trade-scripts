"""Stop hook の merge-summary 転記チェック（Issue #2466・#2751・#2752・#2967）.

`.claude/hooks/on-stop.py` に同居していたロジックをそのまま移設したもの
（Issue #2967 やること: timing 系 #2933 群が今後この領域を再設計する可能性があるため、
本 Issue では挙動変更を避けリロケーションのみに留める）。

Stop hook が発火したとき、直前のターンで merge-summary marker が新規作成されていれば、
transcript_path から直近の assistant メッセージを読み、生成表の合計行との厳密一致を
確認する。一致しなければ exit 2 相当（呼び出し元が実際の `sys.exit` を行う）で
再指示を注入する。

stdlib のみ使用。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stop_orphan_state import resolve_current_issue  # type: ignore[import-not-found]

_DIGITS_RE = re.compile(r"^[0-9]+$")

# merge-summary チェック用のマーカーサブディレクトリ名（merge_summary.py と同じ）
_MERGE_SUMMARY_MARKER_SUBDIR = "merge-summary-emitted"

# 合計行パターン: format_summary_table が生成する「| **合計** | | | **<duration>** | | |」の合計表示部
_MERGE_SUMMARY_TOTAL_RE = re.compile(r"\*\*([^*]+)\*\*")

# marker TTL のデフォルト秒数（30 分）。Issue #2751 フェイルセーフ。
_MERGE_SUMMARY_MARKER_TTL_SECONDS_DEFAULT = 1800


def _resolve_merge_summary_marker_ttl() -> int:
    """marker TTL を環境変数から解決する（Issue #2751）.

    MERGE_SUMMARY_MARKER_TTL_SEC が設定されていればその値を使用する。
    非数値・0 以下・空はデフォルト（1800 秒 = 30 分）にフォールバックする。
    """
    raw = os.environ.get("MERGE_SUMMARY_MARKER_TTL_SEC", "").strip()
    if not raw:
        return _MERGE_SUMMARY_MARKER_TTL_SECONDS_DEFAULT
    if not _DIGITS_RE.match(raw):
        return _MERGE_SUMMARY_MARKER_TTL_SECONDS_DEFAULT
    val = int(raw)
    if val <= 0:
        return _MERGE_SUMMARY_MARKER_TTL_SECONDS_DEFAULT
    return val


def _resolve_merge_summary_marker_dir(repo_root: str | None = None) -> Path:
    """merge-summary-emitted ディレクトリのパスを解決する（merge_summary._marker_dir() と同規則）.

    優先順:
    1. ISSUE_NEXT_STATE_ROOT 環境変数（テスト・worktree 対応）
    2. repo_root 引数（git rev-parse --show-toplevel で取得）
    3. カレントディレクトリ（フォールバック）
    """
    root_override = os.environ.get("ISSUE_NEXT_STATE_ROOT", "")
    if root_override:
        return Path(root_override) / "cache" / _MERGE_SUMMARY_MARKER_SUBDIR
    if repo_root:
        return Path(repo_root) / "cache" / _MERGE_SUMMARY_MARKER_SUBDIR
    return Path.cwd() / "cache" / _MERGE_SUMMARY_MARKER_SUBDIR


def _read_last_assistant_text(transcript_path: str) -> str | None:
    """transcript JSONL の直近 assistant メッセージのテキストを返す.

    ファイルが読めない、assistant メッセージが見つからない場合は None を返す。
    """
    path = Path(transcript_path)
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") != "assistant":
            continue
        message = record.get("message", {})
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                if isinstance(text, str) and text:
                    return text
    return None


def check_merge_summary_in_transcript(
    payload: dict, repo_root: str | None = None
) -> int:
    """merge-summary marker が直前ターンで新規作成されていれば transcript の assistant メッセージと照合する.

    Issue #2466 (D): Stop hook が発火したとき、直前のターンで merge-summary marker が
    新規作成されていれば、transcript_path から直近の assistant メッセージを読み、
    生成表の合計行との厳密一致を確認する。一致しなければ exit 2 で再指示を注入する。

    Issue #2751: フェイルセーフを追加する。
    - 照合に成功したとき、対象 marker を削除する（消し忘れ防止）。
    - marker の mtime が TTL（既定 1800 秒 = 30 分、MERGE_SUMMARY_MARKER_TTL_SEC で上書き可）を
      超過している場合は照合をスキップして exit 0 で pass する。

    Returns:
        0: チェック不要 or 合計行が含まれている
        2: 合計行が含まれていない（再指示を注入）
    """
    try:
        marker_dir = _resolve_merge_summary_marker_dir(repo_root)
        if not marker_dir.is_dir():
            return 0

        # issue-next-state.json から current_issue を取得し、対象 marker を絞り込む。
        # 「残存 marker（別 Issue の完了済み marker）」による誤ブロックを防ぐ（#2466 codex 指摘）。
        current_issue = resolve_current_issue(repo_root)
        if current_issue is None:
            # state が取得できない場合は fail-safe で pass（実行中 Issue 不明）
            return 0

        # 対象 marker: current_issue に対応するファイルのみ
        target_marker = marker_dir / f"{current_issue}.marker"
        if not target_marker.is_file():
            return 0

        # Issue #2752: marker に記録された発行元セッション識別子と Stop hook payload の
        # session_id を照合する。複数ターミナルで同一ディレクトリを開いている場合、
        # 別セッション（別ターミナルの issue-next 等）が発行した marker が無関係な
        # 会話をブロックしてしまう事故（#2733/#2735/#2742）を防ぐ。
        try:
            recorded_session_id = target_marker.read_text(encoding="utf-8").strip()
        except OSError:
            recorded_session_id = ""
        if recorded_session_id:
            payload_session_id = payload.get("session_id", "")
            if not isinstance(payload_session_id, str) or not payload_session_id:
                # payload に session_id が無い場合は照合不能のため fail-safe で pass する
                return 0
            if payload_session_id != recorded_session_id:
                # 別セッションが発行した marker → このセッションの会話はブロックしない
                return 0
            # session_id が一致 → 通常の照合フローに進む
        # recorded_session_id が空（session 識別子未記録の旧 marker）は後方互換として
        # 常に照合する。

        # Issue #2751: TTL チェック。mtime が TTL を超過していれば照合をスキップして pass する。
        marker_ttl = _resolve_merge_summary_marker_ttl()
        try:
            marker_mtime = target_marker.stat().st_mtime
            marker_age = time.time() - marker_mtime
            if marker_age > marker_ttl:
                # TTL 超過 → 消し忘れフェイルセーフとして照合をスキップ
                return 0
        except OSError:
            pass  # stat 失敗は無視して照合を続行する

        marker_files = [target_marker]

        # transcript_path が payload にない場合は fail-safe で pass
        transcript_path = payload.get("transcript_path", "")
        if not isinstance(transcript_path, str) or not transcript_path:
            return 0

        # 各 marker に対して対応する .txt を確認する
        for marker_file in marker_files:
            issue_num = marker_file.stem  # "2466"
            txt_path = marker_dir / f"{issue_num}.txt"
            if not txt_path.is_file():
                # txt がない（旧 marker）場合は fail-safe でスキップ
                continue

            table_text = txt_path.read_text(encoding="utf-8")
            # 合計行を抽出: "| **合計** | | | **<duration>** | | |" の duration 部分
            total_row = None
            for line in table_text.splitlines():
                if "**合計**" in line:
                    total_row = line
                    break
            if total_row is None:
                continue

            # **duration** の部分を抽出（例: **30分00秒**）
            # 合計行の形式: | **合計** | | | **30分00秒** | | |
            # 最初の **合計** の後にある **...** を取得
            bold_parts = _MERGE_SUMMARY_TOTAL_RE.findall(total_row)
            # bold_parts の例: ["合計", "30分00秒"]
            # 最初は "合計" なので 2 番目が duration
            if len(bold_parts) < 2:
                continue
            total_duration = bold_parts[1]
            # transcript の assistant メッセージ本文を取得
            assistant_text = _read_last_assistant_text(transcript_path)
            if assistant_text is None:
                continue

            # 厳密一致チェック: 合計行（total_row）全体が含まれるか
            if total_row.strip() not in assistant_text:
                sys.stderr.write(
                    f"on-stop: merge-summary 転記未確認 (issue #{issue_num}): "
                    f"合計行「{total_duration}」が直近の assistant メッセージに含まれていません。\n"
                    f"所要時間サマリを報告に含めてください（`cache/merge-summary-emitted/{issue_num}.txt` 参照）。\n"
                )
                return 2

            # Issue #2751: 照合成功 → marker を削除する（消し忘れフェイルセーフ）
            try:
                marker_file.unlink(missing_ok=True)
            except OSError:
                pass  # 削除失敗は無視（次回 TTL で自動スキップされる）

    except Exception:  # noqa: BLE001 — Stop hook は fail-safe で exit 0
        return 0

    return 0
