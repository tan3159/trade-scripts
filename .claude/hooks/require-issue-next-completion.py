#!/usr/bin/env python3
"""Stop hook: `/issue-next <N>`（引数ありモード）の途中終了を機械的に防ぐ (Issue #2321・#3724)。

**背景:** `/issue-next <N>` は STEP 0（品質チェック）後に turn が終了しても機械的な
継続保証がなかった。既存の `on-stop.py` 孤児検出（Issue #1698）は `current_issue` に
対応する worktree・open PR の有無で判定するため、worktree 作成前（STEP 0 〜 STEP 1 の
間）の離脱を検出できない。本 hook は issue-next state の `current_issue` が設定されて
いる「間」を単純にブロック対象とすることでこのギャップを埋める。

**Issue #3724（コミット禁止規約誤適用による停止防止）:**

実装・テスト・pre-flight まで完了した後に、エージェントが一般規約「明示的に求められない限り
コミットしない」を理由にコミット・PR作成を停止する事象が発生した。issue-next の継続フローは
実装から PR・レビューまでを一気通貫で進める契約だが、一般規約との優先順位が機械的に明示されて
いなかった。

**Issue #3779（別セッションの state による誤ブロック防止）:**

state ファイルの走査にセッション所有者判定が無く、複数ターミナルが並行稼働する運用では
別ターミナルが作業中の Issue の state が無関係なセッションの stop を誤ってブロックする
事故が実際に発生した。対策として state ファイルの `session_id`（PostToolUse hook
`stamp-issue-next-session.py` が記録）と Stop hook payload の `session_id` を照合し、
不一致の state はチェック対象から除外する。`session_id` 未記録の旧 state は後方互換として
従来どおり扱う。
併せて `_pr_exists()` は `gh pr list --search` の緩い全文検索結果を鵜呑みにせず、各 PR 本文が
実際に `closes #<N>` を含むかを検証してから「PR 作成済み」と判定する（GitHub の検索 API は
フレーズ一致ではないため、無関係な PR が誤ヒットしていた）。

対策:
1. state ファイルは #2474 移行後の per-issue 形式
   （`cache/issue-next-state/issue-<N>.json`）を読む。旧フラット
   （`cache/issue-next-state.json`）は移行未了・後方互換のため併読する。
2. in-progress かつ TTL 内で、PR 未作成の場合（コミット・push・PR作成が未完了）は
   **exit 1** で stop をブロックし、Issue 番号 N と未完了の遷移を stderr に出力する。
   stderr には一般規約より継続契約（コミット・push・PR作成まで進める）が優先される旨も
   含める。
3. PR 作成済み（マージ・後処理待ち）は従来どおり **exit 2** でブロックする。
4. Issue に `🙋 needs-human-input` ラベルが付いている場合（park 済み・人間判断待ち）は
   ブロックしない。単なるコミット・push・PR作成未実施を人間待ちとして扱わない。

**動作:**

1. issue-next state ファイルを走査する。ファイル無し・JSON 不正・`current_issue` が
   null の場合は何もしない（exit 0）。
2. `current_issue` が設定されていても、`last_active`（無ければ `started_at`）から
   `REQUIRE_ISSUE_NEXT_COMPLETION_TTL_SECONDS`（デフォルト 7200 秒 = 2 時間）以上
   経過していればブロックしない（フェイルセーフ。state 消し忘れによる永久ブロック防止）。
3. state の `session_id`（`stamp-issue-next-session.py` が記録）が Stop hook payload の
   `session_id` と異なる場合はブロックしない（別セッション所有・#3779）。`session_id` が
   未記録（旧 state）または自セッションの `session_id` が不明な場合は後方互換・
   フェイルセーフとしてチェック対象にする（判定不能時はブロック方向に倒す）。
4. それ以外（in-progress かつ TTL 内・自セッション所有）は:
   - Issue が `🙋 needs-human-input`（park 済み）→ ブロックしない（exit 0）
   - PR 未作成（`closes #N` を本文に含む PR が実在しない）→ **exit 1** で stop をブロックし、
     Issue 番号と未完了の遷移（コミット・push・PR作成）を stderr に出力する
   - PR 作成済み → **exit 2** で stop をブロックし、マージ・後処理の継続指示を
     stderr に注入する

`config.json` で default OFF（`is_hook_enabled()` の非安全系 hook デフォルト）。
stdlib のみ使用。git/gh subprocess 実行は `_lib/git_helpers.py` に委譲する。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.git_helpers import git_toplevel, run_gh  # type: ignore[import-not-found]
from _lib.hook_io import (  # type: ignore[import-not-found]
    is_hook_enabled,
    read_stop_hook_input,
)

_DEFAULT_TTL_SECONDS = 7200  # 2 時間
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_NEEDS_HUMAN_INPUT_LABEL = "🙋 needs-human-input"
# close(s)|fixes|fix|resolves #N（`shared.issue_body.CLOSES_RE` と同一パターン）。
# hooks/ は stdlib のみ使用の制約上 tidd_tools を import できないため regex を複製する
# （Issue #3779: gh 全文検索の緩いヒットを本文の実文字列一致で後段フィルタするため）。
_CLOSES_RE = re.compile(r"\b(?:closes?|fixes|fix|resolves)[:\s]+#(\d+)", re.IGNORECASE)


def _resolve_ttl_seconds() -> int:
    raw = os.environ.get("REQUIRE_ISSUE_NEXT_COMPLETION_TTL_SECONDS", "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TTL_SECONDS
    if value <= 0:
        return _DEFAULT_TTL_SECONDS
    return value


def _iter_state_files(repo_root: str) -> list[Path]:
    """issue-next state ファイル一覧を返す（per-issue 優先・旧フラット併読）.

    Issue #2474 で per-issue ファイル（`cache/issue-next-state/issue-<N>.json`）に移行した。
    移行未了・後方互換のため旧フラット（`cache/issue-next-state.json`）も対象に含める。
    """
    cache_dir = Path(repo_root) / "cache"
    candidates: list[Path] = []
    per_issue_dir = cache_dir / "issue-next-state"
    if per_issue_dir.is_dir():
        candidates.extend(sorted(per_issue_dir.glob("issue-*.json")))
    legacy = cache_dir / "issue-next-state.json"
    if legacy.is_file():
        candidates.append(legacy)
    return candidates


def _read_current_issue(state_path: Path) -> tuple[int | None, dict]:
    """state ファイルから current_issue と data を返す（不正・null は (None, {})）."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    current = data.get("current_issue")
    if current is None:
        return None, data
    try:
        issue_num = int(current)
    except (TypeError, ValueError):
        return None, data
    if issue_num <= 0:
        return None, data
    return issue_num, data


def _within_ttl(data: dict, ttl_seconds: int) -> bool:
    """`last_active`（無ければ `started_at`）から TTL 内なら True.

    timestamp が解釈できない場合は False（ブロックしない・安全側フォールバック）。
    """
    timestamp_raw = data.get("last_active") or data.get("started_at")
    if not isinstance(timestamp_raw, str) or not timestamp_raw:
        return False
    try:
        last_active = datetime.strptime(timestamp_raw, _TIMESTAMP_FORMAT).replace(
            tzinfo=UTC
        )
    except ValueError:
        return False
    elapsed = (datetime.now(UTC) - last_active).total_seconds()
    return elapsed <= ttl_seconds


def _issue_is_parked(issue_num: int) -> bool:
    """Issue に `🙋 needs-human-input` ラベルがあれば True（park 済み・人間判断待ち）.

    gh 呼び出し失敗時は「park ではない」扱い（fail-closed）。本 hook の目的が
    「コミット・PR作成前の早期離脱防止」であるため、判定不能時はブロック方向（継続強制）
    に倒す。
    """
    rc, out = run_gh("issue", "view", str(issue_num), "--json", "labels", timeout=8)
    if rc != 0:
        return False
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return False
    labels = data.get("labels")
    if not isinstance(labels, list):
        return False
    return any(
        isinstance(label, dict) and label.get("name") == _NEEDS_HUMAN_INPUT_LABEL
        for label in labels
    )


def _pr_body_closes_issue(body: object, issue_num: int) -> bool:
    """PR 本文が実際に `closes #<issue_num>` を含むかを判定する（Issue #3779）."""
    if not isinstance(body, str) or not body:
        return False
    return any(int(m.group(1)) == issue_num for m in _CLOSES_RE.finditer(body))


def _pr_exists(issue_num: int) -> bool:
    """`closes #<N>` を実際に本文に含む PR が 1 件以上存在すれば True.

    `gh pr list --search "closes #<N> in:body"` は GitHub の全文検索を使うため
    フレーズ一致ではなく緩いトークン一致で評価される。"closes" と "#<N>" が本文中の
    別々の場所にあるだけでもヒットしてしまう（Issue #3779 の実測ケース）ため、
    取得した各 PR の本文を `_pr_body_closes_issue()` で後段フィルタし、実際に
    issue_num をクローズする語句が隣接している PR のみを「PR 作成済み」と判定する。

    gh 呼び出し失敗時は「PR なし」扱い（コミット・push・PR作成が未完了として報告する）。
    """
    rc, out = run_gh(
        "pr",
        "list",
        "--state",
        "all",
        "--search",
        f"closes #{issue_num} in:body",
        "--json",
        "number,body",
        timeout=8,
    )
    if rc != 0:
        return False
    try:
        parsed = json.loads(out or "[]")
    except json.JSONDecodeError:
        parsed = []
    if not isinstance(parsed, list):
        return False
    return any(
        isinstance(entry, dict) and _pr_body_closes_issue(entry.get("body"), issue_num)
        for entry in parsed
    )


def _is_own_session(data: dict, own_session_id: str) -> bool:
    """state ファイルが自セッション所有かを判定する（Issue #3779）.

    - state に `session_id`（`stamp-issue-next-session.py` が記録）が無い
      （旧 state・stamp hook 未有効）→ 後方互換で True（従来どおりチェック対象にする）
    - 自セッションの `session_id` が不明（Stop hook payload に含まれない）
      → 判定不能のためブロック方向に倒し True（`_issue_is_parked` と同じ fail-closed 方針。
      本 hook の目的が「コミット・PR作成前の早期離脱防止」であるため）
    - 両方あり一致 → True。不一致 → False（別セッション所有・チェック対象から除外）
    """
    recorded = data.get("session_id")
    if not isinstance(recorded, str) or not recorded:
        return True
    if not own_session_id:
        return True
    return recorded == own_session_id


def _check_in_progress(repo_root: str, ttl_seconds: int, own_session_id: str) -> int:
    """in-progress かつ TTL 内・自セッション所有の state を検出して stop をブロックする.

    Returns:
        int: ブロックする場合は 1（PR 未作成）または 2（PR 作成済み）、
             それ以外は 0。例外は呼び出し元 main() が捕捉して 0 にフォールバック。
    """
    for state_path in _iter_state_files(repo_root):
        issue_num, data = _read_current_issue(state_path)
        if issue_num is None:
            continue
        if not _within_ttl(data, ttl_seconds):
            continue

        # 別セッション所有の state はブロック対象から除外する（Issue #3779）
        if not _is_own_session(data, own_session_id):
            continue

        # park 済み（人間判断待ち）→ ブロックしない（Issue #3724 Scenario 2）
        if _issue_is_parked(issue_num):
            continue

        if _pr_exists(issue_num):
            # PR 作成済み → マージ・後処理待ち（従来どおり exit 2 で stderr 注入）
            sys.stderr.write(
                f"require-issue-next-completion: Issue #{issue_num} はまだ未完了です "
                f"(PR 作成済み・マージ/後処理待ち)。\n"
                f"未完了の遷移: マージ、後処理（worktree 削除・ブランチ削除・main sync）。\n"
                f"マージ・後処理が完了するまで turn を終了せず "
                f"/issue-next #{issue_num} を継続してください。\n"
            )
            return 2

        # PR 未作成 → コミット・push・PR作成待ち（Issue #3724 Scenario 3）
        sys.stderr.write(
            f"require-issue-next-completion: Issue #{issue_num} はまだ未完了です "
            f"(PR 未作成・コミット/push/PR作成待ち)。\n"
            f"未完了の遷移: コミット、push、PR作成。\n"
            f"issue-next 実行中は一般規約「明示的に求められない限りコミットしない」より "
            f"継続契約（コミット・push・PR作成まで進める）が優先されます。\n"
            f"セッションを終了せず、コミット → push → PR作成 まで続行してください。\n"
        )
        return 1
    return 0


def main() -> int:
    # Issue #2957: stdin 読み取りを hook_io.read_stop_hook_input へ集約
    # （従来は readline() で 1 行のみ読んで payload を捨てていた）。
    # Issue #3779: 自セッションの session_id をセッション所有者判定に使うため取得する。
    payload = read_stop_hook_input()

    if not is_hook_enabled("require-issue-next-completion"):
        return 0

    # Issue #2958: toplevel 解決は `_lib/git_helpers.py` の `git_toplevel()` に委譲する
    repo_root = git_toplevel(timeout=5)
    if not repo_root:
        return 0

    own_session_id_raw = payload.get("session_id", "")
    own_session_id = own_session_id_raw if isinstance(own_session_id_raw, str) else ""

    try:
        return _check_in_progress(repo_root, _resolve_ttl_seconds(), own_session_id)
    except Exception:  # noqa: BLE001 — Stop hook はフェイルセーフで exit 0
        return 0


if __name__ == "__main__":
    sys.exit(main())
