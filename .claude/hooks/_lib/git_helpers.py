"""hook 共通の git/gh subprocess 実行ヘルパー（Issue #2958・#2967）.

`.claude/hooks/` 内の複数 hook が独立に定義していた ``_git`` wrapper
（timeout 値・戻り値シグネチャがコピペで乖離していた）と、
``git rev-parse --show-toplevel`` によるリポジトリルート解決を
本モジュールへ集約する。

- ``run_git()``: git subprocess を実行し ``subprocess.CompletedProcess`` を返す。
  git バイナリ未検出・timeout 超過時は例外を投げず ``None`` を返す
  （hook の信頼性を担保するため）。
- ``git_toplevel()``: ``git rev-parse --show-toplevel`` の結果（リポジトリルート）を
  返す。git リポジトリでない場合や失敗時は ``None``。
- ``run_git_in_repo()``: ``git -C <repo> <args>`` を実行し ``(returncode, stdout,
  stderr)`` タプルを返す（Issue #2967: 旧 on-stop.py `_git` の移設）。
- ``run_gh()``: ``gh <args>`` を実行し ``(returncode, stdout)`` タプルを返す
  （Issue #2967: 旧 on-stop.py `_gh` の移設。timeout 超過時は stderr に
  "on-stop: WARN: gh timeout ..." を出力する旧実装の挙動をそのまま踏襲する）。
  呼び出し側で timeout WARN の出力を抑制したい場合は ``warn_on_timeout=False`` を
  渡す（Issue #3882: `require-issue-next-completion.py` が連続ブロック時のスライディング
  ウィンドウ抑制に使う。デフォルト値 ``True`` のため既存呼び出し元の挙動は変わらない）。

stdlib のみ使用（hooks ディレクトリは外部依存を持たない）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# 従来の各 hook 実装で使われていた timeout 値のばらつきを集約したデフォルト。
DEFAULT_TIMEOUT_SEC = 10.0
# `git rev-parse --show-toplevel` は軽量コマンドのため短めの timeout を使う。
TOPLEVEL_DEFAULT_TIMEOUT_SEC = 5.0
# 旧 on-stop.py `_git` のデフォルト timeout（Issue #1175 target: 8s 以下を維持）。
# 汎用の DEFAULT_TIMEOUT_SEC（10.0）とは値が異なるため、挙動保持のため専用定数にする。
ON_STOP_GIT_DEFAULT_TIMEOUT_SEC = 8.0
# 旧 on-stop.py `_gh` のデフォルト timeout。
DEFAULT_GH_TIMEOUT_SEC = 8.0


def run_git(
    *args: str,
    cwd: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> subprocess.CompletedProcess[str] | None:
    """``git <args>`` を実行し ``CompletedProcess`` を返す.

    Args:
        *args: git サブコマンド・オプション（例: ``"status", "--short"``）。
        cwd: 実行ディレクトリ。省略時はプロセス CWD。
        timeout: タイムアウト秒数。

    Returns:
        実行に成功すれば ``subprocess.CompletedProcess``（``returncode`` は
        呼び出し側が判定する）。git バイナリが見つからない・timeout 超過の
        場合は例外を送出せず ``None`` を返す。
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def git_toplevel(
    cwd: str | None = None, timeout: float = TOPLEVEL_DEFAULT_TIMEOUT_SEC
) -> str | None:
    """``git rev-parse --show-toplevel`` の結果（リポジトリルート絶対パス）を返す.

    Args:
        cwd: 解決対象ディレクトリ。省略時はプロセス CWD。
        timeout: タイムアウト秒数。

    Returns:
        リポジトリルートの絶対パス。git リポジトリでない・失敗時は ``None``。
    """
    result = run_git("rev-parse", "--show-toplevel", cwd=cwd, timeout=timeout)
    if result is not None and result.returncode == 0:
        stripped = result.stdout.strip()
        if stripped:
            return stripped
    if cwd is not None:
        return None
    return resolve_repo_root(timeout=timeout)


def resolve_repo_root(timeout: float = DEFAULT_TIMEOUT_SEC) -> str | None:
    """プロセス CWD に依存せず、セッションのリポジトリルートを解決する."""
    candidates: list[str] = []
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        candidates.append(project_dir)
    candidates.append(str(Path(__file__).resolve().parents[3]))
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        result = run_git(
            "-C", candidate, "rev-parse", "--show-toplevel", timeout=timeout
        )
        if result is not None and result.returncode == 0:
            root = result.stdout.strip()
            if root:
                return root
    return None


def run_git_in_repo(
    repo: str, *args: str, timeout: float = ON_STOP_GIT_DEFAULT_TIMEOUT_SEC
) -> tuple[int, str, str]:
    """``git -C <repo> <args>`` を実行し ``(returncode, stdout, stderr)`` を返す.

    Issue #2967: on-stop.py の自前 `_git()` を移設。`run_git()` は ``None`` を
    返しうるが、呼び出し側の分岐を単純化するため本関数は失敗時に
    ``(1, "", "")`` を返す（例外を送出しない）。

    Args:
        repo: 対象リポジトリのパス（``git -C`` に渡す）。
        *args: git サブコマンド・オプション。
        timeout: タイムアウト秒数。

    Returns:
        ``(returncode, stdout, stderr)``。git バイナリ未検出・timeout 超過時は
        ``(1, "", "")``。
    """
    result = run_git("-C", repo, *args, timeout=timeout)
    if result is None:
        return 1, "", ""
    return result.returncode, result.stdout, result.stderr


def run_gh(
    *args: str,
    timeout: float = DEFAULT_GH_TIMEOUT_SEC,
    warn_on_timeout: bool = True,
) -> tuple[int, str]:
    """``gh <args>`` を実行し ``(returncode, stdout)`` を返す.

    Issue #2967: on-stop.py の自前 `_gh()` を移設（挙動変更なし）。

    Args:
        *args: gh サブコマンド・オプション。
        timeout: タイムアウト秒数。
        warn_on_timeout: timeout 超過時に stderr へ WARN を出力するか
            （Issue #3882: 呼び出し側でスライディングウィンドウ抑制を行う場合に
            ``False`` を渡す。デフォルト ``True`` は従来の挙動と同じ）。

    Returns:
        ``(returncode, stdout)``。gh バイナリ未検出時は ``(1, "")``。
        timeout 超過時は（``warn_on_timeout`` が True なら）stderr に WARN を
        出力したうえで ``(1, "")``。
    """
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 1, ""
    except subprocess.TimeoutExpired:
        if warn_on_timeout:
            sys.stderr.write(
                f"on-stop: WARN: gh timeout ({timeout}s) for: gh {' '.join(args)}\n"
            )
        return 1, ""
    return result.returncode, result.stdout
