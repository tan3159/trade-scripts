"""hook の stdin JSON 読み取りユーティリティ（stdlib のみ）.

Claude Code は PreToolUse / PostToolUse / Stop hook の stdin に JSON を渡す。
JSON 解析エラーや stdin 空のときは空 dict を返して hook 側で安全にスキップできるようにする。

Issue #1292: `read_hook_input()` に optional 引数 ``hook_name`` を追加。
指定時に ``_lib/schemas/{hook_name}.json`` で payload を検証し、
不一致なら stderr にエラーを出力して exit 2 で終了する。

Issue #1633: `get_hooks_config_path()` / `is_hook_enabled()` を追加。
~/.config/tidd_tools/config.json（OS ネイティブパス）で hook を個別 on/off できる。
stdlib のみ（platformdirs 不使用）。OS 判定 + 環境変数で同等パスを自前解決する。
旧 hooks-config.json は初回読み込み時に自動 mv される（Issue #2359）。

Issue #2166: `is_hook_enabled()` の default を False に反転（opt-in 設計）。
安全系 2 hook（block-dangerous-git / ban-claude-p）のみキー未設定時 True。
他の hook はキー未設定時 False（no-op）。consumer リポジトリが段階的に自動化を採用できるようにする。

Issue #2354: require-issue を安全系 hook から撤去し default OFF（opt-in）に降格。

Issue #2359: 設定ファイル名を hooks-config.json → config.json に改名。

Issue #2443: `resolve_target_cwd()` を追加。staged 検査系 hook の worktree 盲目対策として、
コマンド解析 → payload `cwd` → プロセス CWD の優先順で対象リポジトリの CWD を解決する。

Issue #2957: `read_stop_hook_input()` を追加。`read_hook_input()` は schema 不一致で
exit 2 する仕様のため Stop hook では使えず、Stop 系 hook が stdin 読み取りを
各自再実装していた。本関数で isatty 対応 + WARN-only validation の Stop 専用
バリアントを共通化する。

Issue #3201: `get_file_path()` に Codex apply_patch 対応を追加。Codex のファイル編集は
canonical な `apply_patch` として届き、`tool_input` は `file_path` ではなく patch 文字列
（`tool_input.command` / `tool_input.input`）になるため、patch から対象パスを抽出する。

Issue #3221: `get_new_content()` を追加。内容依存チェック系 hook が読む
`tool_input.content` / `new_string` は apply_patch では patch 文字列にしか含まれないため、
patch の追加・更新行（`+` 行）から新内容を抽出する（`*** Delete File:` は対象外）。

Issue #3569: `is_hook_enabled()` にリポジトリ単位オーバーライドを追加。
`find_repo_root_for_hooks()` / `get_repo_hooks_config_path()` / `_read_repo_hook_override()`
でプロセス CWD から `.git` を探索してリポジトリ root の `.tidd/config.json` を読み、
マシン設定（`get_hooks_config_path()`）より優先する（優先順位: リポジトリ > マシン >
default）。`.tidd/` はリポジトリ全体が既に `.gitignore` 対象（#2311）でローカル限定。
`get_hooks_config_path()` 自体は既存 `get_hook_config()` 等の呼び出し元との互換性を
保つため、返り値（マシン単位パス）は変更しない。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ── Issue #2443: 対象リポジトリ CWD 解決 ─────────────────────────────────────
#
# hook プロセスはセッション起動ディレクトリ（メイン checkout）の CWD で実行される
# （#2226 で実証済み）。一方 TiDD ワークフローのコミットは worktree 内で行われるため、
# プロセス CWD で git を実行する staged 検査系 hook は worktree の index / コミットに
# 盲目になる。git を実行する hook は本ヘルパーで対象リポジトリの CWD を解決すること。

# `cd <path> &&` prefix / `git -C <path>`（#2226 の require-issue.py と同型のパターン）
_CD_PREFIX_RE = re.compile(r"(?:^|&&|;)\s*cd\s+(.+?)\s*&&")
_GIT_DASH_C_RE = re.compile(r"git\s+-C\s+(\"[^\"]+\"|'[^']+'|\S+)")


def _strip_path_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def resolve_target_cwd(payload: dict[str, Any], command: str | None = None) -> str:
    """git 検査系 hook が対象リポジトリを検査するための CWD を解決する（Issue #2443）.

    優先順:

    1. コマンド内 ``cd <path> &&`` / ``git -C <path>``（#2226 と同型のコマンド解析。
       セッション CWD と異なるリポジトリを明示指定するケース）
    2. payload の ``cwd`` フィールド（Bash 永続 CWD が worktree にあり、
       コマンドにパス指定がないケース。コマンド解析では解決できない）
    3. プロセス CWD（従来挙動フォールバック）

    候補パスは ``os.path.expanduser`` でチルダ展開する（#2304 と同型の誤動作防止）。
    実在しないディレクトリは skip して次の優先順へフォールバックする。

    Args:
        payload: hook が受け取った JSON payload（``cwd`` フィールドを参照する）。
        command: Bash tool のコマンド文字列（省略時はコマンド解析を skip）。

    Returns:
        検査対象リポジトリのディレクトリパス。全候補が実在しなければ ``os.getcwd()``。
    """
    candidates: list[str] = []
    if command:
        match = _CD_PREFIX_RE.search(command)
        if match:
            candidates.append(match.group(1).strip())
        match = _GIT_DASH_C_RE.search(command)
        if match:
            candidates.append(match.group(1).strip())
    payload_cwd = payload.get("cwd")
    if isinstance(payload_cwd, str) and payload_cwd:
        candidates.append(payload_cwd)

    for candidate in candidates:
        expanded = os.path.expanduser(_strip_path_quotes(candidate))
        if os.path.isdir(expanded):
            return expanded
    return os.getcwd()


def read_hook_input(hook_name: str | None = None) -> dict[str, Any]:
    """stdin から hook 入力 JSON を読み取って dict として返す.

    Args:
        hook_name: 指定時に ``_lib/schemas/{hook_name}.json`` で schema 検証する
            （Issue #1292）。未指定なら検証を skip する（既存 hook との後方互換）。

    Returns:
        payload dict。schema 検証失敗時は sys.exit(2) で終了する。

    - stdin が空・非 JSON の場合は空 dict を返す（hook 側でスキップ判定する）
    - 例外を投げないことで hook の信頼性を担保する
    """
    try:
        data = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not data.strip():
        return {}
    try:
        result = json.loads(data)
    except json.JSONDecodeError:
        return {}
    if not isinstance(result, dict):
        return {}

    # Issue #1292: hook_name 指定時は schema 検証を実施
    if hook_name:
        # `_lib/` を sys.path に追加して validate_payload を確実に import できるようにする。
        # 既存 hook は `sys.path.insert(0, hooks_dir)` してから `from _lib.hook_io import ...`
        # で呼ぶため、_lib 直下は自動で通らない場合がある。
        from pathlib import Path as _Path

        _lib_dir = _Path(__file__).resolve().parent
        if str(_lib_dir) not in sys.path:
            sys.path.insert(0, str(_lib_dir))
        try:
            from validate_payload import (  # type: ignore[import-not-found]
                PayloadValidationError,
                validate_payload,
            )
        except ImportError:
            # validate_payload が存在しない場合は skip（前方互換）
            return result
        try:
            validate_payload(result, hook_name)
        except PayloadValidationError as exc:
            sys.stderr.write(f"{exc}\n")
            sys.exit(2)
    return result


def read_stop_hook_input() -> dict[str, Any]:
    """Stop hook 専用の stdin JSON 読み取り（Issue #2957）.

    `read_hook_input()` は schema 不一致で ``sys.exit(2)`` する仕様のため、常に
    exit 0（セッション終了をブロックしない）を維持すべき Stop hook では使えない。
    従来 Stop 系 hook（`on-stop.py`・`analyze-loop-on-stop.py`・
    `require-issue-next-completion.py`）は stdin 読み取りをそれぞれ個別に
    再実装しており、片方の修正が他方に反映されない状態になっていた。

    `read_hook_input()` との相違点:

    - 対話端末（``sys.stdin.isatty()``）では stdin を読まず空 dict を返す
      （旧 `on-stop.py::_drain_stdin` 等と同じ isatty ガード。テストが
      subprocess 経由で stdin を pipe する場合は isatty が False になるため
      影響しない）
    - ``_lib/schemas/Stop.json`` で validate するが、不一致時は stderr に
      WARN を書き込むのみで **exit しない**（Stop hook は fail-safe で
      継続する必要があるため）

    Returns:
        payload dict。stdin が空・非 JSON・非 dict の場合は空 dict。
        例外を投げない。
    """
    if sys.stdin.isatty():
        return {}
    try:
        data = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not data.strip():
        return {}
    try:
        result = json.loads(data)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"WARN: Stop hook stdin JSON decode failed: {exc}\n")
        return {}
    if not isinstance(result, dict):
        return {}

    # `_lib/` を sys.path に追加して validate_payload を確実に import できるようにする
    # （read_hook_input と同じ理由・Issue #1292 参照）。
    from pathlib import Path as _Path

    _lib_dir = _Path(__file__).resolve().parent
    if str(_lib_dir) not in sys.path:
        sys.path.insert(0, str(_lib_dir))
    try:
        from validate_payload import (  # type: ignore[import-not-found]
            PayloadValidationError,
            validate_payload,
        )
    except ImportError:
        # validate_payload が存在しない場合は skip（前方互換）
        return result
    try:
        validate_payload(result, "Stop")
    except PayloadValidationError as exc:
        # Stop hook は exit させず WARN のみで継続する（read_hook_input との相違点）
        sys.stderr.write(f"WARN: Stop schema mismatch: {exc}\n")
    return result


# ── Issue #1633: hook 機能別 on/off ──────────────────────────────────────────

#: キー未設定時に default True（有効）として扱う安全系 hook 名セット（Issue #2166）。
#: consumer リポジトリで copier copy 直後でも最低限の安全ガードが働くよう、
#: この 2 hook のみ default ON に維持する。他の hook は default OFF（opt-in）。
#: Issue #2354: require-issue を撤去（TiDD 採用は明示 opt-in にする）。
_SAFETY_HOOKS: frozenset[str] = frozenset(
    {
        "block-dangerous-git",
        "ban-claude-p",
    }
)

#: 破壊的操作防止（_SAFETY_HOOKS）とは別に、キー未設定時 default True（有効）として
#: 扱う非安全系 hook の例外セット（Issue #3826）。
#:
#: `stamp-issue-next-session` は `require-issue-next-completion.py` の別セッション
#: 誤ブロック対策（#3779）の前提となる session_id を記録する hook で、config.json に
#: キーが存在しないと一度も動作しないため #3779 の機能が実質無効化されていた
#: （実測: 2026-08-14）。この hook のみ既存の _SAFETY_HOOKS（破壊的操作の安全ガード）
#: とは意味合いが異なるため、無効化時の「安全系 hook」WARN 対象には含めない
#: （`is_safety` とは別に判定する）。
_DEFAULT_ON_HOOKS: frozenset[str] = frozenset(
    {
        "stamp-issue-next-session",
    }
)


def get_hooks_config_path() -> str:
    """OS ネイティブ config ディレクトリの config.json パスを返す（stdlib のみ）.

    Issue #2359: 設定ファイル名を hooks-config.json → config.json に改名。
    ADR 013 と同じ方針で OS を判定し、platformdirs 相当のパスを自前計算する。

    - Windows (sys.platform == "win32"):
        ``%APPDATA%\\tidd_tools\\config.json``
        (APPDATA 未設定なら HOME\\.config\\tidd_tools\\config.json にフォールバック)
    - その他 (Linux / macOS):
        ``${XDG_CONFIG_HOME}/tidd_tools/config.json``
        (XDG_CONFIG_HOME 未設定なら ${HOME}/.config/tidd_tools/config.json)

    旧 hooks-config.json が存在し config.json が存在しない場合は自動 mv する（migration）。

    Returns:
        設定ファイルのフルパス文字列（ファイルが存在するかは問わない）。
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or ""
        if appdata:
            config_path = os.path.join(appdata, "tidd_tools", "config.json")
        else:
            # APPDATA 未設定フォールバック
            home = os.path.expanduser("~")
            config_path = os.path.join(home, ".config", "tidd_tools", "config.json")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or ""
        if xdg:
            config_path = os.path.join(xdg, "tidd_tools", "config.json")
        else:
            home = os.environ.get("HOME") or os.path.expanduser("~")
            config_path = os.path.join(home, ".config", "tidd_tools", "config.json")

    # migration: hooks-config.json → config.json（存在時のみ・冪等）
    legacy_path = os.path.join(os.path.dirname(config_path), "hooks-config.json")
    if os.path.isfile(legacy_path) and not os.path.isfile(config_path):
        try:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            os.rename(legacy_path, config_path)
            sys.stderr.write(f"migration: {legacy_path} → {config_path}\n")
        except OSError as e:
            sys.stderr.write(
                f"WARN: hooks-config.json の migration に失敗しました: {e}\n"
            )

    return config_path


#: 文字列値を許可する hook 名 → 許可値セットのレジストリ（Issue #1994）
_STRING_VALUE_HOOKS: dict[str, frozenset[str]] = {
    "yaru-auto-tick": frozenset({"dry-run", "enabled"}),
    "secrets-backend": frozenset({"env", "bw"}),  # Issue #2496
}


def get_hook_config(
    hook_name: str, default: bool | str | None = True
) -> bool | str | None:
    """config.json から hook の設定値を返す（Issue #1994・文字列値対応）.

    ``is_hook_enabled()`` の拡張版。bool だけでなく ``_STRING_VALUE_HOOKS`` に
    登録された許可文字列値（例: ``"yaru-auto-tick": "dry-run"``）も返せる。

    - 設定ファイルなし・キーなし → default
    - 不正 JSON → stderr に WARN を出して default
    - bool 値 → そのまま返す
    - 許可文字列値 → そのまま返す
    - 未知の文字列値・非対応型 → stderr に WARN を出して default（安全側）

    **Issue #3569:** ``is_hook_enabled()`` 同様、プロセス CWD から探索したリポジトリ
    root の ``.tidd/config.json`` にキー（bool または許可文字列値）があれば、
    マシン設定より優先して使う（優先順位: リポジトリ > マシン > default）。
    """
    repo_found, repo_value = _read_repo_hook_config_value(hook_name)
    if repo_found:
        return repo_value

    config_path = get_hooks_config_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return default

    try:
        config = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write(
            "WARN: config.json のパースに失敗しました（不正な JSON）。default を使用します。\n"
        )
        return default

    if not isinstance(config, dict) or hook_name not in config:
        return default

    value = config[hook_name]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        allowed = _STRING_VALUE_HOOKS.get(hook_name)
        if allowed is not None and value in allowed:
            return value
        sys.stderr.write(
            f"WARN: config.json の '{hook_name}' に未知の値 '{value}' が設定されています。default を使用します。\n"
        )
        return default
    sys.stderr.write(
        f"WARN: config.json の '{hook_name}' の値型が不正です（{type(value).__name__}）。default を使用します。\n"
    )
    return default


def find_repo_root_for_hooks(start: str | None = None) -> Path | None:
    """``.git`` を親方向に探索してリポジトリルートを返す（stdlib のみ・Issue #3569）.

    hook はプロセス CWD で実行される（#2226 で実証済み）。``start`` 省略時はプロセス
    CWD から探索する。``.git`` はディレクトリ・ファイル（worktree）のどちらでもよい。

    Args:
        start: 探索の起点（省略時は ``os.getcwd()``）。

    Returns:
        見つかった場合はリポジトリルート、見つからない場合は ``None``
        （呼び出し側はマシン設定のみを使う）。
    """
    current = Path(start or os.getcwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def get_repo_hooks_config_path() -> Path | None:
    """リポジトリ root の ``.tidd/config.json`` パスを返す（Issue #3569）.

    Returns:
        リポジトリルートが見つからない場合は ``None``。
    """
    repo_root = find_repo_root_for_hooks()
    if repo_root is None:
        return None
    return repo_root / ".tidd" / "config.json"


def _read_repo_config_dict() -> dict[str, Any] | None:
    """リポジトリ ``.tidd/config.json`` をパースして dict を返す（Issue #3569）.

    ``_read_repo_hook_override()``（bool 専用・``is_hook_enabled()`` 用）と
    ``_read_repo_hook_config_value()``（文字列値対応・``get_hook_config()`` 用）の
    共通下請け。

    Returns:
        パース済み dict。リポジトリルート未検出・ファイル不在・読み込み不可の
        場合は ``None``（呼び出し側はマシン設定へフォールバックする）。不正 JSON の
        場合は stderr に ``WARN`` とファイルパスを出力して ``None`` を返す。
    """
    cfg_path = get_repo_hooks_config_path()
    if cfg_path is None or not cfg_path.is_file():
        return None
    try:
        with open(cfg_path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        config = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write(
            f"WARN: {cfg_path} のパースに失敗しました（不正な JSON）。マシン設定へフォールバックします。\n"
        )
        return None
    if not isinstance(config, dict):
        return None
    return config


def _read_repo_hook_override(hook_name: str) -> tuple[bool, bool]:
    """リポジトリ ``.tidd/config.json`` から ``hook_name`` の設定を読む（Issue #3569）.

    Returns:
        ``(found, value)`` のタプル。``found=False`` はリポジトリ側にオーバーライド
        がなくマシン設定へフォールバックすべきことを示す（ファイル不在・キー不在・
        非 bool 値のいずれも ``found=False``）。不正 JSON の場合は stderr に
        ``WARN`` とファイルパスを出力して ``found=False`` を返す。
    """
    config = _read_repo_config_dict()
    if config is None or hook_name not in config:
        return False, False
    value = config[hook_name]
    if not isinstance(value, bool):
        return False, False
    return True, value


def _read_repo_hook_config_value(hook_name: str) -> tuple[bool, bool | str | None]:
    """リポジトリ ``.tidd/config.json`` から hook 設定値を読む（``get_hook_config()`` の
    repo オーバーライド版・Issue #3569）.

    ``_read_repo_hook_override()`` と異なり ``_STRING_VALUE_HOOKS`` に登録された
    許可文字列値（例: ``"yaru-auto-tick": "dry-run"``）も返す。

    Returns:
        ``(found, value)`` のタプル。``found=False`` はマシン設定へフォールバック
        すべきことを示す（ファイル不在・キー不在・非対応型・許可外の文字列値の
        いずれも ``found=False``）。
    """
    config = _read_repo_config_dict()
    if config is None or hook_name not in config:
        return False, None
    value = config[hook_name]
    if isinstance(value, bool):
        return True, value
    if isinstance(value, str):
        allowed = _STRING_VALUE_HOOKS.get(hook_name)
        if allowed is not None and value in allowed:
            return True, value
    return False, None


def is_hook_enabled(hook_name: str) -> bool:
    """config.json を読んで hook が有効かどうかを返す（Issue #1633 / #2166 / #3569）.

    **Issue #2166 変更: opt-in 設計に反転。Issue #2354: 安全系 hook を 3→2 に縮小。**
    - 安全系 2 hook（_SAFETY_HOOKS）: 設定ファイルなし・キーなし → True（default ON）
    - **Issue #3826:** `_DEFAULT_ON_HOOKS`（`stamp-issue-next-session` 等）:
      設定ファイルなし・キーなし → True（default ON）。安全系とは意味合いが異なるため
      無効化時の「安全系 hook」WARN 対象には含めない。
    - それ以外の非安全系 hook: 設定ファイルなし・キーなし → False（default OFF、no-op）

    不正 JSON の場合は stderr に WARN を出し、_SAFETY_HOOKS・_DEFAULT_ON_HOOKS は True、
    それ以外は False を返す。安全系 hook が明示的に無効化されている場合は追加で WARN を出す。

    **Issue #3569:** プロセス CWD から探索したリポジトリ root の ``.tidd/config.json``
    にキーがあれば、マシン設定より優先して使う（優先順位: リポジトリ > マシン >
    default）。リポジトリ設定にキーがない・ファイルがない・JSON が不正な場合は
    マシン設定（従来の挙動）へフォールバックする。

    ``TIDD_HOOKS_ALL_ENABLED=1`` 環境変数が設定されている場合はすべての hook を有効とする
    （テスト・CI 環境で全 hook の動作を検証するために使用する）。

    Args:
        hook_name: hook の名前（拡張子なし、例: "require-issue"）。

    Returns:
        True なら hook を通常通り実行する。False なら hook を no-op で終了する。
    """
    # テスト・CI 環境: 全 hook を強制有効化（TIDD_HOOKS_ALL_ENABLED=1）
    if os.environ.get("TIDD_HOOKS_ALL_ENABLED") == "1":
        return True

    # Issue #3569: リポジトリ単位オーバーライドをマシン設定より優先する
    repo_found, repo_value = _read_repo_hook_override(hook_name)
    if repo_found:
        if not repo_value:
            sys.stderr.write(
                f"hook '{hook_name}' is disabled (repo .tidd/config.json).\n"
            )
        return repo_value

    is_safety = hook_name in _SAFETY_HOOKS
    # Issue #3826: 破壊的操作の安全ガード（_SAFETY_HOOKS）とは別に default ON にする例外
    is_default_on = is_safety or hook_name in _DEFAULT_ON_HOOKS
    config_path = get_hooks_config_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = f.read()
    except (OSError, FileNotFoundError):
        # 設定ファイルが存在しない → default ON 対象は True、それ以外は False
        return is_default_on

    try:
        config = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write(
            "WARN: config.json のパースに失敗しました（不正な JSON）。hook を有効として扱います。\n"
        )
        return is_default_on

    if not isinstance(config, dict):
        return is_default_on

    value = config.get(hook_name)
    if value is None:
        # キーが存在しない → default ON 対象は True、それ以外は False
        return is_default_on

    enabled = bool(value)
    if not enabled:
        sys.stderr.write(f"hook '{hook_name}' is disabled (config.json).\n")
        if is_safety:
            sys.stderr.write(
                f"WARN: 安全系 hook '{hook_name}' が無効化されています。意図的な設定であることを確認してください。\n"
            )
    return enabled


# ── Issue #1606: schema guard ─────────────────────────────────────────────────

#: 現行の Claude Code tool_response 実 schema キー一覧。
#: 未知キーのみで既知キーから値が取れない場合に WARN を出すための基準セット。
_KNOWN_TOOL_RESPONSE_KEYS: set[str] = {
    "stdout",
    "stderr",
    "interrupted",
    "isImage",
    "noOutputExpected",
    "output",  # 旧 schema（後方互換）
}


def get_tool_name(payload: dict[str, Any]) -> str:
    return str(payload.get("tool_name", ""))


def is_file_edit_tool(tool_name: str) -> bool:
    """ファイル編集系ツールかどうかを返す（Issue #3201）.

    Claude Code は ``Edit`` / ``Write``、Codex は canonical な ``apply_patch`` が
    ファイル編集ツールとして届く。Edit|Write 系 hook のゲート判定は本ヘルパーに
    集約し、ツール名の追加時はここ 1 箇所の修正で済むようにする。
    """
    return tool_name in {"Edit", "Write", "apply_patch"}


def get_command(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return str(tool_input.get("command") or "")


def get_file_path(payload: dict[str, Any]) -> str:
    """PreToolUse payload から対象ファイルパスを返す（Issue #3201 で apply_patch 対応）.

    - Claude Code の Edit/Write: ``tool_input.file_path`` / ``tool_input.path``
    - Codex の apply_patch: ``tool_input.command``（または ``tool_input.input``）の
      patch 文字列から ``*** Add/Update/Delete File:`` / ``*** Move to:`` ヘッダで
      指定された対象パスを抽出する。複数ファイルを含む patch では最初のパスを返す。

    パスを取得できない場合は空文字列を返す（hook 側の skip 条件）。例外は投げない。
    """
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    direct = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if direct:
        return direct
    patch_text = _extract_apply_patch_text(tool_input)
    if not patch_text:
        return ""
    return _extract_first_patch_path(patch_text)


#: apply_patch の対象ファイルヘッダ（Issue #3201）。
#: `*** Add File:` / `*** Update File:` / `*** Delete File:` / `*** Move to:`
_PATCH_FILE_HEADER_RE = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$", re.MULTILINE
)
_PATCH_MOVE_TO_RE = re.compile(r"^\*\*\* Move to:\s*(.+?)\s*$", re.MULTILINE)


def _extract_apply_patch_text(tool_input: dict[str, Any]) -> str:
    """tool_input から apply_patch の patch 文字列を抽出する（Issue #3201）.

    Codex は patch を ``tool_input.command`` で渡す（環境によっては
    ``tool_input.input``）。フィールド名の揺れに備えて既知キーを順に試し、
    どれも空なら patch マーカー（``*** ... File:``）を含む任意の文字列値を
    フォールバックとして使う。patch 文字列が見つからなければ空文字列。
    """
    for key in ("command", "input"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for value in tool_input.values():
        if isinstance(value, str) and "*** " in value and "File:" in value:
            return value
    return ""


def _extract_first_patch_path(patch_text: str) -> str:
    """apply_patch の patch 文字列から最初の対象ファイルパスを抽出する（Issue #3201）."""
    for pattern in (_PATCH_FILE_HEADER_RE, _PATCH_MOVE_TO_RE):
        match = pattern.search(patch_text)
        if match:
            return _strip_path_quotes(match.group(1).strip())
    return ""


def get_new_content(payload: dict[str, Any]) -> str | None:
    """PreToolUse payload から編集で追加・更新される新内容を返す（Issue #3221）.

    - Claude Code の Write: ``tool_input.content``（全文）をそのまま返す
    - Claude Code の Edit: ``tool_input.new_string`` / ``new_str`` /
      ``replacement`` をそのまま返す（後方互換）
    - Codex の apply_patch: ``tool_input.command``（または ``tool_input.input``）の
      patch 文字列から追加・更新行（``+`` 行）を抽出して返す。
      ``*** Delete File:`` は「新内容」ではないため対象外とし、削除のみの patch は
      空文字列を返す（削除内容を新内容として検査しない）。

    新内容を取得できない場合は ``None`` を返す（hook 側の disk fallback / skip 条件）。
    不正な patch 文字列でも例外は送出せず空文字列を返す。
    """
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None

    for key in ("content", "new_string", "new_str", "replacement"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value

    patch_text = _extract_apply_patch_text(tool_input)
    if not patch_text:
        return None
    return _extract_added_lines_from_patch(patch_text)


def _extract_added_lines_from_patch(patch_text: str) -> str:
    """apply_patch の patch 文字列から追加・更新行（``+`` 行）を抽出する（Issue #3221）.

    ``get_file_path()`` と同じく最初の対象ファイルセクションを対象とする。
    ``*** Delete File:`` セクションは対象外（削除内容を新内容として検査しない）。
    patch 形式でない文字列は空文字列を返す。
    """
    header_match = _PATCH_FILE_HEADER_RE.search(patch_text)
    if header_match is None:
        return ""
    header = header_match.group(0)
    if header.startswith("*** Delete File:"):
        return ""

    section_end = _PATCH_FILE_HEADER_RE.search(patch_text, header_match.end())
    section = patch_text[
        header_match.end() : section_end.start() if section_end else len(patch_text)
    ]
    added_lines: list[str] = []
    for line in section.splitlines():
        # `+++`（git diff ヘッダ相当）はコンテンツ行ではないため除外
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added_lines.append(line[1:])
    return "\n".join(added_lines)


def get_tool_output(payload: dict[str, Any]) -> str:
    """PostToolUse payload の ``tool_response`` から stdout 相当を取り出す (Issue #1596).

    Claude Code の実 Bash tool schema は ``tool_response`` に
    ``{"stdout", "stderr", "interrupted", "isImage", "noOutputExpected"}`` を返す。
    旧実装は ``output`` キーのみを読んでいたため、実 payload では常に空文字列となり、
    ``label-pr.py`` などの PostToolUse hook が 100% skip する silent skip 経路が
    できていた（2026-07-03 以降 merged 100 PR で size ラベル付与率 0/100 = 0%）。

    優先順位:
      1. ``tool_response.stdout``（実 Claude Code Bash tool schema）
      2. ``tool_response.output``（旧テスト・後方互換）

    どちらも欠落 or 空文字列なら空文字列を返す。呼び出し側は空文字列を skip 条件として
    扱ってよい。

    **schema guard（Issue #1606）:**
    ``tool_response.keys() - _KNOWN_TOOL_RESPONSE_KEYS`` が非空かつ既知キーから値を
    取れない場合、stderr に ``WARN: unknown tool_response schema: keys=[...]`` を出力する。
    ``SCHEMA_GUARD_STRICT=1`` 環境変数が設定されている場合は exit 2 でブロックする。
    """
    tool_response = payload.get("tool_response") or {}
    if not isinstance(tool_response, dict):
        return ""
    # 実 schema (stdout) を優先し、旧 schema (output) は fallback として残す。
    stdout = tool_response.get("stdout")
    if isinstance(stdout, str) and stdout:
        return stdout
    legacy = tool_response.get("output")
    if isinstance(legacy, str) and legacy:
        return legacy

    # 値が取れなかった場合のみ schema guard を発動する（Issue #1606）。
    unknown_keys = set(tool_response.keys()) - _KNOWN_TOOL_RESPONSE_KEYS
    if unknown_keys:
        sorted_keys = sorted(unknown_keys)
        warn_msg = f"WARN: unknown tool_response schema: keys={sorted_keys}\n"
        sys.stderr.write(warn_msg)
        if os.environ.get("SCHEMA_GUARD_STRICT") == "1":
            sys.exit(2)

    return str(tool_response.get("output") or "")


def is_bash_success(payload: dict[str, Any]) -> bool:
    """PostToolUse[Bash] の payload が成功扱いか判定する（Issue #3552）.

    PostToolUse[Bash] の tool_response schema（hooks.md §label-pr.py 参照）には
    exit_code キーが存在しないため（既知キー: stdout / stderr / interrupted /
    isImage / noOutputExpected）、`interrupted` のみを失敗判定に使う。
    旧 `sweep-merged-branches.py::_bash_success` と同じ基準を本関数へ集約し、
    `sweep-merged-branches.py` と `record-timing-boundaries.py` の両方から呼ぶ。

    tool_response が非 dict（欠落・list / str 等）の場合は成功扱い（True）を返す
    （PreToolUse 等で tool_response が無い呼び出し形態をブロックしない）。
    """
    tool_response = payload.get("tool_response") or {}
    return not (
        isinstance(tool_response, dict) and tool_response.get("interrupted") is True
    )


# ── Issue #3340: 統一日誌（timing-events/timing.db）の hook 用 stdlib アクセス ──
#
# `tidd_tools.timing_log`（SQLite・timing-events/timing.db）を #1 系統の
# `~/.cache/ai-review-timing/*.jsonl` の後継として、hook からも stdlib のみで
# 読み書きする。パスは `tidd_tools.shared.paths.cache_dir()`（platformdirs）と
# 同一になるよう OS 別に解決する（hook_io.get_hooks_config_path と同方針）。

_TIMING_DB_SUBDIR = "timing-events"
_TIMING_DB_FILENAME = "timing.db"

# `tidd_tools.timing_log.get_current_repo()` と同型の正規表現（Issue #3588）。
# 二重実装の一致は `test_timing_log_hook_io_repo_contract.py` で機械検証する。
_REMOTE_OWNER_REPO_RE = re.compile(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$")


def get_current_repo(cwd: str | None = None) -> str | None:
    """`git remote get-url origin` から現在のリポジトリ（`owner/repo`）を解決する（Issue #3588）.

    ``tidd_tools.timing_log.get_current_repo()``（tidd_tools 側）と同型の実装
    （stdlib のみ）。``.claude/hooks/label-pr.py`` の ``_session_repo()``
    （Issue #2865）とも同型。取得・解析に失敗した場合は None を返し、呼び出し元は
    repo=NULL で記録する（``append_timing_event()`` の fail-open 方針と同じ）。

    Args:
        cwd: git コマンドを実行するディレクトリ。None ならプロセスの現在の cwd を使う。

    Returns:
        ``"owner/repo"`` 文字列。取得・解析に失敗した場合は None。
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            cwd=cwd,
        )
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        OSError,
        subprocess.CalledProcessError,
    ):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    match = _REMOTE_OWNER_REPO_RE.search(url)
    if not match:
        return None
    return match.group(1)


def get_tidd_cache_dir() -> Path:
    """``tidd_tools.shared.paths.cache_dir()`` と同パス（tidd キャッシュ基準）を返す.

    ``tidd_tools.shared.paths.cache_dir()``（platformdirs）と同一になるよう OS 別に
    自前解決する（``get_timing_db_path`` と同方針・stdlib のみ）。hook から
    ai-review の state_dir 配下（``ai-reviewer/pr-<N>/``）を参照するために使う（#3629）。

    Issue #3683: app 名を中立な ``tidd`` へ変更した（旧 app 名は上流固有）。
    旧キャッシュの移行は ``tidd_tools.shared.paths.cache_dir()``（tidd CLI 側）が
    行う。hook は stdlib のみのため、ここでは新パス解決のみを担う。
    """
    if sys.platform == "win32":
        appdata = os.environ.get("LOCALAPPDATA") or ""
        if appdata:
            return Path(appdata) / "tidd"
        return Path.home() / "AppData" / "Local" / "tidd"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "tidd"
    xdg = os.environ.get("XDG_CACHE_HOME") or ""
    if xdg:
        return Path(xdg) / "tidd"
    return Path.home() / ".cache" / "tidd"


def get_ai_reviewer_state_dir(pr_num: str) -> Path:
    """ai-review の state_dir（``tidd/ai-reviewer/pr-<N>``）を返す（#3629）.

    ``tidd_tools.ai_review.state_dir.resolve_state_dir()`` の STATE_DIR 未設定時の
    デフォルトと同一パス。hook（block-unauthorized-fallback-review.py）から
    ``backend-unavailable`` 証跡フラグの有無を確認するために使う。
    """
    return get_tidd_cache_dir() / "ai-reviewer" / f"pr-{pr_num}"


def get_timing_db_path() -> Path:
    """統一日誌 SQLite DB（timing-events/timing.db）のパスを返す（Issue #3340）."""
    return get_tidd_cache_dir() / _TIMING_DB_SUBDIR / _TIMING_DB_FILENAME


def has_timing_event(issue_key: str, step: str) -> bool:
    """統一日誌に ``issue_key`` の ``step`` イベントが記録済みか確認する（Issue #3340）.

    DB が存在しない・読み込み失敗時は False（安全側）。例外は投げない。

    **Issue #3588 レビュー指摘（PR #3599, HIGH）:** 呼び出し元自身のリポジトリ
    （``get_current_repo()``）に絞り込み、別リポジトリの同一 ``issue_key``/``step``
    行を既存扱いしない。``timing_log.read_events()``（tidd_tools 側）と同じ
    ``(repo = ? OR repo IS NULL)`` 複合フィルタ（legacy 行は repo 不問で一致）と
    fail-open（呼び出し元自身の repo が解決できない場合はフィルタなし）を踏襲する。
    ``events`` テーブルに ``repo`` カラム自体が存在しない場合（``append_timing_event()``
    の migration が未実行の legacy DB）も、読み取り専用の本関数では ALTER TABLE せず
    フィルタなしで問い合わせる（fail-open）。
    """
    db = get_timing_db_path()
    if not db.is_file():
        return False
    try:
        import sqlite3

        con = sqlite3.connect(str(db), timeout=3.0)
        try:
            columns = {
                row[1] for row in con.execute("PRAGMA table_info(events)").fetchall()
            }
            current_repo = get_current_repo() if "repo" in columns else None
            if current_repo is None:
                row = con.execute(
                    "SELECT 1 FROM events WHERE issue_key = ? AND step = ? LIMIT 1",
                    (issue_key, step),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT 1 FROM events WHERE issue_key = ? AND step = ?"
                    " AND (repo = ? OR repo IS NULL) LIMIT 1",
                    (issue_key, step, current_repo),
                ).fetchone()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return False
    return row is not None


def append_timing_event(
    issue_key: str,
    step: str,
    source: str,
    *,
    meta: dict[str, Any] | None = None,
) -> None:
    """統一日誌へ point イベントを追記する（Issue #3340・stdlib のみ）.

    ``tidd_tools.timing_log.record_event()`` と同様に直近 attempt_id を引き継ぐ
    （未記録時は 1）。書き込み失敗は握りつぶす（hook フローを妨げない・fail-open）。

    ``meta`` は JSON 直列化して events.meta カラムへ保存する。step4-pr-created は
    ``meta.created_at``（PR の実 createdAt）・``meta.created_at_source``（"github" /
    "fallback"）を格納する（Issue #3552）。

    **Issue #3687:** 接続クローズ前に ``PRAGMA wal_checkpoint(TRUNCATE)`` を実行し、
    WAL の書き込みを DB 本体ファイルへ追い出してから閉じる。DB が WAL モード
    （``tidd_tools.timing_log._connect()`` が先に触れて設定済み）のとき、他の接続が
    WAL を保持したまま本関数が書き込むと、コミット済みの行が WAL にのみ残り DB 本体
    ファイルへ反映されない。WSL2 等で ``-shm`` が破棄されると WAL 経由でも読めなくなり
    記録が消失するため、``tidd_tools.timing_log._connect()``（Issue #3685）と同じ
    checkpoint を行う。checkpoint 失敗は記録自体を失敗させない（fail-open）。
    """
    import sqlite3

    now = (
        __import__("datetime")
        .datetime.now(__import__("datetime").UTC)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    db = get_timing_db_path()
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db), timeout=5.0)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            con.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " issue_key TEXT NOT NULL,"
                " attempt_id INTEGER NOT NULL,"
                " step TEXT NOT NULL,"
                " kind TEXT NOT NULL,"
                " source TEXT NOT NULL,"
                " timestamp TEXT NOT NULL,"
                " meta TEXT NOT NULL DEFAULT '{}',"
                " repo TEXT)"
            )
            # 既存 DB（repo カラムなしで作成済み）へ冪等に追加する（Issue #3588）。
            columns = {
                row[1] for row in con.execute("PRAGMA table_info(events)").fetchall()
            }
            if "repo" not in columns:
                con.execute("ALTER TABLE events ADD COLUMN repo TEXT")
            row = con.execute(
                "SELECT attempt_id FROM events WHERE issue_key = ? ORDER BY id DESC LIMIT 1",
                (issue_key,),
            ).fetchone()
            attempt_id = (row[0] if row and isinstance(row[0], int) else 0) or 1
            repo = get_current_repo()
            con.execute(
                "INSERT INTO events (issue_key, attempt_id, step, kind, source, timestamp, meta, repo)"
                " VALUES (?, ?, ?, 'point', ?, ?, ?, ?)",
                (issue_key, attempt_id, step, source, now, meta_json, repo),
            )
            con.commit()
            # Issue #3687: WAL の書き込みを DB 本体へ checkpoint してから閉じる。
            # checkpoint 失敗は記録自体を失敗させない（fail-open・#3685 と同じ方針）。
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return
