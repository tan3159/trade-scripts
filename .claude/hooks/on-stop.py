#!/usr/bin/env python3
"""Stop hook: Slack 通知判定・gone ブランチ削除・brief.md 生成を行う.

旧 on-stop.sh を 1:1 で踏襲する（Phase 4 / #1057 で Python 化）。
Issue #349 (Slack 入力待ち通知)・#1033 (PR-based ブランチ削除)・#1043 (Slack 通知デフォルト無効)。
Issue #1265: ネットワーク I/O を含む重い処理を日次に throttle する。
Issue #1175: 重い処理を並列化 + AI_REVIEW_STOP_HOOK_TIMEOUT で全体を打ち切る。
Issue #1557: ON_STOP_PROFILE=1 で各 phase の所要時間を stderr に出力する。
Issue #1561: `cache/issue-next-state.json` の孤児 state（worktree なし・PR なし）を検出して警告する。
Issue #1698: 孤児 state 検出時に exit 2 + stderr で auto-resume 指示を注入する。
  - Stop hook exit 2 は stop をブロックして stderr を Claude のコンテキストに注入する仕様を利用。
  - `resume_attempts` フィールドで 3 回超の無限ループを防ぐ（fail-safe: exit 0）。

動作:
  1. ON_STOP_SLACK_ENABLED=1 なら stop_reason=input_required で Slack 通知（fire-and-forget）
  2. `cache/issue-next-state.json` の `current_issue` について、対応する worktree
     （branch 名に issue-<N>- を含む）と open PR（closes #<N> を本文に含む）の両方が
     無ければ exit 2 + stderr で "Orphan issue-next state" と auto-resume 指示を注入する
     （Issue #1698。旧 #1561 の stderr WARN は UI に表示されなかったため exit 2 に強化）。
     - `resume_attempts` が 3 以上の場合は fail-safe で exit 0 に落とし無限ループを防ぐ。
     - この検出は throttle の外で毎回走る（軽量で人間介入が必要なため）。
  3. 以下の重い処理は ON_STOP_THROTTLE_SECONDS（デフォルト 86400 秒 = 1 日）の
     間隔でのみ実行する。最終実行時刻は ON_STOP_LAST_CLEANUP_FILE
     （デフォルト ~/.cache/on-stop-last-cleanup）に保存する。
     3 phase を ThreadPoolExecutor で並列実行し、AI_REVIEW_STOP_HOOK_TIMEOUT
     （デフォルト 15 秒）で全体を打ち切る。
     - git fetch --prune した後、リモート削除済み（[gone]）ローカルブランチを強制削除
     - squash merge 後の MERGED PR 紐付きブランチも削除（OPEN PR・チェックアウト中ブランチは保持）
     - cache/brief.md を生成（ブランチ・直近コミット・Open PR / Issue）

hook は常に exit 0（セッション終了をブロックしない）。stdlib のみ使用。

環境変数:
  ON_STOP_SLACK_ENABLED        1 なら Slack 通知を送る（デフォルト 0）。
                               **deprecated（Issue #1994）**: hooks-config.json の
                               "on-stop-slack" キー（true/false）が優先される。
  ON_STOP_THROTTLE_SECONDS     重い処理の実行間隔秒数（デフォルト 86400）。
                               0 なら throttle 無効で毎回実行する。
                               非数値ならデフォルトにフォールバック。
  ON_STOP_LAST_CLEANUP_FILE    最終実行タイムスタンプ保存先
                               （デフォルト ~/.cache/on-stop-last-cleanup）
  AI_REVIEW_STOP_HOOK_TIMEOUT  重い処理全体の壁時計タイムアウト秒数（デフォルト 15）。
                               超過時は未完了処理を打ち切って exit 0 で終わる。
                               非数値・0 以下ならデフォルトにフォールバック。
  ON_STOP_PROFILE              1 なら各 phase の所要時間を stderr にログ出力する（Issue #1557）。
                               "on-stop: PROFILE: <phase>=<elapsed_ms>ms" 形式で出力。
                               デフォルト 0（無効）。実測ボトルネック特定用の opt-in。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_DEFAULT_THROTTLE_SECONDS = 86400  # 1 日
_DEFAULT_STOP_HOOK_TIMEOUT_SECONDS = 15  # Issue #1175 target
_DIGITS_RE = re.compile(r"^[0-9]+$")


def _drain_stdin() -> str:
    """非対話の場合のみ stdin を 1 行読む。対話端末では空を返す."""
    if sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.readline()
    except (OSError, ValueError):
        return ""


def _extract_stop_reason(raw: str) -> str:
    if not raw.strip():
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 旧 sh は grep -o で抜き出していたので、JSON でなくてもパターン抽出を試みる
        m = re.search(r'"stop_reason"\s*:\s*"([^"]*)"', raw)
        return m.group(1) if m else ""
    if isinstance(data, dict):
        # Issue #1364: Stop schema で validate（不一致は WARN log のみで exit 2 は避ける・
        # Stop hook は Slack 通知等の副作用処理を伴うため silent fail 原則の対象外）
        _lib_dir = Path(__file__).resolve().parent / "_lib"
        if str(_lib_dir) not in sys.path:
            sys.path.insert(0, str(_lib_dir))
        try:
            from validate_payload import (  # type: ignore[import-not-found]
                PayloadValidationError,
                validate_payload,
            )

            validate_payload(data, "Stop")
        except ImportError:
            pass
        except PayloadValidationError as exc:
            sys.stderr.write(f"on-stop.py: WARN: Stop schema mismatch: {exc}\n")
        return str(data.get("stop_reason") or "")
    return ""


def _notify_slack(webhook: str) -> None:
    """Fire-and-forget Slack 通知（Issue #1175）: Popen で投げっぱなしにし結果を待たない.

    curl 側の ``--max-time`` で自身をタイムアウトさせる。親（この hook）は wait せず
    速やかに返る。
    """
    try:
        subprocess.Popen(  # noqa: S603 — 引数は静的なので shell injection なし
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


def _git(repo: str, *args: str, timeout: int = 8) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 1, "", ""
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"on-stop: WARN: git timeout ({timeout}s) for: git {' '.join(args)}\n"
        )
        return 1, "", ""
    return result.returncode, result.stdout, result.stderr


def _gh(*args: str, timeout: int = 8) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=timeout
        )
    except FileNotFoundError:
        return 1, ""
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"on-stop: WARN: gh timeout ({timeout}s) for: gh {' '.join(args)}\n"
        )
        return 1, ""
    return result.returncode, result.stdout


def _detect_default_branch(repo: str) -> str:
    rc, out, _ = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    if rc != 0:
        return "main"
    name = out.strip()
    name = name.replace("refs/remotes/origin/", "", 1)
    return name or "main"


def _cleanup_gone_branches(repo: str, default_branch: str) -> None:
    # Issue #1175: fetch はネットワーク I/O のためデフォルト 8s より長めの 15s。
    # 全体は AI_REVIEW_STOP_HOOK_TIMEOUT で覆われるので、fetch 個別の timeout は
    # global timeout 上限（デフォルト 15s）と揃えておく。
    _git(repo, "fetch", "--prune", timeout=15)

    rc, out, _ = _git(
        repo,
        "for-each-ref",
        "--format=%(refname:short) %(upstream:track)",
        "refs/heads",
    )
    if rc != 0:
        return

    gone: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "[gone]" and parts[0] != default_branch:
            gone.append(parts[0])

    if not gone:
        return

    sys.stderr.write(
        f"on-stop: リモート削除済みブランチを検出しました: {' '.join(gone)} \n"
    )
    for branch in gone:
        _, out_b, err_b = _git(repo, "branch", "-D", "--", branch)
        for line in (out_b + err_b).splitlines():
            if line:
                sys.stderr.write(f"on-stop: {line}\n")


def _checked_out_branches(repo: str) -> set[str]:
    rc, out, _ = _git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        return set()
    result: set[str] = set()
    for line in out.splitlines():
        if line.startswith("branch refs/heads/"):
            result.add(line[len("branch refs/heads/") :])
    return result


def _all_local_branches(repo: str, default_branch: str) -> list[str]:
    rc, out, _ = _git(repo, "branch", "--format=%(refname:short)")
    if rc != 0:
        return []
    return [b for b in out.splitlines() if b and b != default_branch]


def _fetch_pr_state_map(repo_nwo: str) -> dict[str, set[str]]:
    """Issue #1565: `gh pr list --state all` を **1 回** だけ叩いてブランチ→状態集合を返す.

    従来はブランチごとに `gh pr list --head <branch> --state open` / `... --state merged`
    を N+1 で呼んでいたため 20 ブランチで最大 40 回の gh 呼び出しになっていた。
    本関数は 1 回の bulk クエリに統合し、Python 側で dict にマップして各ブランチの
    state を lookup できる形に変換する。

    Returns:
        {"branch_name": {"OPEN", "MERGED", "CLOSED"}, ...} 形式の dict。
        1 ブランチに複数の PR が紐付いている場合は state 集合になる（例: 過去 MERGED あり + 現在 OPEN）。
        gh 呼び出しが失敗した場合や JSON parse 失敗の場合は空 dict を返す。
    """
    args = ["pr", "list"]
    if repo_nwo:
        args.extend(["--repo", repo_nwo])
    # `--limit 200`: 実運用の並行 PR 数 (STEP 0 上限 3) を大きく上回る余裕を持たせる
    args.extend(
        ["--state", "all", "--limit", "200", "--json", "headRefName,state"]
    )
    rc, out = _gh(*args)
    if rc != 0:
        return {}
    try:
        parsed = json.loads(out or "[]")
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, list):
        return {}

    result: dict[str, set[str]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        branch = item.get("headRefName")
        state = item.get("state")
        if not isinstance(branch, str) or not isinstance(state, str):
            continue
        result.setdefault(branch, set()).add(state)
    return result


def _cleanup_merged_pr_branches(repo: str, default_branch: str) -> None:
    """Issue #1565: bulk `gh pr list --state all` で N+1 を解消する.

    従来はブランチごとに `_pr_count(open)` / `_pr_count(merged)` を呼び出していたため
    20 ブランチで最大 40 回の gh 呼び出しになっていた。本改修で `gh pr list` の呼び出しは
    1 回（+ `gh repo view` 1 回）に定数化される。
    """
    if shutil.which("gh") is None:
        return
    checked_out = _checked_out_branches(repo)
    all_branches = _all_local_branches(repo, default_branch)
    rc, repo_nwo = _gh(
        "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"
    )
    repo_nwo = repo_nwo.strip() if rc == 0 else ""

    # bulk クエリ: 1 回の gh 呼び出しで全 PR の (branch, state) を取得する
    pr_state_map = _fetch_pr_state_map(repo_nwo)

    for branch in all_branches:
        if branch in checked_out:
            continue
        states = pr_state_map.get(branch, set())
        # OPEN が 1 件でもあれば保持
        if "OPEN" in states:
            continue
        # MERGED があれば削除対象
        if "MERGED" in states:
            sys.stderr.write(
                f"on-stop: MERGED PR 紐付きブランチを削除します: {branch}\n"
            )
            _, out_b, err_b = _git(repo, "branch", "-D", "--", branch)
            for line in (out_b + err_b).splitlines():
                if line:
                    sys.stderr.write(f"on-stop: {line}\n")


def _resolve_last_cleanup_file() -> Path:
    """throttle 用のタイムスタンプ保存先を解決する."""
    env_path = os.environ.get("ON_STOP_LAST_CLEANUP_FILE", "").strip()
    if env_path:
        return Path(env_path)
    return Path.home() / ".cache" / "on-stop-last-cleanup"


def _resolve_throttle_seconds() -> int:
    """throttle 秒数を環境変数から解決する.

    非数値ならデフォルト 86400 にフォールバックする（クラッシュしない）。
    """
    raw = os.environ.get("ON_STOP_THROTTLE_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_THROTTLE_SECONDS
    if not _DIGITS_RE.match(raw):
        return _DEFAULT_THROTTLE_SECONDS
    return int(raw)


def _should_run_cleanup(last_cleanup_file: Path, throttle_seconds: int) -> bool:
    """前回 cleanup から throttle_seconds 以上経過していれば True.

    タイムスタンプファイルが存在しない or 壊れている場合は初回とみなして True。
    throttle_seconds が 0 の場合は throttle 無効として常に True。
    """
    if throttle_seconds <= 0:
        return True
    if not last_cleanup_file.is_file():
        return True
    try:
        raw = last_cleanup_file.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    if not _DIGITS_RE.match(raw):
        return True
    last_ts = int(raw)
    now = int(time.time())
    return (now - last_ts) >= throttle_seconds


def _update_last_cleanup(last_cleanup_file: Path) -> None:
    """タイムスタンプファイルを「今」で更新する。失敗しても黙って続行."""
    try:
        last_cleanup_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        last_cleanup_file.write_text(f"{int(time.time())}\n", encoding="utf-8")
    except OSError:
        pass


def _write_brief(repo: str, brief_path: Path) -> None:
    """Issue #1175: 独立した git / gh 呼び出しを ThreadPoolExecutor で並列実行する.

    ``repo view -> pr list`` のみ順序依存で、それ以外は完全に独立。
    """
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    issue_high_args = (
        "issue",
        "list",
        "--label",
        "priority: high",
        "--label",
        "priority: critical",
        "--state",
        "open",
        "--json",
        "number,title",
        "--template",
        '{{range .}}- #{{.number}} {{.title}}{{"\\n"}}{{end}}',
    )
    issue_all_args = (
        "issue",
        "list",
        "--state",
        "open",
        "--json",
        "number,title,labels",
        "--template",
        '{{range .}}- #{{.number}} {{.title}}{{"\\n"}}{{end}}',
    )

    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="brief") as ex:
        f_branch = ex.submit(_git, repo, "branch", "--show-current")
        f_log = ex.submit(_git, repo, "log", "--oneline", "-5")
        f_repo_nwo = ex.submit(
            _gh, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"
        )
        f_issue_high = ex.submit(_gh, *issue_high_args)
        f_issue_all = ex.submit(_gh, *issue_all_args)

        rc_nwo, repo_nwo_raw = f_repo_nwo.result()
        repo_nwo = repo_nwo_raw.strip() if rc_nwo == 0 else ""
        pr_args = ["pr", "list"]
        if repo_nwo:
            pr_args.extend(["--repo", repo_nwo])
        pr_args.extend(
            [
                "--state",
                "open",
                "--json",
                "number,title,headRefName",
                "--template",
                '{{range .}}- #{{.number}} {{.title}} ({{.headRefName}}){{"\\n"}}{{end}}',
            ]
        )
        f_pr = ex.submit(_gh, *pr_args)

        rc_branch, out_branch, _ = f_branch.result()
        rc_log, out_log, _ = f_log.result()
        rc_pr, out_pr = f_pr.result()
        rc_ih, out_ih = f_issue_high.result()
        rc_ia, out_ia = f_issue_all.result()

    lines: list[str] = []
    lines.append(f"# Brief — {now}")
    lines.append("")
    lines.append("## ブランチ")
    lines.append(out_branch.strip() if rc_branch == 0 else "")
    lines.append("")
    lines.append("## 直近のコミット")
    lines.append(out_log.rstrip() if rc_log == 0 else "")
    lines.append("")

    lines.append("## Open PR")
    lines.append(out_pr.rstrip() if rc_pr == 0 and out_pr.strip() else "（なし）")
    lines.append("")

    lines.append("## Open Issue（priority: high/critical）")
    lines.append(out_ih.rstrip() if rc_ih == 0 and out_ih.strip() else "（なし）")
    lines.append("")

    lines.append("## Open Issue（全件）")
    lines.append(out_ia.rstrip() if rc_ia == 0 and out_ia.strip() else "（なし）")

    brief_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resolve_stop_hook_timeout() -> int:
    """AI_REVIEW_STOP_HOOK_TIMEOUT を int で解決する（Issue #1175）.

    非数値・0 以下・空はデフォルト (_DEFAULT_STOP_HOOK_TIMEOUT_SECONDS) にフォールバック。
    """
    raw = os.environ.get("AI_REVIEW_STOP_HOOK_TIMEOUT", "").strip()
    if not raw or not _DIGITS_RE.match(raw):
        return _DEFAULT_STOP_HOOK_TIMEOUT_SECONDS
    val = int(raw)
    if val <= 0:
        return _DEFAULT_STOP_HOOK_TIMEOUT_SECONDS
    return val


def _profile_enabled() -> bool:
    """ON_STOP_PROFILE=1 が set されているか判定する（Issue #1557）."""
    return os.environ.get("ON_STOP_PROFILE", "0").strip() == "1"


def _log_profile(phase: str, elapsed_ms: float) -> None:
    """profile 有効時のみ phase 所要時間を stderr に出力する（Issue #1557）.

    出力形式: `on-stop: PROFILE: <phase>=<elapsed_ms>ms`（1 行）
    """
    sys.stderr.write(f"on-stop: PROFILE: {phase}={elapsed_ms:.1f}ms\n")


def _run_heavy_work(repo_root: str, brief: Path) -> None:
    """重い処理を並列実行する（Issue #1175）.

    `_cleanup_gone_branches` (fetch --prune + git branch -D) と
    `_cleanup_merged_pr_branches` (git branch -D) は共に refs を書き換えるため、
    並列に走らせると `.git/packed-refs` や個別 ref ロックで衝突しうる。よって
    この 2 phase は 1 スレッドに逐次化し、read-only の `_write_brief` とだけ
    並列実行する。個別 phase の例外は握りつぶす（Stop hook は exit 0 が原則）。

    Issue #1557: ON_STOP_PROFILE=1 なら各 phase の所要時間を stderr に出力する。
    """
    profile = _profile_enabled()
    default_branch = _detect_default_branch(repo_root)

    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 — Stop hook は必ず exit 0
            pass

    def _timed(phase: str, fn, *args) -> None:
        """profile 有効時は phase の所要時間を stderr に出力する."""
        if not profile:
            _safe(fn, *args)
            return
        start = time.monotonic()
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 — Stop hook は必ず exit 0
            pass
        finally:
            _log_profile(phase, (time.monotonic() - start) * 1000.0)

    def _sequential_branch_cleanups() -> None:
        _timed("cleanup_gone_branches", _cleanup_gone_branches, repo_root, default_branch)
        _timed("cleanup_merged_pr_branches", _cleanup_merged_pr_branches, repo_root, default_branch)

    def _write_brief_task() -> None:
        _timed("write_brief", _write_brief, repo_root, brief)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="on-stop") as ex:
        futures = [
            ex.submit(_sequential_branch_cleanups),
            ex.submit(_write_brief_task),
        ]
        for f in futures:
            try:
                f.result()
            except Exception:  # noqa: BLE001
                pass


_MAX_RESUME_ATTEMPTS = 3


def _detect_orphan_issue_next_state(repo_root: str) -> int:
    """Issue #1698: `cache/issue-next-state.json` の孤児 state を検出して auto-resume 指示を注入する.

    孤児 state = `current_issue` に着手中番号があるが、対応する worktree（branch 名に
    `issue-<N>-` を含む）と open PR（本文に `closes #<N>` を含む）の両方が存在しない状態。

    `/issue-next` の STEP 1.5 (品質チェック) で PASS コメント投稿後に turn が終了
    してしまうと発生する（Issue #1558 で実際に確認）。

    旧実装（Issue #1561）の stderr WARN は Claude Code UI に表示されないため無効だった。
    本実装（Issue #1698）では Stop hook exit 2 を返して stop をブロックし、stderr に
    auto-resume 指示（具体的な git worktree add コマンドを含む）を注入する。

    fail-safe: `resume_attempts` が _MAX_RESUME_ATTEMPTS (3) 以上の場合は exit 0 に落とし
    無限ループを防ぐ。

    Returns:
        int: 孤児 state を検出し自動再開を試みる場合は 2、それ以外は 0。
             例外が発生した場合は常に 0（fail-safe）。
    """
    state_path = Path(repo_root) / "cache" / "issue-next-state.json"
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

    # PR 検出: `gh pr list --search "closes #<N> in:body" --state open` が空でなければ存在。
    # gh が未インストールの環境では false positive を避けるため「PR あり」扱いとする
    # （gh 検出できないなら警告を出さない）。
    if shutil.which("gh") is None:
        return 0
    rc_gh, out_gh = _gh(
        "pr",
        "list",
        "--state",
        "open",
        "--search",
        f"closes #{issue_num} in:body",
        "--json",
        "number",
        timeout=8,
    )
    if rc_gh == 0:
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
        state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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


def _read_slack_enabled() -> bool:
    """Slack 通知の有効判定（Issue #1994: hooks-config.json 優先・env var は deprecated）.

    hooks-config.json の ``"on-stop-slack"`` キーを get_hook_config で読む。
    未設定（None）なら env var ``ON_STOP_SLACK_ENABLED`` にフォールバックする。
    """
    try:
        _lib_dir = Path(__file__).resolve().parent / "_lib"
        if str(_lib_dir) not in sys.path:
            sys.path.insert(0, str(_lib_dir))
        from hook_io import get_hook_config  # type: ignore[import-not-found]

        config_value = get_hook_config("on-stop-slack", default=None)
    except Exception:  # noqa: BLE001 — Stop hook は fail-safe
        config_value = None
    if config_value is not None:
        return bool(config_value)
    return os.environ.get("ON_STOP_SLACK_ENABLED", "0") == "1"


def main() -> int:
    raw_stdin = _drain_stdin()

    # Slack 通知判定
    if _read_slack_enabled():
        stop_reason = _extract_stop_reason(raw_stdin)
        webhook = (
            os.environ.get("CLAUDE_WEBHOOK_URL")
            or os.environ.get("SLACK_WEBHOOK_URL")
            or ""
        )
        if stop_reason == "input_required" and webhook:
            _notify_slack(webhook)
            print("on-stop: slack 通知を送信しました (stop_reason=input_required)")
        else:
            # 有効化されていることを毎回可視化する（Issue #1994 受け入れ基準）
            print(
                f"on-stop: slack 通知は有効です（今回は送信条件を満たさず送信せず: "
                f"stop_reason={stop_reason or '(なし)'}, webhook={'設定済み' if webhook else '未設定'}）"
            )

    # リポジトリルート
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    if result.returncode != 0:
        return 0
    repo_root = result.stdout.strip()
    if not repo_root:
        return 0

    # Issue #1698: 孤児 issue-next state 検出は throttle の外で毎回走る。
    # 軽量（state file の JSON parse + git worktree list + gh pr list --search 1 回）で、
    # 孤児 state を検出したら exit 2 + stderr で auto-resume 指示を注入する。
    # 3 回超の場合は fail-safe で exit 0 に落とす。
    try:
        orphan_exit_code = _detect_orphan_issue_next_state(repo_root)
    except Exception:  # noqa: BLE001 — Stop hook は fail-safe で exit 0
        orphan_exit_code = 0
    if orphan_exit_code != 0:
        return orphan_exit_code

    # Issue #1265: 重い処理（fetch --prune・gh 経由の PR 列挙・brief.md 生成）は
    # ON_STOP_THROTTLE_SECONDS ごとにしか実行しない。
    # Stop イベントは会話ターンごとに発火するため、毎回走らせると数秒〜10 秒の遅延になる。
    last_cleanup_file = _resolve_last_cleanup_file()
    throttle_seconds = _resolve_throttle_seconds()
    if not _should_run_cleanup(last_cleanup_file, throttle_seconds):
        return 0

    brief = Path(repo_root) / "cache" / "brief.md"

    # Issue #1175: 重い処理は daemon thread に隔離して全体タイムアウトで打ち切る。
    # 打ち切り時は残った subprocess はプロセス終了で kill される（daemon thread なので
    # Python interpreter の shutdown を止めない）。個別 subprocess の timeout も 8s に
    # 揃えているため、orphan 化しても最大 8s で自然消滅する。
    total_timeout = _resolve_stop_hook_timeout()
    done = threading.Event()

    def _worker() -> None:
        try:
            _run_heavy_work(repo_root, brief)
        finally:
            done.set()

    thread = threading.Thread(target=_worker, name="on-stop-worker", daemon=True)
    thread.start()
    completed = done.wait(timeout=total_timeout)

    # 全 phase 完了時のみタイムスタンプを更新する（部分完了時は次回もリトライさせる）。
    if completed:
        _update_last_cleanup(last_cleanup_file)
    else:
        sys.stderr.write(
            f"on-stop: WARN: heavy work exceeded {total_timeout}s "
            f"(AI_REVIEW_STOP_HOOK_TIMEOUT); skipped remaining work\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
