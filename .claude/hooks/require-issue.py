#!/usr/bin/env python3
"""PreToolUse hook: git commit に Issue 参照キーワードが含まれるか確認する.

NO TICKET NO WORK — Issueなしのコミットをブロックする（exit 2）。
cache 優先で Issue 存在を確認し、TTL 切れ時は stale を即返しつつ
バックグラウンドで gh subprocess を起動する（Issue #1312 → #1393 stale-while-revalidate）。

#1969: rate limit 判定（_get_rate_limit_remaining）は cache 完全 miss 時のみ実行する。
stale hit 時は BG refresh 側（gh_cache_refresh.py）が quota 保護を担う。

Python 化（Phase 4 / #1057）。旧 require-issue.sh の振る舞いを 1:1 で踏襲する。
stdlib のみ使用。

#2638: 受理キーワードを closes のみから closes / fixes / resolves / refs に拡張する。
refs は GitHub のクローズキーワードではないため、決定記録など未実装 Issue の参照に使用できる。

Issue #2958: 対象リポジトリ CWD の解決を `_lib.hook_io.resolve_target_cwd()` へ移行する。
従来の `_resolve_target_repo_path` はコマンド文字列内の `cd <path> &&` / `git -C <path>`
しか見ておらず、payload の `cwd` フィールド（Bash 永続 CWD が worktree にあり、
コマンド自体にはパス指定がないケース）を無視していた。

PR #3026 codex レビュー指摘: `resolve_target_cwd()` は候補が 1 つもなければプロセス
CWD へフォールバックし常に非 None を返すため、戻り値をそのまま ``_issue_exists()`` の
``repo_path`` に渡すと、cd / -C / payload cwd のいずれも指定がない通常のコミットでも
常に ``repo_path`` 指定ありの経路（direct verify・cache 未使用）に入ってしまい、
fresh/stale cache と rate-limit bypass 経路（#1393 / #1969）が使われなくなる。
解決先がプロセス CWD 自身と一致する場合（＝実質「指定なし」）は ``repo_path=None``
を渡し、従来どおり cache 優先経路を使う。解決先がプロセス CWD と異なる場合のみ
（cd / -C / payload cwd による明示的なクロスリポジトリ指定）direct verify を使う。

Issue #3717: 本 hook は「セッションを開いたリポジトリ」の NO TICKET NO WORK 強制を
目的とする。Claude Code セッションが repo A で開かれている状態で repo B（別 git
リポジトリ）のファイルをコミットする場合、repo A のルールを repo B の操作へ適用しない。
コミット対象リポジトリがセッション CWD のリポジトリと異なる場合は skip（exit 0）する。
同一リポジトリの別 worktree は「同じリポジトリ」として扱う（common git dir 一致判定）。

Issue #4016: `_CLOSES_RE.search(command)` は従来コマンド文字列**全体**に対して 1 回だけ
実行されていたため、1 回の Bash 呼び出しで複数の `git commit` を `&&`/`;` で連結すると、
そのうち 1 つにでも Issue 参照があれば他の参照なしコミットも素通りしていた。
`git commit` invocation 単位にコマンド文字列を分割し、それぞれ独立して
Issue 参照の有無・Issue 存在を判定する（#4023 codex レビュー指摘を反映し、
`shlex` によるクォート考慮のトークン化で各セグメントを invocation 自身
（次の top-level `&&`/`;`/`||`/`|` まで）に限定し、間に挟まる非 commit
コマンドの文字列が漏れ込まないようにしている）。

#4023 codex レビュー指摘（3 件目）: `_is_outside_session_repo()` も従来コマンド文字列
全体に対して 1 回だけ判定していたため、複数 `git commit` のうち先頭が
`git -C <外部リポジトリ> commit` だと、後続の自リポジトリ向け commit まで
まとめて skip され Issue 参照なしでも NO TICKET NO WORK をバイパスできていた。
`_split_git_commit_segments()` が invocation ごとに算出する `context`
（`cd` の持続的な作用と `git -C` の invocation 局所的な作用を区別した文字列）を
使って、outside-repo 判定・対象リポジトリ解決の両方を invocation ごとに
独立して行う。

escape hatch: 環境変数 `SKIP_REQUIRE_ISSUE_GATE=1` でチェックを全スキップする。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.gh_cache import get_issue as _get_issue_fresh
from _lib.gh_cache import (
    get_issue_or_stale_with_bg_refresh as _get_issue_swr,
)
from _lib.gh_cache import upsert_issue as _upsert_issue
from _lib.gh_command import CLOSES_OR_REFS_RE as _CLOSES_RE
from _lib.hook_io import (
    get_command,
    is_hook_enabled,
    read_hook_input,
    resolve_target_cwd,
)

# 旧 sh: grep -qE '(^|&&|;)\s*git commit(\s|$)'
# Issue #2226: `git -C <path> commit` 形式（クロスリポジトリコミット）も検出対象に含める。
# Issue #4023 codex/ai-reviewer subagent レビュー指摘: `_split_git_commit_segments()` は
# `||`/`|` も top-level 演算子として分割対象にしているが、本ガードが `(^|&&|;)` のみを
# 見ていたため、唯一の `git commit` が `||`/`|` の直後にしかない場合（例:
# `false || git commit -m "..."`）にガードが False を返し、`_split_git_commit_segments()`
# が早期に空リストを返して NO TICKET NO WORK ゲート自体が完全にバイパスされていた。
# `||`/`|` の直後も検出対象に含める。
# Issue #4023 codex レビュー指摘（4 件目）: 改行も Bash のコマンド区切りなので、
# 唯一の `git commit` が改行直後にしかない場合（例: `echo start\ngit commit -m "..."`）
# もガードが検出するよう `\n` を区切りの選択肢に含める。
# Issue #4023 claude-code レビュー指摘（6 件目）: 単独 `&`（バックグラウンド演算子）も
# Bash の top-level 区切りで `_TOP_LEVEL_SEPARATOR_CHARS` は既に対応済みだったが、
# 本ガードの分離子集合に無かったため `echo hi & git commit -m "..."` のように唯一の
# `git commit` が単独 `&` の直後にしかない場合にガードが False を返していた。
# `&&` を先に並べたまま末尾に単独 `&` を追加する（`&&` の優先マッチを崩さない）。
#
# Issue #4060: 上記の区切り文字（先頭/`&&`/`;`/`||`/`|`/改行/単独`&`）のいずれも
# `()` サブシェル・`if`/`for`/`while`/`case` 等の制御構造キーワード・`$(...)` コマンド
# 置換の「開き」を考慮していなかった。対象コマンドがこれらの直後（`cd ... &&` を挟ま
# ない単純な形）に現れる場合、`git` の直前に上記区切り文字が存在しないため本ガードが
# False を返し、`_split_git_commit_segments()` が早期に空リストを返して NO TICKET NO
# WORK ゲート自体が完全にバイパスされていた（fail-open。実測: Issue #4055 の実装中に
# 発見）。本ガードは「tokenize すべきか」を高速に判定する前置フィルタに過ぎず、実際の
# invocation 境界判定は `_split_git_commit_segments()` 側の `_strip_leading_shell_noise()`
# が担うため、区切り文字集合を列挙し続ける代わりに語境界のみを要求する形へ単純化し、
# 上記の構文でも tokenize 側の判定へ確実に処理を渡すようにする（`docs/decisions/
# 2026-08-19-issue-4055-require-issue-shell-parser-boundary.md` の決定 B に基づく
# 最小限の修正・フルシェルパーサー導入は見送り）。
_GIT_COMMIT_RE = re.compile(r"\bgit(?:\s+-C\s+\S+)?\s+commit\b")
# 旧 sh: grep -qiE 'closes #[0-9]+'
# #2638: closes / fixes / resolves / refs を受理する。
# refs は GitHub のクローズキーワードではないため、決定記録など未実装 Issue の参照に使用できる。
# #2653: word boundary を追加し、単語の一部として現れた文字列（例: unrefs / dereferences）を拒否する。
# Issue #2952: パターン実体は `_lib/gh_command.CLOSES_OR_REFS_RE` へ集約（refs を含む変種）。

# Issue #1463: adaptive stale TTL の閾値
_RATE_LIMIT_NEAR_EXCEEDED = 5

# escape hatch（Issue #3717）: 1 を設定すると本 hook のチェックを全スキップする.
_ESCAPE_HATCH_ENV = "SKIP_REQUIRE_ISSUE_GATE"


def _get_rate_limit_remaining() -> int | None:
    """Issue #1463: `gh api rate_limit --jq .resources.core.remaining` で remaining を返す.

    取得失敗時は None。
    #1969: cache 完全 miss 時のみ呼び出す（stale hit 時は BG refresh 側が quota 保護を担う）。
    """
    try:
        result = subprocess.run(
            ["gh", "api", "rate_limit", "--jq", ".resources.core.remaining"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    stripped = result.stdout.strip()
    if not stripped.isdigit():
        return None
    return int(stripped)


_RATE_LIMIT_SENTINEL: dict[str, str] = {"__rate_limit_exceeded__": "true"}


def _verify_issue_via_gh(issue_number: int, cwd: str | None = None) -> dict | None:
    """gh subprocess で Issue 存在を確認する。存在すれば dict を返す。

    Issue #1463: `gh` の stderr に "rate limit" が含まれる場合は
    ``_RATE_LIMIT_SENTINEL`` を返して呼び出し側で bypass 経路に流す。
    通常の失敗（Issue が存在しない・network error 等）は従来通り None。
    Issue #2226: ``cwd`` 指定時はそのディレクトリで `gh issue view` を実行する
    （クロスリポジトリコミット時に対象リポジトリを照会するため）。
    """
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--json", "number,title,state"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
            cwd=cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        # Issue #1463: rate limit exhaustion を stderr パターンで検知 → sentinel を返す
        if "rate limit" in (result.stderr or "").lower():
            sys.stderr.write(
                "GitHub API のリクエスト制限に達したため、一時的に Issue 確認をスキップします。\n"
            )
            return _RATE_LIMIT_SENTINEL
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _issue_exists(issue_number: int, repo_path: str | None = None) -> bool:
    """Issue の存在を確認する（#1393 SWR + #1463 adaptive TTL + #1969 rate limit 判定の遅延化）。

    Issue #2226: ``repo_path`` 指定時（コマンドが `cd <path> &&` / `git -C <path>` で
    セッション CWD と異なるリポジトリを対象にしている場合）は、セッション CWD の
    gh-cache（別リポジトリのキャッシュ）を使わず、対象リポジトリで直接 `gh issue view`
    を実行して存在確認する。

    優先順（``repo_path`` 未指定時。従来どおり）:
    1. fresh cache hit → 即 True
    2. stale cache hit → stale を即返し BG refresh 起動（rate limit guard は BG 側 gh_cache_refresh.py が行う。#1969）
    3. cache 完全 miss → rate limit 判定
       - remaining < 5 → allow without verification（一時 bypass・stderr 通知）
       - それ以外 → 同期 gh fetch（rate limit 枯渇 sentinel は bypass）

    Issue #2958: ``repo_path`` は呼び出し元（``_main_impl``）が
    ``_lib.hook_io.resolve_target_cwd()`` で解決するため、非 None 時は必ず実在する
    ディレクトリである（実在しない候補は ``resolve_target_cwd()`` 内部で silent に
    skip 済み）。よって本関数側での存在チェック・フォールバック WARN は不要（撤去）。
    """
    if repo_path is not None:
        data = _verify_issue_via_gh(issue_number, cwd=repo_path)
        if data is _RATE_LIMIT_SENTINEL:
            return True
        return data is not None

    # 1. fresh cache hit（既存テスト互換のため独立チェックを維持）
    fresh = _get_issue_fresh(issue_number)
    if fresh is not None:
        return True

    # 2. SWR: stale hit なら BG refresh を起動して即 True（同期 subprocess なし。#1969）
    cached = _get_issue_swr(issue_number)
    if cached is not None:
        return True

    # 3. cache 完全 miss のみ rate limit を同期照会（直後に同期 gh fetch する経路なので許容）
    remaining = _get_rate_limit_remaining()
    if remaining is not None and remaining < _RATE_LIMIT_NEAR_EXCEEDED:
        sys.stderr.write(
            f"GitHub API のリクエスト制限に近づいています（残り {remaining} 回）。"
            f"一時的に Issue 確認をスキップします。\n"
        )
        return True

    data = _verify_issue_via_gh(issue_number)
    if data is _RATE_LIMIT_SENTINEL:
        return True
    if data is not None:
        _upsert_issue(issue_number, data)
        return True
    return False


def _session_repo_root() -> str | None:
    """セッション CWD（process CWD）の git リポジトリルートを返す（Issue #3717）.

    `git rev-parse --show-toplevel` は cwd 基準で解決されるため、対象リポジトリの
    ルートは ``_target_repo_root`` で対象パスから引き直す。取得失敗時は None。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _target_repo_root(payload: dict, command: str) -> str | None:
    """コミット対象リポジトリのルートを返す（Issue #3717）.

    `resolve_target_cwd()` で解決したディレクトリから ``git rev-parse --show-toplevel``
    でリポジトリルートを引き直す（cwd 基準の解決を対象パス基準へ引き直す）。
    対象が git リポジトリでない・失敗時は None。
    """
    resolved_cwd = resolve_target_cwd(payload, command)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
            cwd=resolved_cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _repo_identity(repo_root: str) -> str | None:
    """リポジトリの同一性識別子（common git dir の絶対パス）を返す（Issue #3717）.

    同一リポジトリの全 worktree で共通の gitdir を返すため、worktree / メイン
    チェックアウトをまたいだ「同一リポジトリ」判定に使える。相対パス（メイン
    チェックアウトでは `.git`）は ``repo_root`` 起点に解決する。失敗時は None。
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    common = result.stdout.strip()
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.join(repo_root, common)
    return os.path.normpath(os.path.abspath(common))


def _is_outside_session_repo(payload: dict, command: str) -> bool:
    """コミット対象リポジトリがセッション CWD のリポジトリと異なるかを返す（Issue #3717）.

    セッション CWD（process CWD）のリポジトリとコミット対象リポジトリが「同じ
    リポジトリ」でない場合 True を返す（本 hook の対象外として skip する）。
    同一リポジトリの別 worktree は同じ扱い。

    セッション CWD が git リポジトリ外・対象リポジトリが判定不能の場合は False
    （従来挙動を維持し、自リポジトリの NO TICKET NO WORK を弱めない）。
    """
    session_root = _session_repo_root()
    if session_root is None:
        return False
    target_root = _target_repo_root(payload, command)
    if target_root is None:
        return False
    session_identity = _repo_identity(session_root)
    target_identity = _repo_identity(target_root)
    if session_identity is None or target_identity is None:
        return False
    return session_identity != target_identity


# Issue #4023 codex レビュー指摘（4 件目）: Bash では改行も `;` と同等のコマンド
# 区切りなので、`&`/`;`/`|` と同じ punctuation として扱う。shlex は連続する
# punctuation を 1 トークンにまとめる（`&&\n` 等）ため、トークン全体がこの文字
# 集合のみで構成されるかどうかで区切り判定する。
_TOP_LEVEL_SEPARATOR_CHARS = frozenset("&;|\n")

# Issue #4023 codex レビュー指摘（7 件目 / 10 件目）: `cd` 自身の実行到達を保証する
# 区切り演算子。`;` / 改行 / 単独 `&` の直後（およびコマンド先頭 = 空文字）の `cd` は
# 親シェルで無条件に実行される（単独 `&` は「直前」のコマンドをバックグラウンド化する
# 演算子であり、「直後」のコマンドの実行到達性には影響しない）。
_CD_REACHABLE_OPERATORS = frozenset(("", ";", "\n", "&"))

# Bash の top-level 区切り演算子（長い順。`_leading_separator_operator()` 用）
_SEPARATOR_OPERATORS = ("&&", "||", ";", "|", "&", "\n")

# `cd` の実行到達性の 3 分類（Issue #4023 レビュー指摘 8 件目）
_CD_REACH_CERTAIN = "certain"  # コマンド先頭 / `;` / 改行の直後 → 必ず実行される
_CD_REACH_CONDITIONAL = "conditional"  # `&&` の右辺 → 左辺の成否次第
_CD_REACH_NONE = "none"  # `||` / `|` / 単独 `&` の直後 → 短絡・サブシェル

# Issue #4023 レビュー指摘（9 件目）: `cd` を「終端する」区切りに単独 `&`（バックグラウンド）
# または `|`（パイプライン）が含まれる場合、`cd` はサブシェルで実行され親シェルの CWD を
# 変えないため後続 invocation へ効果が及ばない（`&&`/`||` は親シェル実行なので対象外）。
_CD_SUBSHELL_TERMINATOR_CHARS = frozenset("&|")


def _leading_separator_operator(separator: str) -> str:
    """区切りトークン列の先頭にある Bash 演算子を返す（Issue #4023 レビュー指摘 10 件目）.

    shlex は連続する punctuation を 1 トークンにまとめる（`&&\\n` / `;\\n` / `&\\n` 等）ため、
    区切り文字列は複数演算子の連結になりうる。直前コマンドと後続コマンドを実際に結合する
    のは**先頭**の演算子で、それに続く改行は継続行・空行にすぎない（`a &&\\n b` は `&&`、
    `a &\\n b` は `&` の意味論）。文字集合での判定は `&&` と単独 `&` を区別できないため、
    先頭演算子を取り出して判定する。
    """
    for operator in _SEPARATOR_OPERATORS:
        if separator.startswith(operator):
            return operator
    return ""


def _cd_reachability(separator: str) -> str:
    """直前の区切りから見た `cd` の実行到達性を 3 分類で返す（Issue #4023）.

    レビュー指摘 5 件目の修正は「`cd` から commit までの区切りがすべて `&&` か」
    だけを見ており、`cd` 自身が実行されるかを見ていなかった。Bash は `&&`/`||` を
    同一優先度で左から評価するため `true || cd /other && git commit ...` は
    `((true || cd /other) && git commit ...)` となり、`cd` は短絡評価で実行されない
    まま commit がセッション CWD のリポジトリで走る（`|` の右辺はパイプラインの
    サブシェル実行のため `cd` の効果が後続へ及ばない）。

    レビュー指摘 8 件目: 一方で `&&` 直後の `cd`（`_CD_REACH_CONDITIONAL`）と
    コマンド先頭・`;` / 改行直後の `cd`（`_CD_REACH_CERTAIN`）は区別が必要。前者は
    「未実行の可能性があるが、後続も `&&` 連鎖なら commit 自体も実行されない」と
    いう条件付きの安全性しかないため `&&` 連鎖が切れた時点で引き継ぎを打ち切るが、
    後者は必ず実行されるので `;` / 改行を越えても効果が持続する（`cd <外部リポジトリ>
    && git add -A; git commit ...` の commit は外部リポジトリで走る）。

    レビュー指摘 10 件目: 単独 `&` は「直前」のコマンドをバックグラウンド化する演算子で
    あり、「直後」のコマンドは `;` と同様に親シェルで無条件に実行される
    （`sleep 1 & cd <外部リポジトリ> && git commit ...` は `sleep 1 &` と
    `cd <外部リポジトリ> && git commit ...` の 2 リストに分解され、後者は親シェルの
    フォアグラウンドで走るので `cd` は親シェルの CWD を変える）。従来は文字集合による
    判定で `&&` と単独 `&` を区別できず、後者を `_CD_REACH_NONE` として `cd` を伝播
    させなかったため、別リポジトリの commit を自リポジトリ扱いで誤ブロックしていた
    （Issue #3717 の outside-repo skip 契約違反）。なお `cd` を「終端する」単独 `&`
    （`cd /x & ...`）は従来どおり `_cd_effect_persists()` 側で除外する。
    """
    operator = _leading_separator_operator(separator)
    if operator == "&&":
        return _CD_REACH_CONDITIONAL
    if operator in _CD_REACHABLE_OPERATORS:
        return _CD_REACH_CERTAIN
    return _CD_REACH_NONE


def _cd_effect_persists(terminator: str) -> bool:
    """`cd` を終端する区切りから見て `cd` の効果が後続へ及ぶかを返す（Issue #4023）.

    レビュー指摘 9 件目: 指摘 7/8 件目の `_cd_reachability()` は `cd` の「直前」の
    区切りしか見ていなかったため、`cd <外部リポジトリ> & git commit ...` のように
    `cd` が単独 `&` で終端されるケースを `_CD_REACH_CERTAIN` として伝播させていた。
    Bash では単独 `&`（バックグラウンド）・`|`（パイプライン）で終端された `cd` は
    サブシェルで実行され親シェルの CWD を変えないため、後続 invocation の commit は
    セッション CWD のリポジトリで走る。`&&`/`||`/`;`/改行 で終端された `cd` は
    親シェルで実行されるため効果が持続する。
    """
    if terminator.startswith(("&&", "||")):
        return True
    return not any(char in _CD_SUBSHELL_TERMINATOR_CHARS for char in terminator)


# Issue #4060: shlex トークン化後、各 top-level コマンドの先頭が `()` サブシェル・
# 制御構造キーワード・`$(...)` コマンド置換の「開き」であるために `cd`/`git` の
# 検出が失敗していたケースを解消する。1 行の `if`/`while`/`until`/`select` 文は
# 条件部と本体が `;`（top-level 区切り）で分かれるため、本体側チャンクの先頭には
# `then`/`do` キーワードだけが残る（例: `if true; then git commit ...; fi` の
# `;` 分割後チャンクは `['then', 'git', 'commit', ...]`）。`elif`/`else` も同様。
_LEADING_NOISE_KEYWORDS = frozenset(
    {"if", "then", "elif", "else", "do", "while", "until", "select"}
)

# Issue #4060: 先頭トークンの `()`/`$()` 開き括弧（変数代入 `NAME=$(` を含む）を
# 除去する。`+` により `((` のような連続した開きにも対応する。
_OPENER_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=)?(?:\$\(|\()+")

# Issue #4060 codex レビュー指摘: `commit` に引数がなく `()` サブシェルの閉じ括弧が
# 直後に隣接する場合（例: `(git commit)`）、shlex の `punctuation_chars` に `)` を
# 含めていないため `commit` トークン自体に `)` が結合され（`"commit)"`）、
# `cmd_tokens[idx] == "commit"` の完全一致判定に失敗して invocation が検出できず
# ゲートがバイパスされていた。`commit` の直後に閉じ括弧のみが連続する場合はそれを
# 除去してから比較する。
_TRAILING_CLOSER_RE = re.compile(r"^\)+$")


def _strip_leading_shell_noise(cmd_tokens: list[str]) -> list[str]:
    """top-level コマンドの先頭から制御構造キーワード・`case` パターン・

    `()`/`$()` の開き括弧を取り除き、対象コマンド（`cd`/`git` 等）自身から
    始まるトークン列を返す（Issue #4060）。

    対応する構文（cmd_tokens が top-level コマンド 1 つのチャンクである前提。
    `for`/`while` の条件部・`case` の subject 等、対象コマンドではない先頭ノイズ
    のみを対象とし、フルシェルパーサーは実装しない）:

    - `()` サブシェル: `(git commit ...)` / `( git commit ... )` /
      `true && (git commit ...)`
    - 1 行の `if`/`elif`/`else`/`while`/`until`/`select` 文: `;` 分割後チャンクの
      先頭に残る `then`/`do` キーワード
    - `case <subject> in <pattern>) ...`: `case`・subject・`in`・`<pattern>)` を
      1 セットとして読み飛ばす（複数パターン `pat1|pat2)` 等は対象外）
    - 変数代入を伴うコマンド置換: `NAME=$(git commit ...)`

    `echo $(git commit ...)` のように対象コマンドが他コマンドの**引数位置**に
    埋め込まれたコマンド置換は対象外のまま（`docs/reference/hooks.md` の
    「対応外構文」参照）。
    """
    if not cmd_tokens:
        return cmd_tokens
    tokens = list(cmd_tokens)
    if tokens[0] == "case":
        idx = 1
        if idx < len(tokens):
            idx += 1  # <subject>
        if idx < len(tokens) and tokens[idx] == "in":
            idx += 1
            if idx < len(tokens) and tokens[idx].endswith(")"):
                idx += 1  # <pattern>)
        tokens = tokens[idx:]
    else:
        idx = 0
        while idx < len(tokens) and tokens[idx] in _LEADING_NOISE_KEYWORDS:
            idx += 1
        tokens = tokens[idx:]
    if not tokens:
        return tokens
    stripped_head = _OPENER_RE.sub("", tokens[0], count=1)
    if stripped_head:
        tokens[0] = stripped_head
    else:
        # 開き括弧のみのトークン（例: `(` が単独トークン）は取り除く。
        tokens = tokens[1:]
    return tokens


class _CommitSegment(NamedTuple):
    """1 つの `git commit` invocation とその対象リポジトリ解決用コンテキスト.

    Issue #4023 codex レビュー指摘（3 件目）: `message` は Issue 参照キーワード
    判定に使う invocation 自身の文字列。`context` は `_is_outside_session_repo()` /
    `resolve_target_cwd()` に渡す文字列で、シェルの `cd` が同一コマンドライン上の
    以降の invocation へ持続的に作用する一方 `git -C <path>` は当該 invocation
    のみに作用する、という意味論の違いを反映する（`git -C` を持つ invocation は
    `message` 自身がそのまま `context` になり、持たない invocation は直前の
    `cd <path>` を合成した文字列になる）。
    """

    message: str
    context: str


def _split_git_commit_segments(command: str) -> list[_CommitSegment]:
    """`git commit` invocation 単位でコマンド文字列を分割する（Issue #4016 / #4023）.

    各セグメントは対応する `git commit` invocation 自身（次の top-level
    `&&`/`;`/`||`/`|` まで）に限定される。`git commit` invocation が 1 つも
    見つからない場合は空リストを返す。1 回の Bash 呼び出しで複数 `git commit`
    を連結したコマンドに対して、Issue 参照キーワードの有無を invocation
    ごとに独立して判定するために使う。

    Issue #4023 codex レビュー指摘: 従来は「次の `git commit` invocation の
    開始位置まで」をセグメント範囲としていたため、間に挟まる非 commit
    コマンド（例: `echo "refs #1"`）の文字列が手前の `git commit` セグメント
    に漏れ込み、Issue 参照なしの commit を誤って通過させてしまっていた。
    `shlex`（stdlib）でクォート考慮のトークン化を行い、top-level 演算子
    （`&&`/`;`/`||`/`|`）でコマンドを分割してから `git commit` invocation
    のみを抽出することで、この漏れ込みを防ぐ。

    Issue #4023 codex レビュー指摘（3 件目）: `cd <path> &&` はシェル上同一
    コマンドライン内の以降の invocation にも持続的に作用するため、直前に
    現れた `cd` の対象を各セグメントの `context` に引き継ぐ（`git -C <path>`
    を持つセグメントは自身の `-C` を優先し `cd` を引き継がない）。

    Issue #4023 codex レビュー指摘（4 件目）: 改行も Bash の top-level 区切り
    なので区切り文字に含める（従来は shlex の whitespace 扱いで、改行区切りの
    複数 `git commit` が 1 invocation に潰れて見逃されていた）。クォート内の
    改行は区切りにならないため、複数行コミットメッセージは従来どおり通る。

    Issue #4023 codex レビュー指摘（5 件目）: `cd` は実行されるとは限らない
    （`false && cd /other; git commit ...` では短絡評価で `cd` が走らず commit は
    セッション CWD のリポジトリで動く）。`cd` の効果が commit 実行時点で確実に
    有効なのは `cd` から当該 invocation までの top-level 区切りがすべて `&&` の
    場合のみなので（`&&` なら `cd` 未実行・失敗時に後続 commit も実行されない）、
    `&&` 以外の区切りが挟まった時点で引き継ぎを打ち切る（fail-closed で自
    リポジトリ向けとして扱い NO TICKET NO WORK を適用する）。

    Issue #4023 codex レビュー指摘（7 件目）: `cd` 自身も実行される保証がない
    （`true || cd /other && git commit ...` は `((true || cd /other) && git
    commit ...)` と評価され `cd` だけが短絡される）。`cd` の直前の区切りが実行
    到達を保証しない場合（`||`・`|`・単独 `&`）は `cd` を引き継がない
    （`_cd_reachability()`）。

    Issue #4023 codex レビュー指摘（8 件目）: 指摘 5 件目の「`&&` 以外の区切りで
    引き継ぎを打ち切る」は必ず実行される `cd`（コマンド先頭・`;` / 改行直後）にも
    適用されていたため、`cd <外部リポジトリ> && git add -A; git commit ...` の
    commit を自リポジトリ扱いでブロックし、別リポジトリの commit は skip する
    既存契約（Issue #3717）に反していた。Bash では実行された `cd` の効果は同一
    コマンドライン上で `;` / 改行を越えても持続するため、`_CD_REACH_CERTAIN` の
    `cd` は区切り種別によらず引き継ぎ、`&&` 右辺の条件付き `cd` のみ `&&` 連鎖が
    切れた時点で打ち切る。

    Issue #4023 codex レビュー指摘（9 件目）: 指摘 7/8 件目は `cd` の「直前」の区切り
    しか見ていなかったため、`cd <外部リポジトリ> & git commit ...` のように単独 `&`
    で終端された `cd`（バックグラウンドのサブシェル実行で親シェルの CWD は変わらない）
    まで引き継いでいた。`cd` を終端する区切りも見て、サブシェル実行になる単独 `&` /
    `|` の場合は引き継がない（`_cd_effect_persists()`）。
    """
    if not _GIT_COMMIT_RE.search(command):
        return []

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="&;|\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        # クォート不整合等で tokenize に失敗した場合は block-by-default とし、
        # コマンド文字列全体を 1 セグメントとして扱う（判定不能を素通りさせない）。
        return [_CommitSegment(message=command, context=command)]

    # 各 top-level コマンドを「直前の区切り文字列」と対で保持する（Issue #4023
    # レビュー指摘 5 件目: `cd` の効果を引き継いでよいか区切り種別で判断する）。
    top_level_commands: list[tuple[str, list[str]]] = []
    current: list[str] = []
    pending_sep = ""
    for tok in tokens:
        if tok and all(char in _TOP_LEVEL_SEPARATOR_CHARS for char in tok):
            if current:
                top_level_commands.append((pending_sep, current))
                current = []
                pending_sep = tok
            else:
                # 連続する区切りトークン（`;` + 改行 等）は 1 つの区切りとして扱う
                pending_sep += tok
            continue
        current.append(tok)
    if current:
        top_level_commands.append((pending_sep, current))

    segments: list[_CommitSegment] = []
    cd_context: str | None = None
    # レビュー指摘 8 件目: 引き継ぎ中の `cd` が「必ず実行される位置にあるか」を保持する。
    # 必ず実行される `cd` の効果は `;` / 改行を越えても持続するが、`&&` 右辺の `cd` は
    # `&&` 連鎖が切れた時点で保証を失う。
    cd_is_certain = False
    for index, (separator, cmd_tokens) in enumerate(top_level_commands):
        if not cmd_tokens:
            continue
        # Issue #4060: 対象コマンドが `()` サブシェル・制御構造キーワード・
        # `$(...)` コマンド置換の先頭に現れる場合、チャンクの先頭トークンが
        # `cd`/`git` そのものではなくこれらの「開き」になっているため、
        # 先頭ノイズを取り除いてから判定する。
        cmd_tokens = _strip_leading_shell_noise(cmd_tokens)
        if not cmd_tokens:
            continue
        # 条件付き（`&&` 右辺）の `cd` は `&&` 以外の区切りを越えると成功の保証が
        # 切れるため引き継ぎを破棄する（fail-closed で自リポジトリ向けとして扱う）
        if not cd_is_certain and not separator.startswith("&&"):
            cd_context = None
        if cmd_tokens[0] == "cd" and len(cmd_tokens) > 1:
            # レビュー指摘 7 件目: `cd` 自身が実行される保証のない区切り
            # （`||`・`|`・単独 `&` の直後）では引き継がない（fail-closed）
            reach = _cd_reachability(separator)
            # レビュー指摘 9 件目: `cd` を終端する区切りが単独 `&`（バックグラウンド）
            # または `|`（パイプライン）ならサブシェル実行で親シェルの CWD は変わらない
            terminator = (
                top_level_commands[index + 1][0]
                if index + 1 < len(top_level_commands)
                else ""
            )
            if reach == _CD_REACH_NONE or not _cd_effect_persists(terminator):
                cd_context = None
                cd_is_certain = False
            else:
                cd_context = cmd_tokens[1]
                cd_is_certain = reach == _CD_REACH_CERTAIN
            continue
        if cmd_tokens[0] != "git":
            continue
        idx = 1
        has_dash_c = False
        if len(cmd_tokens) > idx + 1 and cmd_tokens[idx] == "-C":
            idx += 2
            has_dash_c = True
        if len(cmd_tokens) > idx and cmd_tokens[idx] != "commit":
            commit_token = cmd_tokens[idx]
            if commit_token.startswith("commit") and _TRAILING_CLOSER_RE.fullmatch(
                commit_token[len("commit") :]
            ):
                cmd_tokens = list(cmd_tokens)
                cmd_tokens[idx] = "commit"
        if len(cmd_tokens) > idx and cmd_tokens[idx] == "commit":
            message = shlex.join(cmd_tokens)
            if has_dash_c or cd_context is None:
                context = message
            else:
                context = f"cd {shlex.quote(cd_context)} && {message}"
            segments.append(_CommitSegment(message=message, context=context))
    return segments


def _main_impl() -> tuple[int, dict[str, object]]:
    # Issue #1292: PreToolUse schema で payload を検証（不一致時は exit 2）
    payload = read_hook_input(hook_name="PreToolUse")
    command = get_command(payload)
    if not command:
        return 0, {"skip_reason": "no_command"}
    segments = _split_git_commit_segments(command)
    if not segments:
        return 0, {"skip_reason": "not_git_commit"}

    # Issue #4016: `git commit` invocation ごとに独立して Issue 参照の有無・
    # Issue 存在を判定する（1 回の Bash 呼び出しに複数 `git commit` が含まれ、
    # 一部にのみ Issue 参照があるケースの見逃しを防ぐ）。
    #
    # Issue #3717 / #4023 codex レビュー指摘（3 件目）: コミット対象リポジトリが
    # セッション CWD のリポジトリと異なるかどうか（本 hook の対象外判定）も、
    # 各 invocation の `context`（`cd`/`git -C` の意味論を反映した文字列）を
    # 使って invocation ごとに独立して判定する。以前はコマンド文字列全体に
    # 対して 1 回だけ判定していたため、複数 `git commit` のうち先頭が別リポジトリ
    # 向け（`git -C <外部パス> commit`）だと、後続の自リポジトリ向け commit まで
    # まとめて skip され Issue 参照なしでも通過してしまっていた。
    issue_number: int | None = None
    checked_any = False
    for segment in segments:
        if _is_outside_session_repo(payload, segment.context):
            continue
        checked_any = True

        resolved_cwd = resolve_target_cwd(payload, segment.context)
        # PR #3026 codex レビュー指摘: resolve_target_cwd() の結果がプロセス CWD 自身と
        # 一致する場合はクロスリポジトリ指定なし（実質フォールバックのみ）とみなし、
        # None を渡して cache 優先経路（fresh/stale/rate-limit bypass）を維持する。
        repo_path = resolved_cwd if resolved_cwd != os.getcwd() else None

        match = _CLOSES_RE.search(segment.message)
        if not match:
            sys.stderr.write(
                "コミットメッセージに Issue 参照キーワードが含まれていません。NO TICKET NO WORK。\n"
                "Issue なしのコミットはブロックされます。対象の Issue 番号を指定してください。\n"
                "  作業完了時（Issue をクローズする）: closes #N / fixes #N / resolves #N\n"
                "  参照のみ（Issue をクローズしない）: refs #N\n"
                '例: git commit -m "feat: XXX を追加する closes #1234"\n'
                '例: git commit -m "docs(decisions): 決定を記録する refs #1234"\n'
            )
            sys.stderr.write(
                "詳細: 上流リポジトリ本体の docs/reference/ 配下・`hooks.md#require-issuepy`（consumer 未配布）\n"
            )
            return 2, {"blocked_by": "no_closes_ref"}

        issue_number = int(match.group(1))
        if not _issue_exists(issue_number, repo_path=repo_path):
            sys.stderr.write(
                f"Blocked: Issue #{issue_number} が見つかりません。"
                "有効な Issue 番号を指定してください（closes / fixes / resolves / refs #N）。\n"
            )
            sys.stderr.write(
                "詳細: 上流リポジトリ本体の docs/reference/ 配下・`hooks.md#require-issuepy`（consumer 未配布）\n"
            )
            return 2, {"blocked_by": "issue_not_found", "issue_number": issue_number}

    if not checked_any:
        return 0, {"skip_reason": "outside_session_repo"}

    return 0, {"issue_number": issue_number}


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("require-issue"):
        return 0
    # Issue #3717: escape hatch（環境変数）
    if os.environ.get(_ESCAPE_HATCH_ENV) == "1":
        sys.stderr.write(
            f"require-issue: {_ESCAPE_HATCH_ENV}=1 によりバイパスされました。\n"
        )
        return 0
    exit_code, _extra = _main_impl()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
