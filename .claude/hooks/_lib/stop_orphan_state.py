"""Stop hook の孤児 issue-next state 検出（Issue #1561・#1698・#2544・#2967）.

`.claude/hooks/on-stop.py` に同居していた孤児 state 検出ロジックを移設したもの。

孤児 state = `current_issue` に着手中番号があるが、対応する worktree（branch 名に
`issue-<N>-` を含む）と open PR（本文に `closes #<N>` を含む）の両方が存在しない状態。
検出時は auto-resume 指示を stderr に注入するため exit code 2 相当の値を返す
（実際の `sys.exit` は呼び出し元 on-stop.py の責務）。

- ``resolve_current_issue()``: per-issue state ファイルから現在アクティブな
  Issue 番号を返す。merge-summary 転記チェック（`_lib/stop_merge_summary_check.py`）
  からも共用される。
- ``detect_orphan_issue_next_state()``: 孤児判定のエントリポイント。

stdlib のみ使用。git/gh subprocess 実行は ``_lib/git_helpers.py`` に委譲する。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_helpers import run_gh as _gh  # type: ignore[import-not-found]
from git_helpers import run_git_in_repo as _git  # type: ignore[import-not-found]

_MAX_RESUME_ATTEMPTS = 3

_ORPHAN_LIVENESS_TTL_SECONDS_DEFAULT = (
    1800  # 30 分（issue_next_state.py の DEFAULT_LIVENESS_TTL_SECONDS と同値）
)

_ISSUE_NEXT_STATE_SUBDIR = (
    "issue-next-state"  # issue_next_state.py の STATE_SUBDIR と同名（#2474）
)


def _iter_state_files_from_cache_dir(cache_root: Path) -> list[Path]:
    """cache ディレクトリから state ファイル一覧を返す共通ヘルパー（Issue #2517）.

    優先順（すべて走査対象）:
    1. `cache/issue-next-state/issue-*.json`（新: per-issue ファイル、複数存在しうる）
    2. `cache/issue-next-state.json`（旧: 単一フラットファイル、移行未了・後方互換）

    `_iter_orphan_candidate_state_files()` および `resolve_current_issue()` が共用する。
    """
    candidates: list[Path] = []
    per_issue_dir = cache_root / _ISSUE_NEXT_STATE_SUBDIR
    if per_issue_dir.is_dir():
        candidates.extend(sorted(per_issue_dir.glob("issue-*.json")))
    legacy_flat_file = cache_root / "issue-next-state.json"
    if legacy_flat_file.is_file():
        candidates.append(legacy_flat_file)
    return candidates


def resolve_current_issue(repo_root: str | None = None) -> int | None:
    """per-issue state ファイルを走査して current_issue 番号を返す（#2466・#2517）.

    Issue #2474 以降、`tidd issue-next-state init` は per-issue ファイル
    (`cache/issue-next-state/issue-<N>.json`) にのみ書く。本関数は
    `_iter_state_files_from_cache_dir()` を通じて per-issue ファイルおよび
    旧フラットファイルを両方走査し、current_issue を持つファイルのうち
    liveness_at（または last_active）が最新のものを「現在アクティブな Issue」として返す。

    複数の per-issue ファイルが同時に存在する場合（複数 Issue の並列 loop 実行中等）は
    liveness_at が最も新しいファイルを採用する。

    state ファイルが存在しない・読めない・current_issue が設定されていない場合は None を返す。
    """
    root_override = os.environ.get("ISSUE_NEXT_STATE_ROOT", "")
    if root_override:
        cache_root = Path(root_override) / "cache"
    elif repo_root:
        cache_root = Path(repo_root) / "cache"
    else:
        cache_root = Path.cwd() / "cache"

    candidates = _iter_state_files_from_cache_dir(cache_root)

    best_issue: int | None = None
    best_liveness: str = ""  # ISO 形式の文字列比較（辞書順 = 時刻順）

    for state_file in candidates:
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        current = data.get("current_issue")
        if not isinstance(current, int) or current <= 0:
            continue
        # liveness_at（または last_active）で最新のものを選ぶ
        liveness = data.get("liveness_at") or data.get("last_active") or ""
        if not isinstance(liveness, str):
            liveness = ""
        if best_issue is None or liveness > best_liveness:
            best_issue = current
            best_liveness = liveness

    return best_issue


def _latest_liveness_timestamp(data: dict) -> _dt.datetime | None:
    """`liveness_at` と `last_active` のうち新しい方を aware datetime で返す（#2819）.

    issue_next_state.py の `_latest_liveness` と同一仕様。解釈できる値が
    1 つもなければ None（呼び出し元は従来どおり孤児判定へ進む）。
    """
    parsed_values = []
    for raw in (data.get("liveness_at"), data.get("last_active")):
        if not isinstance(raw, str) or not raw:
            continue
        try:
            parsed = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError, TypeError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        parsed_values.append(parsed)
    if not parsed_values:
        return None
    return max(parsed_values)


def _iter_orphan_candidate_state_files(repo_root: str) -> list[Path]:
    """孤児判定対象の state ファイル一覧を返す（Issue #2474: per-issue ファイル対応）.

    `_iter_state_files_from_cache_dir()` に委譲する（Issue #2517: 共通化）。

    on-stop.py は stdlib のみ使用のため tidd_tools パッケージを直接 import せず、
    ディレクトリ名・ファイル名のみ `issue_next_state.py` と揃える。
    """
    cache_dir = Path(repo_root) / "cache"
    return _iter_state_files_from_cache_dir(cache_dir)


def detect_orphan_issue_next_state(repo_root: str) -> int:
    """Issue #1698: issue-next state の孤児を検出して auto-resume 指示を注入する.

    Issue #2474: 単一グローバル state ファイルから per-issue state ファイル
    （`cache/issue-next-state/issue-<N>.json`）に移行したため、存在するすべての
    per-issue ファイル（+ 後方互換で旧単一ファイル）を走査する。いずれか 1 件でも
    孤児と判定されたら即座に exit 2 を返す（複数の孤児が同時に存在する場合は
    先に見つかった 1 件分の auto-resume 指示のみ注入し、次回 Stop で残りを検出する）。

    孤児 state = `current_issue` に着手中番号があるが、対応する worktree（branch 名に
    `issue-<N>-` を含む）と open PR（本文に `closes #<N>` を含む）の両方が存在しない状態。

    Returns:
        int: 孤児 state を検出し自動再開を試みる場合は 2、それ以外は 0。
             例外が発生した場合は常に 0（fail-safe）。
    """
    for state_path in _iter_orphan_candidate_state_files(repo_root):
        exit_code = _check_state_file_for_orphan(state_path, repo_root)
        if exit_code != 0:
            return exit_code
    return 0


def _check_state_file_for_orphan(state_path: Path, repo_root: str) -> int:
    """単一の state ファイルを孤児判定する（`detect_orphan_issue_next_state` の内部ヘルパ）.

    `/issue-next` の STEP 1.5 (品質チェック) で PASS コメント投稿後に turn が終了
    してしまうと発生する（Issue #1558 で実際に確認）。

    旧実装（Issue #1561）の stderr WARN は Claude Code UI に表示されないため無効だった。
    本実装（Issue #1698）では Stop hook exit 2 を返して stop をブロックし、stderr に
    auto-resume 指示（具体的な git worktree add コマンドを含む）を注入する。

    Issue #2464: liveness TTL チェックを追加。`liveness_at`（または `last_active`）が
    TTL（環境変数 `ISSUE_NEXT_LIVENESS_TTL_SECONDS`、デフォルト 1800 秒）内なら
    「他セッション作業中」として孤児判定をスキップする。これにより worktree 作成前の
    正常な作業中 state が誤って孤児と判定されることを防ぐ。
    TTL 値は `issue_next_state.py` の `check-liveness` と同一の環境変数・デフォルト値を使用。

    fail-safe: `resume_attempts` が _MAX_RESUME_ATTEMPTS (3) 以上の場合は exit 0 に落とし
    無限ループを防ぐ。

    Returns:
        int: 孤児 state を検出し自動再開を試みる場合は 2、それ以外は 0。
             例外が発生した場合は常に 0（fail-safe）。
    """
    if not state_path.is_file():
        return 0

    try:
        raw = state_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0

    current = data.get("current_issue")
    if current is None:
        return 0
    try:
        issue_num = int(current)
    except (TypeError, ValueError):
        return 0
    if issue_num <= 0:
        return 0

    # Issue #2464: liveness TTL チェック
    # `issue_next_state.py` の `check-liveness` と同一の環境変数・デフォルト値を使用する。
    # on-stop.py は stdlib のみ使用のため tidd_tools パッケージを直接 import しない。
    ttl_raw = os.environ.get("ISSUE_NEXT_LIVENESS_TTL_SECONDS", "")
    try:
        liveness_ttl = int(ttl_raw)
        if liveness_ttl <= 0:
            raise ValueError("TTL must be positive")
    except (ValueError, TypeError):
        liveness_ttl = _ORPHAN_LIVENESS_TTL_SECONDS_DEFAULT

    # Issue #2819: `liveness_at` と `last_active` のうち新しい方を採用する。
    # 片方の更新が漏れても他方が生きていればバッチ継続中と判定できるようにする。
    liveness_at = _latest_liveness_timestamp(data)
    if liveness_at is not None:
        now = _dt.datetime.now(_dt.timezone.utc)
        deadline = now - _dt.timedelta(seconds=liveness_ttl)
        if liveness_at >= deadline:
            # TTL 内 → 他セッション作業中のため孤児判定をスキップ
            return 0

    # Issue #2544: 孤児判定の前に Issue の state を確認する。
    # CLOSED Issue は「完了して worktree も PR も片付いた」状態であり、孤児ではない。
    # gh が失敗した場合も false positive を避けるため「完了済み」扱いとする。
    if shutil.which("gh") is not None:
        rc_issue, out_issue = _gh(
            "issue",
            "view",
            str(issue_num),
            "--json",
            "state",
            timeout=8,
        )
        if rc_issue != 0:
            # gh 呼び出し失敗 → false positive を避けるため孤児判定しない
            return 0
        try:
            issue_data = json.loads(out_issue or "{}")
        except json.JSONDecodeError:
            issue_data = {}
        if isinstance(issue_data, dict) and issue_data.get("state") == "CLOSED":
            # CLOSED Issue → 完了済みのため孤児判定しない。
            # Issue #2825: バッチモードでは同一 state ファイルに次の Issue の番号列（queue）が残る。
            # queue が非空のときは state ファイルを削除せず、current_issue を null に更新して
            # 次の consume が続行できる状態にする。queue が空（単一 Issue モード）なら従来通り削除する。
            remaining_queue = data.get("queue")
            if not isinstance(remaining_queue, list):
                remaining_queue = []
            if remaining_queue:
                # queue が残っている → state ファイルを保持し current_issue のみ null に更新する
                data["current_issue"] = None
                try:
                    state_path.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except OSError:
                    pass  # 書き込み失敗は無視（next consume が再チェックするため）
            else:
                # queue が空（単一 Issue モード）→ 従来通り state ファイルを削除する
                try:
                    state_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return 0
    else:
        # gh 未インストール → false positive を避けるため孤児判定しない
        return 0

    # worktree 検出: `git worktree list --porcelain` の branch 名に `issue-<N>-` を含むか。
    # worktree のディレクトリ名も `<repo>-issue-<N>-<slug>` 形式なので、branch 名検査で
    # SKILL の推奨形式をカバーできる。
    rc, out, _ = _git(repo_root, "worktree", "list", "--porcelain", timeout=5)
    if rc == 0:
        marker = f"issue-{issue_num}-"
        for line in out.splitlines():
            # `branch refs/heads/<name>` 形式
            if line.startswith("branch refs/heads/") and marker in line:
                return 0

    # PR 検出: `gh pr list --search "closes #<N> in:body" --state all` が空でなければ存在。
    # Issue #2544: --state open から --state all に変更し MERGED PR も検出対象にする。
    # gh が未インストールの環境では false positive を避けるため「PR あり」扱いとする
    # （gh 検出できないなら警告を出さない）。既に上で gh 存在確認済みのためここは通過済み。
    rc_gh, out_gh = _gh(
        "pr",
        "list",
        "--state",
        "all",
        "--search",
        f"closes #{issue_num} in:body",
        "--json",
        "number",
        timeout=8,
    )
    if rc_gh != 0:
        # gh 呼び出し失敗 → false positive を避けるため「PR あり」扱い
        return 0
    try:
        parsed = json.loads(out_gh or "[]")
    except json.JSONDecodeError:
        parsed = []
    if isinstance(parsed, list) and len(parsed) > 0:
        return 0

    # ここまで到達 = worktree なし + open PR なし → 孤児 state
    # resume_attempts を確認して fail-safe ロジックを適用する
    try:
        resume_attempts = int(data.get("resume_attempts") or 0)
    except (TypeError, ValueError):
        resume_attempts = 0

    if resume_attempts >= _MAX_RESUME_ATTEMPTS:
        # fail-safe: 3 回超で exit 0 に落とし無限ループを防ぐ
        sys.stderr.write(
            f"on-stop: WARN: Resume attempts exceeded for #{issue_num} "
            f"(resume_attempts={resume_attempts} >= {_MAX_RESUME_ATTEMPTS}). "
            f"Manual intervention required. See {state_path}.\n"
        )
        return 0

    # resume_attempts をインクリメントして state ファイルを更新する
    data["resume_attempts"] = resume_attempts + 1
    try:
        state_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # 書き込み失敗は無視（重要なのは exit 2 で auto-resume を注入すること）

    # exit 2 で stop をブロックし、stderr に auto-resume 指示を注入する（Issue #1698）
    # Claude Code の Stop hook exit 2 は stop をブロックして stderr を Claude のコンテキストに
    # 注入する仕様を利用する。
    repo_name = Path(repo_root).name
    worktree_dir = f"../{repo_name}-issue-{issue_num}-<slug>"
    worktree_cmd = (
        f"git worktree add -b fix/issue-{issue_num}-<slug> {worktree_dir} origin/main"
    )
    sys.stderr.write(
        f"on-stop: Orphan issue-next state detected for #{issue_num} "
        f"(no worktree, no open PR). "
        f"Attempt {resume_attempts + 1}/{_MAX_RESUME_ATTEMPTS}. "
        f"Resuming STEP 2 now: {worktree_cmd}\n"
        f"Please execute: {worktree_cmd}\n"
        f"Then continue with /issue-next STEP 2 (worktree creation) for Issue #{issue_num}.\n"
    )
    return 2
