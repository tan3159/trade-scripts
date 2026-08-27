"""`tidd config` サブコマンド（Issue #2359 / #3569）.

hook の on/off を対話なし（非対話）CLI で管理する。
旧称 `tidd hooks-config`（Issue #2168）から完全破壊的改名。

機能:
- `tidd config init --all-on --repo|--machine` : 全 hook を true にした JSON を書き出す
- `tidd config init --safety-only --repo|--machine` : 安全系 2 hook のみ true にした JSON を書き出す
  既存ファイルがある場合は --force を要求する。
- `tidd config enable <hook> --repo|--machine` : 個別 hook を true に更新する
- `tidd config disable <hook> --repo|--machine` : 個別 hook を false に更新する
- `tidd config show` : 現在の設定 + 未設定 hook の default を tabular に表示する（実効値の出所も表示）
- `tidd config disable github-mcp` : ~/.claude/settings.json の disabledMcpjsonServers に "github" を追加（Issue #2360）
- `tidd config enable github-mcp` : disabledMcpjsonServers から "github" を除去（Issue #2360）
- `tidd config`（裸実行） : 対話ウィザード（作成・更新 or 削除 → マシン単位 or リポジトリ単位 → 各設定）（Issue #3569）

初回実行時に既存の hooks-config.json を config.json へ自動 mv する（migration・冪等）。

**Issue #3569: リポジトリ単位オーバーライド。** `init`/`enable`/`disable` は
`--repo`（リポジトリ root の `.tidd/config.json`）または `--machine`
（`~/.config/tidd_tools/config.json`）のどちらかを明示指定する必要がある。
両方省略時は exit 2 でエラー終了する（暗黙のデフォルトスコープは持たない）。
`.tidd/` は既にリポジトリ全体が `.gitignore`（#2311）で git 管理対象外のため、
`.tidd/config.json` もローカル限定（チーム共有なし）として扱う。
`github-mcp`（MAGIC_KEYS）と文字列 enum キー（`secrets-backend` 等）はマシン単位
固定のため `--repo`/`--machine` は不要（指定不可）。

stdlib のみ使用（依存追加禁止）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from tidd_tools.deprecated_config_registry import DEPRECATED_ENV_VARS
from tidd_tools.shared import app_config
from tidd_tools.shared.cli import add_common_flags

# ── 文字列 enum キー（configure.py と共通の概念）（Issue #2533） ────────────────
#: config init --all-on / config enable / config disable の対象から外すキーの集合。
#: bool 前提の hook レジストリには登録しない文字列値キー。
#: configure.py の STRING_ENUM_KEYS と同じキーを管理する。ここでは set だけで十分。
_STRING_ENUM_KEY_NAMES: frozenset[str] = frozenset({"secrets-backend"})

# ── 安全系 hook（デフォルト ON） ──────────────────────────────────────────────

#: hook_io.py の _SAFETY_HOOKS と同じセット。
#: Issue #2354: require-issue を撤去し 2 hook のみに縮小（default OFF・opt-in へ降格）。
SAFETY_HOOKS: frozenset[str] = frozenset(
    {
        "block-dangerous-git",
        "ban-claude-p",
    }
)

#: 「利用可否」型のキー（未設定時 default ON）。hook ではなく tidd_tools 側の
#: バックエンド可用性を config.json で管理するために追加された（Issue #2495）。
#: 実装側は :func:`hook_io.get_hook_config` を default=True で呼び出しており、
#: ``tidd config show`` / ``tidd config init --safety-only`` の default 表示・
#: 初期化結果もこれに合わせる。
#: さらに on-stop サブ機能（Issue #2955）は on-stop.py の ``_read_bool_hook_config``
#: で default=True として読まれているため、表示と実体の食い違いを防ぐため
#: ここにも登録する（Issue #3446）。
DEFAULT_TRUE_KEYS: frozenset[str] = frozenset(
    {
        "ai-review-agy",
        "ai-review-codex",
        "on-stop-branch-cleanup",
        "on-stop-brief",
        "on-stop-orphan-detect",
    }
)

# ── magic key（hook 以外の設定キー） ─────────────────────────────────────────

#: tidd config enable/disable で扱う magic key のセット。
#: hook-groups.yaml に登録されていないが、enable/disable で特殊処理される。
#: Issue #2360: "github-mcp" を追加（~/.claude/settings.json の disabledMcpjsonServers を操作）。
MAGIC_KEYS: frozenset[str] = frozenset({"github-mcp"})

# ── hook-groups.yaml 解析・config.json 操作（Issue #2947: shared/app_config.py へ集約） ──
#: 以下はすべて `tidd_tools.shared.app_config` への薄いラッパー。
#: configure.py / ai_review/backends.py / ai_review/yaru_auto_tick.py と共通実装を使う。


def _find_repo_root() -> Path | None:
    """リポジトリルートを検索して返す（見つからない場合は None）."""
    return app_config.find_repo_root()


def _load_hook_groups() -> list[dict[str, Any]]:
    """`.claude/rules/hook-groups.yaml` を読み込んでグループリストを返す."""
    return app_config.load_hook_groups()


def _get_all_hook_names() -> list[str]:
    """hook-groups.yaml に登録されているすべての hook 名を返す."""
    return app_config.get_all_hook_names()


def _get_config_path() -> Path:
    """OS ネイティブ config ディレクトリの config.json パスを返す."""
    return app_config.config_path()


def _migrate_if_needed() -> None:
    """hooks-config.json → config.json への migration（存在するときのみ・冪等）.

    Issue #2359: 初回 `tidd config` 実行時に旧ファイルを新パスへ自動 mv する。
    config.json がすでに存在する場合は mv しない（冪等）。
    """
    app_config.migrate_if_needed()


def _read_config() -> dict[str, Any]:
    """config.json を読み込んで dict として返す（存在しない場合は空 dict）."""
    return app_config.read_config()


def _write_config(config: dict[str, Any]) -> None:
    """config.json に dict を書き込む（親ディレクトリを自動作成）."""
    app_config.write_config(config)


# ── スコープ解決（Issue #3569） ───────────────────────────────────────────────

#: `--repo`/`--machine` 両方省略時に stderr へ出すエラーメッセージ（Gherkin と一致）。
_SCOPE_REQUIRED_MSG = "--repo または --machine を指定してください\n"


def _resolve_scope(args: argparse.Namespace) -> str | None:
    """``args`` の ``--repo``/``--machine`` からスコープを解決する（Issue #3569）.

    Returns:
        ``"repo"`` / ``"machine"``。両方省略時（暗黙のデフォルトを持たない）は ``None``。
    """
    is_repo = bool(getattr(args, "repo", False))
    is_machine = bool(getattr(args, "machine", False))
    if is_repo and not is_machine:
        return "repo"
    if is_machine and not is_repo:
        return "machine"
    return None


def _require_scope(args: argparse.Namespace) -> str | None:
    """スコープを解決する。省略時は stderr にメッセージを書いて ``None`` を返す（Issue #3569）.

    呼び出し側は ``None`` のとき exit code 2 を返すこと。
    """
    scope = _resolve_scope(args)
    if scope is None:
        sys.stderr.write(_SCOPE_REQUIRED_MSG)
    return scope


def _config_path_for_scope(scope: str) -> Path:
    """スコープに対応する config.json パスを返す（Issue #3569）."""
    if scope == "repo":
        repo_root = _find_repo_root()
        if repo_root is None:
            raise RuntimeError("リポジトリルートが見つかりません（.git が見つかりません）")
        path = app_config.repo_config_path(repo_root)
        assert path is not None  # repo_root が非 None なら必ず非 None
        return path
    return _get_config_path()


def _read_config_for_scope(scope: str) -> dict[str, Any]:
    """スコープに対応する config.json を読み込む（Issue #3569）."""
    if scope == "repo":
        return app_config.read_repo_config()
    return _read_config()


def _write_config_for_scope(scope: str, config: dict[str, Any]) -> Path:
    """スコープに対応する config.json へ書き込み、書き込み先パスを返す（Issue #3569）."""
    if scope == "repo":
        return app_config.write_repo_config(config)
    _write_config(config)
    return _get_config_path()


# ── ~/.claude/settings.json 操作（Issue #2360） ───────────────────────────────


def _get_claude_settings_path() -> Path:
    """~/.claude/settings.json のパスを返す.

    CLAUDE_SETTINGS_PATH 環境変数が設定されている場合はそのパスを使う（テスト用）。
    """
    env_path = os.environ.get("CLAUDE_SETTINGS_PATH") or ""
    if env_path:
        return Path(env_path)
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".claude" / "settings.json"


def _read_claude_settings(settings_path: Path) -> dict[str, Any]:
    """~/.claude/settings.json を読み込んで dict として返す（存在しない場合は空 dict）."""
    if not settings_path.is_file():
        return {}
    try:
        result: dict[str, Any] = json.loads(settings_path.read_text(encoding="utf-8"))
        return result
    except (json.JSONDecodeError, OSError):
        return {}


def _write_claude_settings_atomic(settings_path: Path, settings: dict[str, Any]) -> None:
    """~/.claude/settings.json を atomic write で書き込む（POSIX: tmpfile + os.replace）.

    Issue #2360, Q19: concurrent write は best-effort（ロックなし）。
    atomic write により書き込み途中のファイルが残らないことを保証する。
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
    # tmp ファイルは同一ディレクトリに作成（os.replace の POSIX atomic 性を保つため）
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=str(settings_path.parent),
        prefix=".settings_tmp_",
        suffix=".json",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path_str, str(settings_path))
    except Exception:
        # tmp ファイルを残さないようにクリーンアップ
        with contextlib.suppress(OSError):
            os.unlink(tmp_path_str)
        raise


def _cmd_toggle_github_mcp(enabled: bool) -> int:
    """github-mcp magic key の enable/disable.

    Issue #2360:
    - tidd config.json に "github-mcp": bool を記録する
    - ~/.claude/settings.json の disabledMcpjsonServers を atomic write + preserve マージで更新する
    """
    # (1) tidd config.json に記録
    tidd_config = _read_config()
    tidd_config["github-mcp"] = enabled
    _write_config(tidd_config)

    # (2) ~/.claude/settings.json を更新
    settings_path = _get_claude_settings_path()
    settings = _read_claude_settings(settings_path)

    disabled: list[str] = list(settings.get("disabledMcpjsonServers") or [])

    if enabled:
        # enable: "github" を除去（存在しない場合も冪等）
        disabled = [s for s in disabled if s != "github"]
    else:
        # disable: "github" を重複なしで追加
        if "github" not in disabled:
            disabled.append("github")

    settings["disabledMcpjsonServers"] = disabled
    _write_claude_settings_atomic(settings_path, settings)

    status = "enabled" if enabled else "disabled"
    print(f"github-mcp: {status}")
    print(f"  tidd config: {_get_config_path()}")
    print(f"  claude settings: {settings_path}")
    return 0


# ── サブコマンド実装 ──────────────────────────────────────────────────────────


def _cmd_init(all_on: bool, safety_only: bool, force: bool, scope_args: argparse.Namespace) -> int:
    """全 hook を true / 安全系のみ true にした JSON を書き出す.

    Issue #2533: 文字列 enum キー（secrets-backend 等）は bool で上書きしない。
    既存の文字列値を保持し、存在しない場合のみデフォルト値で初期化する。

    Issue #3569: `--repo`/`--machine` のどちらかを明示指定する（省略時 exit 2）。
    """
    scope = _require_scope(scope_args)
    if scope is None:
        return 2

    _migrate_if_needed()
    config_path = _config_path_for_scope(scope)

    if config_path.is_file() and not force:
        sys.stderr.write(f"既存の config.json があります。--force で上書きしてください: {config_path}\n")
        return 1

    # 既存設定を読み込む（--force 時でも文字列 enum キーを保持するため）
    existing_config = _read_config_for_scope(scope)

    hook_names = _get_all_hook_names()

    if all_on:
        config: dict[str, Any] = {name: True for name in hook_names}
    elif safety_only:
        # safety-only でも DEFAULT_TRUE_KEYS（backend availability 等）は True で書き出す
        # 実行時 default（get_hook_config default=True）と CLI 初期化結果を一致させる（#2495）
        config = {name: (name in SAFETY_HOOKS or name in DEFAULT_TRUE_KEYS) for name in hook_names}
    else:
        # どちらも指定されていない場合は usage エラー（argparse 側で弾くはずだが念のため）
        sys.stderr.write("--all-on または --safety-only を指定してください。\n")
        return 1

    # 文字列 enum キーは bool で上書きしない（Issue #2533）
    # 既存に文字列値があれば保持し、新規の場合はデフォルト値を設定する
    for key in _STRING_ENUM_KEY_NAMES:
        existing_val = existing_config.get(key)
        if isinstance(existing_val, str):
            # 既存の文字列値を保持
            config[key] = existing_val
        else:
            # bool が書き込まれていた / 未設定の場合: 生成 config から削除（bool は書かない）
            config.pop(key, None)

    _write_config_for_scope(scope, config)
    print(f"config.json を書き出しました: {config_path}")
    print(f"  {len(config)} hook を設定しました。")
    return 0


def _cmd_enable(hook_name: str, args: argparse.Namespace) -> int:
    """指定した hook を true に更新する."""
    return _cmd_toggle(hook_name, enabled=True, scope_args=args)


def _cmd_disable(hook_name: str, args: argparse.Namespace) -> int:
    """指定した hook を false に更新する."""
    return _cmd_toggle(hook_name, enabled=False, scope_args=args)


def _cmd_toggle(hook_name: str, *, enabled: bool, scope_args: argparse.Namespace) -> int:
    """指定した hook または magic key を enabled/disabled に更新する（Issue #3569: スコープ必須化）."""
    # 文字列 enum キー（secrets-backend 等）は config enable/disable で扱わない（Issue #2533）
    # スコープ不要（マシン単位固定）。
    if hook_name in _STRING_ENUM_KEY_NAMES:
        sys.stderr.write(
            f"'{hook_name}' は on/off ではなく文字列値で設定します。\n"
            f"  tidd configure --set {hook_name}=<値> を使ってください。\n"
        )
        return 1

    # magic key（github-mcp 等）は専用ハンドラへ委譲する（Issue #2360）。スコープ不要。
    if hook_name in MAGIC_KEYS:
        return _cmd_toggle_github_mcp(enabled) if hook_name == "github-mcp" else _fallback_toggle(hook_name, enabled)

    # Issue #3569: --repo/--machine のどちらかを明示指定する（省略時 exit 2）
    scope = _require_scope(scope_args)
    if scope is None:
        return 2

    valid_hooks = _get_all_hook_names()
    if valid_hooks and hook_name not in valid_hooks:
        sys.stderr.write(f"unknown hook: {hook_name}\n")
        sys.stderr.write(f"valid hooks: {', '.join(sorted(valid_hooks))}\n")
        return 1

    config = _read_config_for_scope(scope)
    config[hook_name] = enabled

    status = "enabled" if enabled else "disabled"
    config_path = _config_path_for_scope(scope)
    if bool(getattr(scope_args, "dry_run", False)):
        print(f"[dry-run] {hook_name} を {status} に変更する予定です。")
        print(f"[dry-run] config.json は変更しません: {config_path}")
        return 0

    _write_config_for_scope(scope, config)
    print(f"{hook_name}: {status}")
    print(f"設定を {config_path} に保存しました。")
    return 0


def _fallback_toggle(hook_name: str, enabled: bool) -> int:
    """MAGIC_KEYS に登録されているが専用ハンドラがない場合のフォールバック（将来拡張用）."""
    config = _read_config()
    config[hook_name] = enabled
    _write_config(config)
    status = "enabled" if enabled else "disabled"
    print(f"{hook_name}: {status}")
    return 0


def _cmd_show() -> int:
    """現在の設定 + 未設定 hook の default を tabular に表示する（Issue #3569: 実効値の出所も表示）."""
    _migrate_if_needed()
    machine_config = _read_config()
    machine_config_path = _get_config_path()
    repo_root = _find_repo_root()
    repo_config = app_config.read_repo_config(repo_root) if repo_root is not None else {}
    repo_config_path = app_config.repo_config_path(repo_root) if repo_root is not None else None
    all_hooks = _get_all_hook_names()

    print(f"マシン設定ファイル: {machine_config_path}")
    if not machine_config_path.is_file():
        print("  (ファイルが存在しません。すべての hook はデフォルト値を使用)")
    if repo_config_path is not None:
        print(f"リポジトリ設定ファイル: {repo_config_path}")
        if not repo_config_path.is_file():
            print("  (ファイルが存在しません)")
    print()

    if not all_hooks:
        print("  hook-groups.yaml が見つかりません。")
        merged = {**machine_config, **repo_config}
        if merged:
            print("  現在の設定:")
            for key, value in sorted(merged.items()):
                status = "on" if value else "off"
                print(f"    {key}: {status}")
        return 0

    # ヘッダ（デフォルトは末尾のまま維持する。既存 test の
    # `.endswith("on")`/`.endswith("-")` アサーションとの互換性のため）
    print(f"  {'hook名':<35} {'設定':<10} {'出所':<10} {'デフォルト'}")
    print(f"  {'-' * 35} {'-' * 10} {'-' * 10} {'-' * 10}")

    for name in all_hooks:
        if name in repo_config:
            value = repo_config[name]
            configured = "on" if value else "off"
            source = "repo"
            default_label = "-"
        elif name in machine_config:
            value = machine_config[name]
            configured = "on" if value else "off"
            source = "machine"
            default_label = "-"
        else:
            configured = "(未設定)"
            source = "default"
            default_on = name in SAFETY_HOOKS or name in DEFAULT_TRUE_KEYS
            default_label = "on" if default_on else "off"

        print(f"  {name:<35} {configured:<10} {source:<10} {default_label}")

    print()
    print("  (未設定の場合、安全系 2 hook と backend availability キーは on・それ以外は off がデフォルト)")
    print("  実効値の優先順位: リポジトリ > マシン > デフォルト。")

    # 廃止済み env var の残存を表示する（Issue #2531）
    _show_deprecated_env_vars(machine_config)

    return 0


def _show_deprecated_env_vars(config: dict[str, Any]) -> None:
    """廃止済み env var が環境に残っていれば実効値との比較を表示する（Issue #2531）.

    旧 env var が設定されており、かつ config.json の実効値と食い違う場合は
    「!」マークを付けて並べて表示する。一致している場合はマークなしで表示。
    """
    present_entries = [
        (env_var_name, entry) for env_var_name, entry in DEPRECATED_ENV_VARS.items() if env_var_name in os.environ
    ]

    if not present_entries:
        return

    print()
    print("  廃止済み env var（環境に残存しており無視されています）:")
    print(f"  {'env var':<35} {'env var の値':<15} {'実効値':<10}")
    print(f"  {'-' * 35} {'-' * 15} {'-' * 10}")

    for env_var_name, entry in present_entries:
        raw_env_value = os.environ.get(env_var_name, "")
        # "0" / "false" / "" → False, それ以外 → True
        env_as_bool = raw_env_value not in ("0", "false", "False", "")

        # config.json の実効値（キーなし → DEFAULT_TRUE_KEYS に含まれれば True）
        config_value = config.get(entry.replacement_key)
        effective_value = entry.replacement_key in DEFAULT_TRUE_KEYS if config_value is None else bool(config_value)

        env_label = "有効（ON）" if env_as_bool else "無効（OFF）"
        effective_label = "有効（ON）" if effective_value else "無効（OFF）"

        # 食い違い判定
        mismatch = env_as_bool != effective_value
        mismatch_mark = "! 食い違い" if mismatch else ""

        print(f"  {env_var_name:<35} {env_label:<15} {effective_label:<10} {mismatch_mark}")
        print(f"    → 移行先: {entry.replacement_key}（廃止: {entry.deprecated_issue}）")


# ── 対話ウィザード（`tidd config` 裸実行・Issue #3569） ────────────────────────


def _ask_choice(prompt: str, options: list[tuple[str, str]], *, default_index: int = 0) -> str:
    """選択肢を番号で提示し、選ばれた ``options`` の key を返す.

    空入力（Enter のみ）は ``default_index`` を選んだものとして扱う。
    """
    print(prompt)
    for i, (_key, label) in enumerate(options, start=1):
        marker = " (デフォルト)" if i - 1 == default_index else ""
        print(f"  {i}. {label}{marker}")
    while True:
        raw = input("番号を選んでください: ").strip()
        if not raw:
            return options[default_index][0]
        try:
            idx = int(raw) - 1
        except ValueError:
            print("数字を入力してください。")
            continue
        if 0 <= idx < len(options):
            return options[idx][0]
        print("範囲外の番号です。")


def _ask_yes_no_wizard(prompt: str, *, default: bool = True) -> bool:
    """y/n を尋ねる。空入力は ``default`` を返す."""
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input(f"{prompt} {suffix}: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("y または n を入力してください。")


def _cmd_wizard_scope_prompt() -> str:
    """スコープ（マシン単位/リポジトリ単位）を尋ねて ``"machine"``/``"repo"`` を返す."""
    return _ask_choice(
        "スコープを選んでください:",
        [
            ("machine", "マシン単位（~/.config/tidd_tools/config.json）"),
            ("repo", "リポジトリ単位（.tidd/config.json）"),
        ],
        default_index=0,
    )


def _cmd_wizard_delete(*, is_repo: bool) -> int:
    """スコープの設定ファイルを削除する（Issue #3569: リポジトリ単位はファイル単位削除）."""
    if is_repo:
        repo_root = _find_repo_root()
        if repo_root is None:
            sys.stderr.write("リポジトリルートが見つかりません（.git が見つかりません）\n")
            return 1
        config_path = app_config.repo_config_path(repo_root)
        assert config_path is not None
        deleted = app_config.delete_repo_config(repo_root)
    else:
        config_path = _get_config_path()
        deleted = config_path.is_file()
        if deleted:
            config_path.unlink()

    if deleted:
        print(f"削除しました: {config_path}")
    else:
        print(f"削除対象のファイルが見つかりませんでした: {config_path}")
    return 0


def _cmd_wizard_create_or_update(*, is_repo: bool) -> int:
    """hook グループごとに Y/n を尋ねて設定を作成・更新する（Issue #3569）."""
    scope = "repo" if is_repo else "machine"
    groups = _load_hook_groups()
    if not groups:
        sys.stderr.write("hook-groups.yaml が見つかりません。\n")
        return 1

    config = _read_config_for_scope(scope)
    for group in groups:
        label = group.get("label") or group.get("name")
        description = group.get("description")
        prompt = f"[{label}] {description}" if description else f"[{label}]"
        enable_group = _ask_yes_no_wizard(f"{prompt} を有効にしますか?", default=True)
        for hook_name in group.get("hooks", []):
            config[hook_name] = enable_group

    config_path = _write_config_for_scope(scope, config)
    print(f"設定を {config_path} に保存しました。")
    return 0


def _cmd_wizard() -> int:
    """`tidd config`（裸実行）: 対話ウィザード（Issue #3569）.

    「作成・更新 or 削除」→「マシン単位 or リポジトリ単位」→ 各処理の順に選択する。
    """
    action = _ask_choice(
        "操作を選んでください:",
        [("create_or_update", "作成・更新"), ("delete", "削除")],
        default_index=0,
    )
    scope = _cmd_wizard_scope_prompt()
    is_repo = scope == "repo"
    if action == "delete":
        return _cmd_wizard_delete(is_repo=is_repo)
    return _cmd_wizard_create_or_update(is_repo=is_repo)


# ── argparse 登録 ─────────────────────────────────────────────────────────────


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "config",
        help="hook の on/off を CLI で管理する（Issue #2359）",
        description=__doc__,
    )
    add_common_flags(parser)

    sub = parser.add_subparsers(dest="config_subcommand", required=False)

    # init サブコマンド
    init_parser = sub.add_parser(
        "init",
        help="config.json を初期化する",
    )
    mode_group = init_parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--all-on",
        action="store_true",
        help="全 hook を true にする",
    )
    mode_group.add_argument(
        "--safety-only",
        action="store_true",
        help="安全系 2 hook のみ true にする（他は false）",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="既存の config.json を上書きする",
    )
    # Issue #3569: スコープ必須。required は付けない（独自 stderr メッセージを
    # _require_scope から出すため、argparse 自体のエラーには任せない）。
    init_scope_group = init_parser.add_mutually_exclusive_group()
    init_scope_group.add_argument("--repo", action="store_true", help="リポジトリ root の .tidd/config.json へ書き込む")
    init_scope_group.add_argument("--machine", action="store_true", help="マシン単位の config.json へ書き込む")

    # enable サブコマンド
    enable_parser = sub.add_parser(
        "enable",
        help="指定した hook を有効にする",
    )
    enable_parser.add_argument("hook", help="hook 名（拡張子なし、例: require-issue）")
    enable_scope_group = enable_parser.add_mutually_exclusive_group()
    enable_scope_group.add_argument(
        "--repo", action="store_true", help="リポジトリ root の .tidd/config.json へ書き込む"
    )
    enable_scope_group.add_argument("--machine", action="store_true", help="マシン単位の config.json へ書き込む")

    # disable サブコマンド
    disable_parser = sub.add_parser(
        "disable",
        help="指定した hook を無効にする",
    )
    disable_parser.add_argument("hook", help="hook 名（拡張子なし、例: require-issue）")
    disable_scope_group = disable_parser.add_mutually_exclusive_group()
    disable_scope_group.add_argument(
        "--repo", action="store_true", help="リポジトリ root の .tidd/config.json へ書き込む"
    )
    disable_scope_group.add_argument("--machine", action="store_true", help="マシン単位の config.json へ書き込む")

    # show サブコマンド
    sub.add_parser(
        "show",
        help="現在の設定を表示する",
    )

    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    subcmd = getattr(args, "config_subcommand", None)
    if subcmd is None:
        # `tidd config`（裸実行）: 対話ウィザード（Issue #3569）
        return _cmd_wizard()
    if subcmd == "init":
        return _cmd_init(
            all_on=getattr(args, "all_on", False),
            safety_only=getattr(args, "safety_only", False),
            force=getattr(args, "force", False),
            scope_args=args,
        )
    if subcmd == "enable":
        return _cmd_enable(args.hook, args)
    if subcmd == "disable":
        return _cmd_disable(args.hook, args)
    if subcmd == "show":
        return _cmd_show()
    sys.stderr.write(f"未知のサブコマンド: {subcmd}\n")
    return 1


# ── エントリポイント ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    sys.exit(
        run_cli(
            argparse.Namespace(
                config_subcommand="show",
                verbose=0,
                dry_run=False,
                json_output=False,
            )
        )
    )
