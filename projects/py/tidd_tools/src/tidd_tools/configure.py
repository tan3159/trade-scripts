"""`tidd configure` サブコマンド（Issue #1634）.

対話ウィザードで hook の on/off と秘匿情報管理方式（SECRETS_BACKEND）を設定する。

機能:
- 対話モード（引数なし）: グループ単位の質問で 4〜6 個の質問で設定を完了する
- `--set name=on/off`: 非対話モード（CI・スクリプト連携用）
- `--show`: 現在の ~/.config/tidd_tools/config.json の内容を表示する

stdlib のみ使用（questionary 等の依存追加は禁止）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from tidd_tools.config import DEFAULT_TRUE_KEYS
from tidd_tools.shared import app_config
from tidd_tools.shared.cli import add_common_flags

# ── 文字列 enum キーのレジストリ（Issue #2533） ────────────────────────────────
#: 値域が bool（on/off）ではなく固定の文字列集合であるキーの定義。
#: key → (allowed_values, default_value)
#: これに登録されたキーは `_cmd_set` で文字列値として扱い、
#: hook-groups.yaml の bool 前提レジストリには登録しない。
STRING_ENUM_KEYS: dict[str, tuple[tuple[str, ...], str]] = {}

# ── hook-groups.yaml 解析・config.json 操作（Issue #2947: shared/app_config.py へ集約） ──
#: 以下はすべて `tidd_tools.shared.app_config` への薄いラッパー。
#: config.py / ai_review/backends.py / ai_review/yaru_auto_tick.py と共通実装を使う。


def _load_hook_groups() -> list[dict[str, Any]]:
    """`.claude/rules/hook-groups.yaml` を読み込んでグループリストを返す（stdlib のみ）.

    Returns:
        [{"name": ..., "label": ..., "description": ..., "hooks": [...]}, ...]
        ファイルが存在しない場合は空リストを返す。
    """
    return app_config.load_hook_groups()


def _get_all_hook_names() -> list[str]:
    """hook-groups.yaml に登録されているすべての hook 名を返す."""
    return app_config.get_all_hook_names()


def _get_config_path() -> Path:
    """OS ネイティブ config ディレクトリの config.json パスを返す."""
    return app_config.config_path()


def _read_config() -> dict[str, Any]:
    """config.json を読み込んで dict として返す（存在しない場合は空 dict）.

    Issue #2400: hooks-config.json → config.json への migration を適用してから読む。
    """
    return app_config.read_config()


def _write_config(config: dict[str, Any]) -> None:
    """config.json に dict を書き込む（親ディレクトリを自動作成）."""
    app_config.write_config(config)


# ── コマンド実装 ──────────────────────────────────────────────────────────────


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "configure",
        help="hook の on/off と秘匿情報管理方式を対話ウィザードで設定する（Issue #1634）",
        description=__doc__,
    )
    add_common_flags(parser)
    parser.add_argument(
        "--set",
        metavar="NAME=on|off",
        dest="set_value",
        help="非対話モード: 指定した hook を on または off にする（例: --set require-issue=off）",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="現在の config.json の内容を表示する",
    )
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    if args.show:
        return _cmd_show()
    if args.set_value:
        return _cmd_set(args.set_value)
    return _cmd_wizard()


# ── --show ────────────────────────────────────────────────────────────────────


def _cmd_show() -> int:
    config = _read_config()
    config_path = _get_config_path()

    if not config:
        print(f"設定ファイルが空か存在しません: {config_path}")
        print("すべての hook はデフォルトで有効（enabled）です。")
        return 0

    print(f"設定ファイル: {config_path}")
    print()
    for key, value in sorted(config.items()):
        if key in STRING_ENUM_KEYS:
            # 文字列 enum キー: 実効値を表示し、不正値（bool 等）は WARN を出す（Issue #2533）
            allowed, default_val = STRING_ENUM_KEYS[key]
            if isinstance(value, str) and value in allowed:
                print(f"  {key}: {value}")
            else:
                # 不正値: WARN を stderr に出して実効値（フォールバック）を stdout に表示
                sys.stderr.write(
                    f"WARN: config.json の '{key}' に不正な値 {value!r} が設定されています。"
                    f"{default_val} を使用します。"
                    f" tidd configure --set {key}={default_val} で修正してください。\n"
                )
                print(f"  {key}: {default_val} (実効値・不正値 {value!r} を無視)")
        else:
            status = "on" if value else "off"
            print(f"  {key}: {str(value).lower()} ({status})")
    return 0


# ── --set ─────────────────────────────────────────────────────────────────────


def _cmd_set(set_value: str) -> int:
    """非対話モード: `name=value` を解析して config.json を更新する.

    - 文字列 enum キー（例: secrets-backend）は STRING_ENUM_KEYS で定義した値域のみ受理する
    - それ以外の hook キーは on/off のみ受理する（従来通り）
    """
    if "=" not in set_value:
        sys.stderr.write(f"Invalid format '{set_value}'. Use 'name=on' or 'name=off'.\n")
        return 1

    name, _, raw_value = set_value.partition("=")
    name = name.strip()
    raw_value = raw_value.strip()

    # 文字列 enum キーの場合は固有の値域でバリデーションする（Issue #2533）
    if name in STRING_ENUM_KEYS:
        allowed, _ = STRING_ENUM_KEYS[name]
        if raw_value not in allowed:
            sys.stderr.write(f"Invalid value '{raw_value}' for '{name}'. Use one of: {', '.join(allowed)}.\n")
            return 1
        # 設定更新（文字列として保存）
        config = _read_config()
        config[name] = raw_value
        _write_config(config)
        config_path = _get_config_path()
        print(f"{name}: {raw_value}")
        print(f"設定を {config_path} に保存しました。")
        return 0

    # 通常の on/off キー: 値バリデーション
    if raw_value not in ("on", "off"):
        sys.stderr.write(f"Invalid value '{raw_value}'. Use 'on' or 'off'.\n")
        return 1

    # hook 名バリデーション
    valid_hooks = _get_all_hook_names()
    if valid_hooks and name not in valid_hooks:
        sys.stderr.write(f"Unknown hook: {name}\n")
        sys.stderr.write(f"Valid hooks: {', '.join(sorted(valid_hooks))}\n")
        return 1

    # 設定更新
    config = _read_config()
    config[name] = raw_value == "on"
    _write_config(config)

    config_path = _get_config_path()
    enabled_str = "enabled" if config[name] else "disabled"
    print(f"{name}: {enabled_str}")
    print(f"設定を {config_path} に保存しました。")
    return 0


# ── 対話ウィザード ─────────────────────────────────────────────────────────────


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    """stdin から Y/n の回答を読み取る（EOF・空行はデフォルト値）."""
    default_hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt} {default_hint}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not answer:
        return default
    return answer.startswith("y")


def _resolve_hook_default(hook_name: str, current_config: dict[str, Any]) -> bool:
    """現在の config.json の値を参照してデフォルトを決定する（Issue #2532）.

    - config.json にキーが存在する場合: その値をデフォルトとして使う（現在値を維持）
    - config.json にキーが存在しない場合: DEFAULT_TRUE_KEYS に基づくデフォルトを使う
    """
    if hook_name in current_config:
        return bool(current_config[hook_name])
    return hook_name in DEFAULT_TRUE_KEYS


def _format_current_value_hint(hook_name: str, current_config: dict[str, Any]) -> str:
    """質問プロンプトに付加する現在値のヒント文字列を返す（Issue #2532）.

    - config.json にキーが存在する場合: "現在: 有効" / "現在: 無効"
    - config.json にキーが存在しない場合: "現在: 未設定（デフォルト: 有効）" / "現在: 未設定（デフォルト: 無効）"
    """
    if hook_name in current_config:
        return "現在: 有効" if current_config[hook_name] else "現在: 無効"
    default_label = "有効" if hook_name in DEFAULT_TRUE_KEYS else "無効"
    return f"現在: 未設定（デフォルト: {default_label}）"


def _cmd_wizard() -> int:
    """対話ウィザードで hook グループを一括設定する."""
    groups = _load_hook_groups()
    if not groups:
        sys.stderr.write("警告: hook-groups.yaml が見つかりません。 --set オプションで個別に設定してください。\n")
        return 1

    print("=" * 60)
    print("tidd configure - hook 設定ウィザード")
    print("=" * 60)
    print()
    print("各 hook グループを有効にするか選択してください。")
    print("（Enter キーのみで現在の設定が維持されます）")
    print()

    config = _read_config()

    for group in groups:
        name = group.get("name", "")
        label = group.get("label", name)
        description = group.get("description", "")
        hooks = group.get("hooks", [])

        if not hooks:
            continue

        print(f"─ {label} ─")
        if description:
            print(f"  {description}")
        print(f"  hook: {', '.join(hooks)}")

        # グループのデフォルト値: 全 hook の現在値が一致すればその値、混在なら最初の hook の現在値
        # （Issue #2532: 現在の config.json の値を参照してデフォルトを決定する）
        default_enabled = _resolve_hook_default(hooks[0], config)

        # 現在値ヒントの表示: グループに hook が 1 つのとき（個別設定グループ）は現在値を表示
        if len(hooks) == 1:
            hint = _format_current_value_hint(hooks[0], config)
            prompt = f"  {label} を有効にしますか？（{hint}）"
        else:
            prompt = f"  {label} を有効にしますか？"

        enabled = _ask_yes_no(prompt, default=default_enabled)
        for hook_name in hooks:
            config[hook_name] = enabled
        print()

    # 秘匿情報の管理方式（Issue #3212: Bitwarden フォールバック廃止・env 方式一本化）
    print("─ 秘匿情報の管理方式 ─")
    print("  シークレットは環境変数のみで解決します（Bitwarden フォールバックは廃止・#3212）。")
    print("  実値は gitignore 済みの .mise.toml に書いてください:")
    print('  # 例: [env] セクションに APP_ID = "<値>" と書く（~/.bashrc には書かないこと）')
    print()
    print("詳細: docs/setup/secrets-management.md")
    print()

    # 設定を保存
    _write_config(config)
    config_path = _get_config_path()
    # 表示用に ~ に省略する（絶対パスは長くて非エンジニアに分かりにくい）
    home = str(Path.home())
    display_path = str(config_path).replace(home, "~", 1)
    print(f"設定を {display_path} に保存しました。")
    print()
    print("現在の設定を確認するには: tidd configure --show")
    print("個別に変更するには: tidd configure --set <hook名>=on/off")
    return 0


# ── エントリポイント ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    sys.exit(run_cli(argparse.Namespace(show=False, set_value=None, verbose=0, dry_run=False, json_output=False)))
