"""バックエンド呼び出し + フォールバックチェーン.

旧 ai-review.sh の §3「バックエンド別レビュー実行」全体を 1:1 で移植する。

公開 API:
- :func:`run_backend_review` — ``AI_REVIEW_BACKEND`` に従ってフォールバックチェーンを実行
- :func:`run_agy_review` — agy バックエンド
- :func:`run_codex_review` — codex バックエンド
- :data:`BackendResult` — レビュー出力 + 終了コード + バックエンド名のタプル

旧 sh の終了コード:
- 0 → 成功（``output`` に結果が入る）
- 3 → すべてのバックエンドが利用不可・失敗（``output`` は空）
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from tidd_tools.ai_review.escalation import record_loop_error
from tidd_tools.ai_review.model_drift import detect_and_report_model_drift
from tidd_tools.ai_review.prompts import build_prompt
from tidd_tools.ai_review.quota import (
    extract_reset_seconds,
    is_quota_exceeded,
    is_quota_skip_active,
    record_quota_exceeded,
)
from tidd_tools.ai_review.state_dir import resolve_state_dir as _resolve_state_dir
from tidd_tools.ai_review.timing_steps import measure_step
from tidd_tools.ai_review.verdict import parse_verdict
from tidd_tools.llm_client import LLMClientError, call_chat_completion
from tidd_tools.shared import app_config, gh_client
from tidd_tools.shared.errors import DiffTooLargeError


def _read_backend_config(key: str, *, default_for_warn: object) -> dict[str, object] | None:
    """config.json を読み込んで dict を返す（読み込み不可 / 不正 JSON 時は None）.

    Issue #2495 / #2861: backend availability・モデル名の両方が同じ config.json を
    参照するため、ファイルパス解決（Issue #2947: shared/app_config.py へ集約）+
    読み込み + JSON パースを共通化する。

    Issue #3569: リポジトリ root の ``.tidd/config.json`` に同キーがあれば
    マシン設定より優先する（優先順位: リポジトリ > マシン > default）。

    Returns:
        マージ済み dict — マシン設定・リポジトリ設定の少なくとも一方が読み込めた場合
        （リポジトリ設定が優先）。
        ``None`` — マシン設定ファイルなし（WARN なし）または不正 JSON（WARN 出力済み）
        かつリポジトリ設定も空の場合。
    """
    config_path = app_config.config_path()

    machine_config: dict[str, object] | None
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError:
        machine_config = None
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            print(
                f"WARN: config.json のパースに失敗しました（不正な JSON）。"
                f"{key} はデフォルト値（{default_for_warn}）を使用します。",
                file=sys.stderr,
            )
            machine_config = None
        else:
            machine_config = parsed if isinstance(parsed, dict) else None

    repo_config = app_config.read_repo_config()

    if machine_config is None and not repo_config:
        return None

    merged: dict[str, object] = dict(machine_config) if machine_config else {}
    merged.update(repo_config)
    return merged


def _get_backend_config_value(key: str, *, default: bool = True) -> bool:
    """config.json から backend availability 設定を読み取る.

    Issue #2495: AI_REVIEW_AGY_ENABLED / AI_REVIEW_CODEX_ENABLED env var を廃止し、
    ~/.config/tidd_tools/config.json の "ai-review-agy" / "ai-review-codex" キーに移行。

    - キーなし / ファイルなし → default（True）
    - 不正 JSON → stderr に WARN を出して default（True）
    - bool 値 → そのまま返す
    """
    config = _read_backend_config(key, default_for_warn=default)
    if config is None or key not in config:
        return default

    value = config[key]
    if isinstance(value, bool):
        return value

    print(
        f"WARN: config.json の '{key}' の値型が不正です（{type(value).__name__}）。"
        f"デフォルト値（{default}）を使用します。",
        file=sys.stderr,
    )
    return default


def _get_backend_config_model(key: str, *, default: str) -> str:
    """config.json からモデル名設定を読み取る（Issue #2861）.

    ``AGY_DEFAULT_MODEL`` / ``CODEX_DEFAULT_MODEL`` のフォールバック値を
    ~/.config/tidd_tools/config.json の "ai-review-agy-model" / "ai-review-codex-model"
    キーから上書きできるようにする。agy/codex 側の新モデルリリースに、ソースコード
    定数を書き換える PR なしで追従できるようにするための仕組み。

    - キーなし / ファイルなし → default（ソースコード定数）
    - 不正 JSON → stderr に WARN を出して default
    - 空文字列 → 未設定として扱い default にフォールバックする（意図しない上書き防止）
    - 文字列以外の値型 → stderr に WARN を出して default
    """
    config = _read_backend_config(key, default_for_warn=default)
    if config is None or key not in config:
        return default

    value = config[key]
    if isinstance(value, str):
        return value if value != "" else default

    print(
        f"WARN: config.json の '{key}' の値型が不正です（{type(value).__name__}）。"
        f"デフォルト値（{default}）を使用します。",
        file=sys.stderr,
    )
    return default


logger = logging.getLogger(__name__)

DIFF_TRUNCATE_BYTES = 512_000  # 旧 sh: 500KB 超は切り詰める

# Backend subprocess timeout（Issue #1298 で 900→300 秒に短縮）
# 実測レスポンス時間 30-120 秒に対して 5 分あれば大規模 PR でも余裕。
# 15 分 × 3 リトライ × 2 バックエンド = 最悪 90 分/PR という監査 §2-3 の指摘への対応。
# 変更履歴: docs/reference/ai-review-timeouts.md
BACKEND_SUBPROCESS_TIMEOUT_SEC = 300

# agy のデフォルトモデル（AGY_MODEL 未設定時）（Issue #1688）
# Issue #2861: ~/.config/tidd_tools/config.json の "ai-review-agy-model" が設定されて
# いればそちらが優先される（_get_backend_config_model 経由）。
# Issue #3827: agy 呼び出し自体は「未設定なら --model を渡さずアカウント既定値に任せる」
# 設計に変更したため、この定数は既に「明示指定」の意味を持たなくなった。実際に使われた
# モデル名は --log-file 経由でログから抽出して記録する（_extract_agy_model_label /
# _resolve_agy_model_id_from_label）。この定数は抽出に失敗した場合の最終フォールバック値
# としてのみ使う（agy CLI へは渡さない）。
AGY_DEFAULT_MODEL = "gemini-3.6-flash-high"

# codex のデフォルトモデル（CODEX_MODEL 未設定時）（Issue #2314）
# codex CLI は `-m/--model` 未指定だとアカウント既定モデルを使うが JSON 出力に
# モデル名が含まれず backend-name への記録・Reviewer フッター表示ができないため、
# agy と同様に明示的にモデルを指定して既知の値を記録する。
# Issue #2861: ~/.config/tidd_tools/config.json の "ai-review-codex-model" が設定されて
# いればそちらが優先される（_get_backend_config_model 経由）。
# Issue #3830: さらに config.json 上書きもなければ `codex debug models` カタログから
# "ai-review-codex-tier"（既定 luna）に一致する priority 最小のモデルを動的解決する
# （resolve_codex_model 経由）。この定数はカタログ取得にも失敗した場合の最終
# フォールバックとして残す。
CODEX_DEFAULT_MODEL = "gpt-5.5"

# Issue #3830: luna tier 動的解決の既定ティア・config.json 上書きキー
CODEX_DEFAULT_TIER = "luna"
_CODEX_TIER_CONFIG_KEY = "ai-review-codex-tier"
_CODEX_MODEL_CONFIG_KEY = "ai-review-codex-model"

# `codex debug models` はカタログ取得のみの軽量コマンドのため、レビュー実行本体の
# BACKEND_SUBPROCESS_TIMEOUT_SEC（300秒）より短いタイムアウトを使う。
CODEX_MODELS_SUBPROCESS_TIMEOUT_SEC = 30

# RUST_LOG=info の codex exec stderr から実際に使用されたモデル slug を抽出する
# アンカー文字列 + 正規表現（Issue #3830）。
_CODEX_GET_DEFAULT_MODEL_ANCHOR = "get_default_model{model.provided=false"
_CODEX_MODEL_LOG_PATTERN = re.compile(r"model=(\S+)\s+slug=(\S+)")

# 旧 sh の awk フィルター: package-lock.json / yarn.lock / uv.lock を除外
_LOCKFILE_PATTERN = ("package-lock.json", "yarn.lock", "uv.lock")

# Issue #3117: フォールバックチェーン要素の正準名。この 4 つ以外は未知として扱う。
KNOWN_CHAIN_ELEMENTS = frozenset({"agy-gemini", "agy-sonnet", "codex", "custom"})

# デフォルトのフォールバックチェーン順序（config.json の "ai-review-chain" 未設定時）。
DEFAULT_CHAIN_ORDER: tuple[str, ...] = ("agy-gemini", "agy-sonnet", "codex")

_CHAIN_CONFIG_KEY = "ai-review-chain"

# チェーン要素の stderr 表示用ラベル。
_CHAIN_DISPLAY_LABELS = {
    "agy-gemini": "agy(gemini)",
    "agy-sonnet": "agy(sonnet)",
    "codex": "codex",
    "custom": "custom",
}


def _resolve_chain_order() -> tuple[str, ...]:
    """config.json の ``ai-review-chain`` からフォールバックチェーンの実行順序を解決する（Issue #3117）.

    - キーなし / ファイルなし → :data:`DEFAULT_CHAIN_ORDER`
    - 値が配列でない、または空配列 → stderr に WARN を出して :data:`DEFAULT_CHAIN_ORDER`
    - 未知の要素（:data:`KNOWN_CHAIN_ELEMENTS` 以外・非文字列）が混在 → stderr に WARN を出して
      その要素をスキップし、既知要素のみで構成した順序を返す
    - スキップの結果、既知要素が 1 つも残らない → stderr に WARN を出して :data:`DEFAULT_CHAIN_ORDER`
    """
    config = _read_backend_config(_CHAIN_CONFIG_KEY, default_for_warn=list(DEFAULT_CHAIN_ORDER))
    if config is None or _CHAIN_CONFIG_KEY not in config:
        return DEFAULT_CHAIN_ORDER

    raw = config[_CHAIN_CONFIG_KEY]
    default_order_display = " → ".join(DEFAULT_CHAIN_ORDER)
    if not isinstance(raw, list) or not raw:
        print(
            f"WARN: config.json の '{_CHAIN_CONFIG_KEY}' の値が不正です（配列でないか空配列）。"
            f"デフォルト順序（{default_order_display}）を使用します。",
            file=sys.stderr,
        )
        return DEFAULT_CHAIN_ORDER

    resolved: list[str] = []
    for item in raw:
        if isinstance(item, str) and item in KNOWN_CHAIN_ELEMENTS:
            resolved.append(item)
        else:
            print(
                f"WARN: config.json の '{_CHAIN_CONFIG_KEY}' に未知の backend 名が含まれています"
                f"（{item!r}）。スキップします。",
                file=sys.stderr,
            )

    if not resolved:
        print(
            f"WARN: config.json の '{_CHAIN_CONFIG_KEY}' に有効な backend 名が1つも含まれていません。"
            f"デフォルト順序（{default_order_display}）を使用します。",
            file=sys.stderr,
        )
        return DEFAULT_CHAIN_ORDER

    return tuple(resolved)


@dataclasses.dataclass
class BackendResult:
    output: str
    exit_code: int
    backend_name: str
    # Issue #2536: exit 3 の失敗区分
    # "transient"  → クォータ超過（時間経過で回復）
    # "permanent"  → 恒久的な環境破損（コマンド未検出・stub・実行失敗）
    # "disabled"   → 利用者が config.json で明示的に無効化（破損ではない）（Issue #2541）
    # "parse-failure" → exit 0 だが VERDICT を抽出できなかった（Issue #3759）
    # ""           → exit_code != 3 の場合は空文字
    failure_kind: str = ""
    # exit 3 の原因説明（stderr メッセージに対応）
    failure_reason: str = ""


_SHELL_SHEBANGS = (
    "#!/bin/bash",
    "#!/bin/sh",
    "#!/bin/zsh",
    "#!/bin/dash",
    "#!/usr/bin/env bash",
    "#!/usr/bin/env sh",
    "#!/usr/bin/env zsh",
    "#!/usr/bin/env dash",
    "#!/usr/bin/bash",
    "#!/usr/bin/sh",
    "#!/usr/bin/zsh",
    "#!/usr/bin/dash",
)


def is_codex_stub(codex_path: str) -> bool:
    """PATH 上の codex バイナリが shell script stub かどうかを判定する.

    Issue #2527: stub（``#!/bin/bash`` 等 + 無条件 VERDICT: APPROVE 出力）を
    backend として採用すると fake APPROVE でマージされる問題を防ぐ。

    判定基準: ファイルの先頭 512 バイトが shell script shebang で始まるか。
    - True  → shell script stub（``#!/bin/bash``・``#!/bin/sh``・``#!/usr/bin/env bash`` 等）
    - False → バイナリ（Node.js 等）・Node.js shebang・空ファイル・読み取り不能

    Node.js shebang（``#!/usr/bin/env node``）は shell stub ではないため False を返す。
    """
    try:
        path = Path(codex_path)
        raw = path.read_bytes()[:512]
    except OSError:
        return False

    try:
        header = raw.decode("utf-8-sig", errors="replace")
    except Exception:
        return False

    lines = header.splitlines()
    if not lines:
        return False

    first_line = lines[0].strip()
    return any(
        first_line == prefix or first_line.startswith(prefix + " ") or first_line.startswith(prefix + "\t")
        for prefix in _SHELL_SHEBANGS
    )


def _strip_lockfile_diffs(diff: str) -> str:
    """``diff --git`` ブロック単位で lock ファイルを除外する."""
    lines = diff.splitlines(keepends=True)
    out: list[str] = []
    skip = False
    for line in lines:
        if line.startswith("diff --git "):
            skip = any(pat in line for pat in _LOCKFILE_PATTERN)
        if not skip:
            out.append(line)
    return "".join(out)


def _fetch_pr_diff(pr_num: str, repo: str) -> str:
    """``gh_client.pr_diff()``（#2940）を呼び出し lock ファイルを除外した diff を返す（Issue #2963）.

    ``gh_client.pr_diff`` は失敗時に空文字列を返す fail-soft 方針のため、空文字列は
    取得失敗とみなし ``RuntimeError`` を送出する（呼び出し元の run_agy_review /
    run_codex_review は RuntimeError を捕捉してエラーメッセージ付き BackendResult を
    返す）。gh CLI の詳細なエラー内容は gh_client 側の WARN ログに出力される。

    Issue #3743: diff が 20000 行上限（406）を超える場合は ``gh_client.pr_diff`` が
    ``DiffTooLargeError`` を送出するため、本関数はそのまま伝播する（呼び出し元が
    ``except DiffTooLargeError`` でクォータ枯渇と区別して扱う）。
    """
    diff = gh_client.pr_diff(pr_num, repo=repo)
    if not diff:
        raise RuntimeError("gh pr diff failed: empty result from gh_client.pr_diff (see WARN log for detail)")
    return _strip_lockfile_diffs(diff)


def _state_dir(pr_num: str) -> Path:
    """``$STATE_DIR`` を Path として返す（未設定時は ``~/.cache/.../pr-{pr_num}``）."""
    return _resolve_state_dir(pr_num)


def _ts_filename(prefix: str) -> str:
    """旧 sh の ``date +%Y%m%dT%H%M%S`` 形式のログファイル名."""
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.log"


def _save_backend_log(prefix: str, output: str, *, pr_num: str) -> Path | None:
    state = _state_dir(pr_num)
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    log_path = state / _ts_filename(prefix)
    try:
        log_path.write_text(output, encoding="utf-8")
    except OSError:
        return None
    return log_path


def _write_backend_name(name: str, model: str | None = None, *, pr_num: str) -> None:
    """``STATE_DIR/backend-name`` にバックエンド名（+ モデル名）を書き込む.

    ``model`` を指定した場合は ``"<name>:<model>"`` 形式で保存する（Issue #1688）。
    ``model`` が None の場合は ``"<name>"`` のみ保存（後方互換）。

    - ``_write_backend_name("agy", "gemini-3.6-flash-high", pr_num="123")`` → ``"agy:gemini-3.6-flash-high"``
    - ``_write_backend_name("codex", pr_num="123")`` → ``"codex"``
    """
    state = _state_dir(pr_num)
    content = f"{name}:{model}" if model else name
    try:
        state.mkdir(parents=True, exist_ok=True)
        (state / "backend-name").write_text(f"{content}\n", encoding="utf-8")
    except OSError as exc:
        logger.debug("backend-name 書き込み失敗: %s", exc)


def _gh_env_with_token(app_token: str) -> dict[str, str]:
    env = dict(os.environ)
    if app_token:
        env["GH_TOKEN"] = app_token
    return env


# ── agy ──────────────────────────────────────────────────────────────────────

# Issue #3827: agy --log-file の詳細ログに出力される、実際に使用されたモデルの
# label を抽出する正規表現。gemini 系モデル（アカウント既定値）だけでなく
# `--model claude-sonnet-4-6` のような明示指定モデルでも同じ行形式で出力されることを
# 実機確認済み（Issue 本文参照）。
_AGY_LOG_MODEL_LABEL_PATTERN = re.compile(r'Propagating selected model override to backend: label="([^"]*)"')

# agy models の出力タイムアウト（カタログ取得だけのため短く抑える）
_AGY_MODELS_SUBPROCESS_TIMEOUT_SEC = 30


def _extract_agy_model_label(log_text: str) -> str | None:
    """agy --log-file の内容から実際に使用されたモデルの label を抽出する（Issue #3827）.

    ``Propagating selected model override to backend: label="..."`` 行が見つからない
    場合（ログが空・agy バージョン差異・出力形式変更等）は ``None`` を返す。
    """
    if not log_text:
        return None
    match = _AGY_LOG_MODEL_LABEL_PATTERN.search(log_text)
    if not match:
        return None
    label = match.group(1).strip()
    return label or None


def _agy_models_catalog() -> dict[str, str]:
    """``agy models`` サブコマンドの出力から label → id のマッピングを取得する（Issue #3827）.

    出力は通常 ``<id>\\t<label>`` のタブ区切り行だが、agy のバージョンによって
    label のみの行になる場合もある。後者はモデル名の命名規則から ID を復元する
    （例: ``Gemini 3.7 Flash (High)`` → ``gemini-3.7-flash-high``）。

    agy コマンド未検出・タイムアウト・非ゼロ終了・パース不能な行はすべて無視し、
    例外を送出せず空 dict を返す（呼び出し元の label→id 変換はフォールバックで継続する）。
    """
    try:
        completed = subprocess.run(  # noqa: S603, S607 — 固定 argv
            ["agy", "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_AGY_MODELS_SUBPROCESS_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("agy models 呼び出し失敗: %s", exc)
        return {}

    if completed.returncode != 0:
        logger.debug("agy models が非ゼロ終了（%s）: %s", completed.returncode, completed.stderr)
        return {}

    catalog: dict[str, str] = {}
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("Fetching "):
            continue
        parts = line.split("\t", maxsplit=1)
        if len(parts) == 2:
            model_id, label = (part.strip() for part in parts)
        else:
            label = parts[0]
            model_id = re.sub(r"[()]", "", label)
            model_id = re.sub(r"[^A-Za-z0-9.]+", "-", model_id).strip("-").lower()
            if model_id.startswith("claude-"):
                model_id = model_id.replace(".", "-").removesuffix("-thinking")
        if model_id and label:
            catalog[label] = model_id
    return catalog


def _resolve_agy_model_id_from_label(label: str) -> str:
    """agy モデルの label を backend-name 記録用の id 形式へ変換する（Issue #3827）.

    ``agy models`` カタログに一致する label が見つからない場合（呼び出し失敗・
    カタログ未掲載の新モデル等）は例外を送出せず label をそのまま返す。
    """
    catalog = _agy_models_catalog()
    return catalog.get(label, label)


def run_agy_review(
    pr_num: str,
    repo: str,
    app_token: str = "",
    *,
    agy_model: str | None = None,
    repo_root: Path | None = None,
    attempt: int = 1,
    prev_issues_file: Path | None = None,
) -> BackendResult:
    """agy バックエンドでレビューを実行する.

    旧 sh ``run_agy_review`` の挙動を踏襲する。
    """
    if shutil.which("agy") is None:
        print("ERROR: agy コマンドが見つかりません。", file=sys.stderr)
        return BackendResult(output="", exit_code=1, backend_name="agy")

    env = _gh_env_with_token(app_token)
    prompt = build_prompt(
        pr_num,
        repo,
        backend="agy",
        attempt=attempt,
        prev_issues_file=prev_issues_file,
        repo_root=repo_root,
    )

    try:
        with measure_step(pr_num, "pr-context-collection"):
            diff = _fetch_pr_diff(pr_num, repo)
    except DiffTooLargeError as exc:
        # Issue #3743: diff 20000 行上限超過はクォータ枯渇とは異なる原因のため、
        # failure_kind="diff-too-large" で区別する（exit 3 メッセージが QUOTA_EXCEEDED と
        # 誤帰属しないように）。
        err_msg = f"ERROR: PR diff が20000行上限を超えています（DIFF_TOO_LARGE）。{exc}"
        print(err_msg, file=sys.stderr)
        _save_backend_log("agy-review", err_msg, pr_num=pr_num)
        return BackendResult(
            output="",
            exit_code=3,
            backend_name="agy",
            failure_kind="diff-too-large",
            failure_reason=err_msg,
        )
    except RuntimeError as exc:
        err_msg = f"ERROR: gh pr diff の取得に失敗しました（{exc}）。agy レビューを中止します。"
        print(err_msg, file=sys.stderr)
        _save_backend_log("agy-review", err_msg, pr_num=pr_num)
        return BackendResult(output="", exit_code=1, backend_name="agy")

    if len(diff.encode("utf-8")) > DIFF_TRUNCATE_BYTES:
        print(
            f"==> diff が {len(diff.encode('utf-8'))} バイト（>500KB）のため先頭 500KB に切り詰めます"
            "（プロンプト肥大防止）。",
            file=sys.stderr,
        )
        encoded = diff.encode("utf-8")[:DIFF_TRUNCATE_BYTES]
        diff = encoded.decode("utf-8", errors="ignore") + "\n\n[... diff truncated due to size ...]"

    full_input = f"{prompt}\n{diff}\n"

    # Issue #3827: config.json の "ai-review-agy-model" / AGY_MODEL 環境変数（呼び出し元が
    # agy_model 引数へ解決済み）のいずれも未設定なら --model を渡さず agy のアカウント既定値
    # に任せる。明示指定の有無にかかわらず、実際に使用されたモデルは --log-file 経由で
    # 事後的に特定する。
    override_model = agy_model or _get_backend_config_model("ai-review-agy-model", default="")

    cmd = ["agy"]
    # #2207: agy 1.1.3 の headless permission 厳格化で allow-list が効かない upstream bug (#565)
    # に遭遇するため、明示的に全 tool 承認する flag を渡す。escape hatch は env で切り替え。
    if os.environ.get("AI_REVIEW_AGY_ALLOW_PERMISSIONS_PROMPT") != "1":
        cmd.append("--dangerously-skip-permissions")
    if override_model:
        cmd.extend(["--model", override_model])

    # Issue #3827: --model を明示指定しない場合でも実際に使用されたモデル名を
    # backend-name に記録するため、一時ログファイルへ --log-file を渡す。
    # サブプロセス終了後（正常終了・タイムアウト・OSError いずれの経路でも）確実に削除する。
    try:
        log_fd, log_path_str = tempfile.mkstemp(prefix="agy-log-", suffix=".log")
    except OSError as exc:
        err_msg = f"==> agy ログ一時ファイル作成失敗（OSError: {exc}）: フォールバックします。"
        print(err_msg, file=sys.stderr)
        return BackendResult(output="", exit_code=1, backend_name="agy")
    os.close(log_fd)
    agy_log_path = Path(log_path_str)
    cmd.extend(["--log-file", str(agy_log_path)])

    try:
        # agy 1.1.1 は prompt flag なし + 非 TTY stdin で prompt を stdin から読む
        # （undocumented・upstream issue #582 参照）。
        # argv 渡しは大きな diff で E2BIG (#1988) になるため stdin 渡しに統一する。
        try:
            with measure_step(pr_num, "backend-subprocess-execution"):
                completed = subprocess.run(  # noqa: S603 — argv はリスト構築済み
                    cmd,
                    input=full_input,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    check=False,
                    timeout=BACKEND_SUBPROCESS_TIMEOUT_SEC,
                )
        except subprocess.TimeoutExpired:
            err_msg = (
                f"==> agy 呼び出しが timeout しました（{BACKEND_SUBPROCESS_TIMEOUT_SEC}秒）: フォールバックします。"
            )
            print(err_msg, file=sys.stderr)
            return BackendResult(output="", exit_code=1, backend_name="agy")
        except OSError as exc:
            err_msg = f"==> agy 呼び出し失敗（OSError: {exc}）: フォールバックします。"
            print(err_msg, file=sys.stderr)
            return BackendResult(output="", exit_code=1, backend_name="agy")

        try:
            log_text = agy_log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_text = ""

        # 明示指定の有無にかかわらず、agy が実際に選択したモデルをログから解決する。
        # ログに該当行がない場合だけ、明示値または旧既定値へフォールバックする。
        label = _extract_agy_model_label(log_text)
        if label is not None:
            resolved_model = _resolve_agy_model_id_from_label(label)
        else:
            resolved_model = override_model or AGY_DEFAULT_MODEL

        review_output = (completed.stdout or "") + (completed.stderr or "")

        # Issue #2380: agy CLI 側のモデルカタログ改定で agy_model 指定が無効化されても
        # 静かに次バックエンドへフォールバックしてしまうため、reactive に検知して自動起票する。
        detect_and_report_model_drift(review_output, pr_num, repo)

        if not review_output.strip():
            print(
                "==> agy が空レスポンスを返しました（空 output）。フォールバックバックエンドに切り替えます。",
                file=sys.stderr,
            )
            # Issue #2535: 空出力をクォータ文字列に置き換えない。
            # 旧実装は "Individual quota reached (empty output from agy)" に置き換えたが、
            # これが _try_agy_step の is_quota_exceeded に一致しクォータとして誤記録された。
            # 空出力はクォータとは独立した一時的障害（認証切れ・crash 等）のため、
            # 呼び出し元 _try_agy_step で別扱いする。
            return BackendResult(output="", exit_code=completed.returncode, backend_name=f"agy:{resolved_model}")

        log_path = _save_backend_log("agy-review", review_output, pr_num=pr_num)
        if log_path is not None:
            print(f"==> agy 生ログ保存: {log_path}", file=sys.stderr)

        _write_backend_name("agy", resolved_model, pr_num=pr_num)
        return BackendResult(output=review_output, exit_code=completed.returncode, backend_name=f"agy:{resolved_model}")
    finally:
        try:
            agy_log_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("agy --log-file 一時ファイル削除失敗: %s", exc)


# ── codex ────────────────────────────────────────────────────────────────────


def _fetch_codex_model_catalog(codex_path: str) -> list[dict[str, object]] | None:
    """``codex debug models`` を実行してモデルカタログを取得する（Issue #3830）.

    - subprocess 実行失敗（``OSError`` / タイムアウト）→ ``None``（stderr に WARN）
    - 非ゼロ終了コード → ``None``（stderr に WARN）
    - stdout が空文字列 → ``None``（stderr に WARN）
    - 不正な JSON → ``None``（stderr に WARN）
    - トップレベルが ``{"models": [...]}`` 形式でも配列そのものでも受理する。
      いずれでもない場合は ``None``（stderr に WARN）
    """
    try:
        completed = subprocess.run(  # noqa: S603 — argv は固定リスト
            [codex_path, "debug", "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=CODEX_MODELS_SUBPROCESS_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"WARN: codex debug models の実行に失敗しました（{exc}）。既存の固定モデルへフォールバックします。",
            file=sys.stderr,
        )
        return None

    if completed.returncode != 0:
        print(
            f"WARN: codex debug models が非ゼロ終了コード（{completed.returncode}）を返しました。"
            "既存の固定モデルへフォールバックします。",
            file=sys.stderr,
        )
        return None

    stdout = completed.stdout or ""
    if not stdout.strip():
        print(
            "WARN: codex debug models の出力が空です。既存の固定モデルへフォールバックします。",
            file=sys.stderr,
        )
        return None

    try:
        data: object = json.loads(stdout)
    except json.JSONDecodeError:
        print(
            "WARN: codex debug models の出力が不正な JSON です。既存の固定モデルへフォールバックします。",
            file=sys.stderr,
        )
        return None

    if isinstance(data, dict):
        data = data.get("models")

    if not isinstance(data, list):
        print(
            "WARN: codex debug models の出力形式が不正です（models 配列が見つかりません）。"
            "既存の固定モデルへフォールバックします。",
            file=sys.stderr,
        )
        return None

    return [entry for entry in data if isinstance(entry, dict)]


def _select_tier_model(catalog: list[dict[str, object]], tier: str) -> str | None:
    """カタログから ``slug`` が ``-<tier>`` で終わるエントリのうち priority 最小の slug を返す（Issue #3830）.

    該当エントリが1件もない場合は ``None`` を返す。
    """
    suffix = f"-{tier}"
    candidates: list[tuple[float, str]] = []
    for entry in catalog:
        slug = entry.get("slug")
        priority = entry.get("priority")
        if (
            isinstance(slug, str)
            and slug.endswith(suffix)
            and isinstance(priority, int | float)
            and not isinstance(priority, bool)
        ):
            candidates.append((float(priority), slug))
    if not candidates:
        return None
    _, best_slug = min(candidates, key=lambda pair: pair[0])
    return best_slug


def resolve_codex_model(
    *,
    explicit_model: str | None,
    codex_path: str | None,
    default_model: str = CODEX_DEFAULT_MODEL,
    default_tier: str = CODEX_DEFAULT_TIER,
) -> str:
    """codex 呼び出しに使うモデル名を解決する（Issue #3830）.

    優先順位:

    1. ``explicit_model``（呼び出し元指定 / ``CODEX_MODEL`` 環境変数）
    2. config.json ``"ai-review-codex-model"``（固定モデル明示指定・カタログ取得をスキップ）
    3. ``codex debug models`` カタログから ``"ai-review-codex-tier"``（既定 ``"luna"``）
       のサフィックスに一致する priority 最小のエントリ
    4. カタログ取得失敗・該当エントリなし → ``default_model``（``CODEX_DEFAULT_MODEL``）
    """
    if explicit_model:
        return explicit_model

    config_model = _get_backend_config_model(_CODEX_MODEL_CONFIG_KEY, default="")
    if config_model:
        return config_model

    if not codex_path:
        return default_model

    catalog = _fetch_codex_model_catalog(codex_path)
    if catalog is None:
        return default_model

    tier = _get_backend_config_model(_CODEX_TIER_CONFIG_KEY, default=default_tier)
    selected = _select_tier_model(catalog, tier)
    if selected is None:
        print(
            f'WARN: codex モデルカタログに tier="{tier}" に一致するエントリがありません。'
            "既存の固定モデルへフォールバックします。",
            file=sys.stderr,
        )
        return default_model
    return selected


def extract_codex_model_from_log(stderr_text: str) -> str | None:
    """``RUST_LOG=info`` の codex exec stderr ログから実際に使用されたモデル slug を抽出する（Issue #3830）.

    ``get_default_model{model.provided=false`` の出現以降で最初に見つかる
    ``model=<slug> slug=<slug>`` の slug を返す。アンカーが見つからない、または
    アンカー以降に該当行がない場合は ``None`` を返す。
    """
    if not stderr_text:
        return None
    anchor_idx = stderr_text.find(_CODEX_GET_DEFAULT_MODEL_ANCHOR)
    if anchor_idx == -1:
        return None
    match = _CODEX_MODEL_LOG_PATTERN.search(stderr_text, anchor_idx)
    if match is None:
        return None
    return match.group(2)


def run_codex_review(
    pr_num: str,
    repo: str,
    app_token: str = "",
    *,
    codex_model: str | None = None,
    repo_root: Path | None = None,
    attempt: int = 1,
    prev_issues_file: Path | None = None,
) -> BackendResult:
    """codex バックエンドでレビューを実行する.

    旧 sh ``run_codex_review`` を移植する。
    多層防御:
    1. ``env -i`` 相当 — GH_TOKEN・PRIVATE_KEY_CONTENT 等を明示的に外す
    2. 一時 HOME に ``auth.json`` のみコピー
    3. ``--sandbox read-only`` / ``--ignore-user-config`` / ``--ignore-rules``
    """
    codex_path = shutil.which("codex")
    if codex_path is None:
        print("ERROR: codex コマンドが見つかりません。", file=sys.stderr)
        return BackendResult(output="", exit_code=1, backend_name="codex")

    prompt = build_prompt(
        pr_num,
        repo,
        backend="codex",
        attempt=attempt,
        prev_issues_file=prev_issues_file,
        repo_root=repo_root,
    )

    try:
        with measure_step(pr_num, "pr-context-collection"):
            diff = _fetch_pr_diff(pr_num, repo)
    except DiffTooLargeError as exc:
        # Issue #3743: diff 20000 行上限超過はクォータ枯渇とは異なる原因のため区別する。
        err_msg = f"ERROR: PR diff が20000行上限を超えています（DIFF_TOO_LARGE）。{exc}"
        print(err_msg, file=sys.stderr)
        _save_backend_log("codex-review", err_msg, pr_num=pr_num)
        return BackendResult(
            output="",
            exit_code=3,
            backend_name="codex",
            failure_kind="diff-too-large",
            failure_reason=err_msg,
        )
    except RuntimeError as exc:
        err_msg = f"ERROR: gh pr diff の取得に失敗しました（{exc}）。codex レビューを中止します。"
        print(err_msg, file=sys.stderr)
        _save_backend_log("codex-review", err_msg, pr_num=pr_num)
        return BackendResult(output="", exit_code=1, backend_name="codex")

    home = Path.home()
    auth_src = home / ".codex" / "auth.json"

    with tempfile.TemporaryDirectory() as tmp_home_str:
        tmp_home = Path(tmp_home_str)
        (tmp_home / ".codex").mkdir(parents=True, exist_ok=True)
        if auth_src.is_file():
            try:
                (tmp_home / ".codex" / "auth.json").write_bytes(auth_src.read_bytes())
            except OSError as exc:
                logger.debug("codex auth.json コピー失敗: %s", exc)

        env: dict[str, str] = {
            "HOME": str(tmp_home),
            "PATH": os.environ.get("PATH", ""),
            "TERM": os.environ.get("TERM", "xterm"),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            # Issue #3830: stderr の verbose ログから実際に使用されたモデル slug を
            # 抽出する（extract_codex_model_from_log）ために明示的に有効化する。
            "RUST_LOG": "info",
        }
        _valid_reasoning_efforts = {"minimal", "low", "medium", "high"}
        _raw_effort = os.environ.get("CODEX_REASONING_EFFORT", "").strip().lower()
        reasoning_effort = _raw_effort if _raw_effort in _valid_reasoning_efforts else "high"
        # Issue #2314: モデル名を明示指定する（codex CLI は -m 未指定だとアカウント既定モデルを
        # 使うが JSON 出力にモデル名が含まれず backend-name への記録ができないため）。
        # Issue #2861: config.json の "ai-review-codex-model" でソースコード既定値を上書き可能にする。
        # Issue #3830: 明示指定がなければ codex debug models カタログから luna tier を動的解決する
        # （resolve_codex_model 経由）。
        resolved_model = resolve_codex_model(
            explicit_model=codex_model or os.environ.get("CODEX_MODEL"),
            codex_path=codex_path,
        )
        cmd = [
            codex_path,
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "-m",
            resolved_model,
            "-c",
            f"model_reasoning_effort={reasoning_effort}",
            "-",
        ]
        stdin_payload = f"{prompt}\n{diff}\n"
        try:
            with measure_step(pr_num, "backend-subprocess-execution"):
                completed = subprocess.run(  # noqa: S603 — argv はリスト構築済み
                    cmd,
                    input=stdin_payload,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    check=False,
                    timeout=BACKEND_SUBPROCESS_TIMEOUT_SEC,
                )
        except subprocess.TimeoutExpired:
            err_msg = (
                f"==> codex 呼び出しが timeout しました（{BACKEND_SUBPROCESS_TIMEOUT_SEC}秒）: フォールバックします。"
            )
            print(err_msg, file=sys.stderr)
            return BackendResult(output="", exit_code=1, backend_name="codex")

        # Issue #3157: サブプロセス終了後、一時 HOME の auth.json が更新されていれば
        # 本来の ~/.codex/auth.json へ書き戻す（リフレッシュトークン循環 401 の根本対策）。
        # 複数プロセス同時書き込みのレース対策: 読み取り→比較→書き込みの前後で内容を確認しない
        # 代わりに原子的な write_bytes + 破損検証を行う。
        tmp_auth = tmp_home / ".codex" / "auth.json"
        if auth_src.is_file() and tmp_auth.is_file():
            try:
                tmp_content = tmp_auth.read_bytes()
                # 破損チェック: 有効な JSON かどうか検証する
                json.loads(tmp_content)
                original_content = auth_src.read_bytes()
                if tmp_content != original_content:
                    auth_src.write_bytes(tmp_content)
                    logger.debug("codex auth.json を書き戻しました（トークンがリフレッシュされた可能性）")
            except (OSError, UnicodeDecodeError) as exc:
                logger.debug("codex auth.json 書き戻し失敗（I/O エラー）: %s", exc)
            except ValueError:
                # 一時 HOME の auth.json が有効な JSON でない → 書き戻しをスキップして警告
                print(
                    "WARNING: 一時 HOME の auth.json が有効な JSON でないため書き戻しをスキップします",
                    file=sys.stderr,
                )

    # Issue #2550: stdout と stderr を改行を挟んで連結し、境界での JSON 行破壊を防ぐ。
    # _extract_codex_text は非 JSON 行を skip する寛容実装のため stderr 混入は無害。
    stdout_part = completed.stdout or ""
    stderr_part = completed.stderr or ""
    raw_output = "\n".join(filter(None, [stdout_part, stderr_part]))

    # Issue #3830: RUST_LOG=info の stderr ログから実際に使用されたモデル slug を抽出し、
    # backend-name / Reviewer フッターには resolved_model（要求値）ではなくこちらを優先する
    # （ログ抽出に失敗した場合のみ resolved_model にフォールバックする）。
    actual_model = extract_codex_model_from_log(stderr_part) or resolved_model

    # Issue #2530: codex exec --json を要求しているのに JSON Lines でない出力は棄却する。
    # これは閾値ヒューリスティクスと違い決定的に判定できる封筒検証であり、
    # stub（平文）・環境破損を最も誤検知なく検出できる。
    # Issue #2550: 検証対象を stdout のみにする（stderr の非 JSON 診断行は検証に含めない）。
    # AI_REVIEW_SKIP_VERDICT_AUTHENTICITY=1 で escape hatch。
    if (
        os.environ.get("AI_REVIEW_SKIP_VERDICT_AUTHENTICITY") != "1"
        and stdout_part.strip()
        and not is_codex_output_json_lines(stdout_part)
    ):
        reason = (
            "codex 出力が JSON Lines 形式でない（codex exec --json を要求しているが平文が返った）。"
            " fake APPROVE を防ぐため backend 利用不可として扱います。"
            " escape hatch: AI_REVIEW_SKIP_VERDICT_AUTHENTICITY=1"
        )
        print(f"ERROR: {reason}", file=sys.stderr)
        # Issue #2550: 棄却時も生出力を backend log に保存して事後調査を可能にする。
        log_path = _save_backend_log("codex-review", raw_output, pr_num=pr_num)
        if log_path is not None:
            print(f"==> codex 生ログ保存: {log_path}", file=sys.stderr)
        return BackendResult(
            output="",
            exit_code=3,
            backend_name=f"codex:{actual_model}",
            failure_kind="permanent",
            failure_reason=reason,
        )

    review_output = _extract_codex_text(raw_output) or raw_output

    log_path = _save_backend_log("codex-review", review_output, pr_num=pr_num)
    if log_path is not None:
        print(f"==> codex 生ログ保存: {log_path}", file=sys.stderr)

    _write_backend_name("codex", actual_model, pr_num=pr_num)
    # Issue #3157: 非 zero 終了時は stderr 内容を failure_reason に含め、_try_codex が
    # 上位 reason に反映できるようにする（auth エラー等の真因を GitHub Issue で可視化するため）。
    failure_reason_for_exit = stderr_part.strip() if completed.returncode != 0 and stderr_part.strip() else ""
    return BackendResult(
        output=review_output,
        exit_code=completed.returncode,
        backend_name=f"codex:{actual_model}",
        failure_reason=failure_reason_for_exit,
    )


def extract_codex_text(raw_output: str) -> str:
    """codex の JSON Lines から ``item.completed`` のテキストを抽出する.

    Issue #2963: backends.py / yaru_auto_tick.py に重複していた実装を本関数へ
    一本化する（yaru_auto_tick.py は本関数を import して使う）。空文字列入力・
    不正 JSON 行混在時も例外を送出せず、抽出できたテキストのみを結合して返す
    （テキストなしの場合は空文字列 ``""`` を返す）。
    """
    import json as _json

    texts: list[str] = []
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        t = obj.get("type", "")
        if t not in {"item.completed", "item_completed", "item/completed"}:
            continue
        item = obj.get("item", obj)
        if not isinstance(item, dict):
            continue
        text = item.get("text", "")
        if not text:
            content = item.get("content", [])
            if isinstance(content, list):
                text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            elif isinstance(content, str):
                text = content
        if isinstance(text, str) and text:
            texts.append(text)
    return "\n".join(texts)


# Issue #2963: 旧 private 名との後方互換 alias（既存テスト・run_codex_review の
# mocker.patch.object 差し替え対象名を維持する）。
_extract_codex_text = extract_codex_text


def is_codex_output_json_lines(raw_output: str) -> bool:
    """codex の stdout が JSON Lines 形式かどうかを判定する（Issue #2530）.

    ``codex exec --json`` を指定して JSON Lines を要求しているのに
    平文が返る場合は偽のレビューである可能性が高い（stub や破損環境）。

    Issue #2550: 呼び出し元は stderr を含めない ``completed.stdout`` のみを渡すこと。
    stderr に非 JSON の診断行が含まれても本関数には到達しない設計になっている。

    判定基準: 非空行が 1 行以上あり、そのいずれも JSON パース可能であること。
    全行が JSON Lines でない（少なくとも 1 行が JSON パース不能）なら False を返す。
    空出力は False を返す（呼び出し元で別処理）。
    """
    import json as _json

    non_empty_lines = [line.strip() for line in raw_output.splitlines() if line.strip()]
    if not non_empty_lines:
        return False
    for line in non_empty_lines:
        try:
            _json.loads(line)
        except _json.JSONDecodeError:
            return False
    return True


# ── custom（OpenAI 互換 API） ──────────────────────────────────────────────────


def run_custom_review(
    pr_num: str,
    repo: str,
    app_token: str = "",
    *,
    repo_root: Path | None = None,
    attempt: int = 1,
    prev_issues_file: Path | None = None,
) -> BackendResult:
    """custom backend（OpenAI 互換 API）でレビューを実行する（Issue #3116）.

    ``AI_REVIEW_BACKEND=custom`` の固定実行時のみ呼ばれる。auto フォールバック
    チェーン（``_run_fallback_chain_from_agy``）には組み込まない（#3117 のスコープ）。
    失敗時は常に exit_code=3 + failure_kind を設定した BackendResult を返す
    （agy/codex のようなサブプロセス終了コードの概念がないため）。
    """
    config = _get_custom_backend_config()
    if config is None:
        reason = "custom-backend が config.json に定義されていません（base-url / model / api-key-env が必要です）"
        print(f"ERROR: {reason}", file=sys.stderr)
        return BackendResult(
            output="", exit_code=3, backend_name="custom", failure_kind="permanent", failure_reason=reason
        )

    base_url = config["base-url"]
    model = config["model"]
    api_key_env = config["api-key-env"]
    backend_label = config.get("display-name") or f"custom:{model}"

    api_key = os.environ.get(api_key_env)
    if not api_key:
        reason = f"環境変数 {api_key_env}（custom-backend.api-key-env が指す変数）が未設定です"
        print("ERROR: custom backend API key is not configured", file=sys.stderr)
        return BackendResult(
            output="", exit_code=3, backend_name=backend_label, failure_kind="permanent", failure_reason=reason
        )

    prompt = build_prompt(
        pr_num,
        repo,
        backend="custom",
        attempt=attempt,
        prev_issues_file=prev_issues_file,
        repo_root=repo_root,
    )

    try:
        with measure_step(pr_num, "pr-context-collection"):
            diff = _fetch_pr_diff(pr_num, repo)
    except DiffTooLargeError as exc:
        # Issue #3743: diff 20000 行上限超過はクォータ枯渇とは異なる原因のため区別する。
        reason = f"PR diff が20000行上限を超えています（DIFF_TOO_LARGE）。{exc}"
        print(f"ERROR: {reason}。custom レビューを中止します。", file=sys.stderr)
        return BackendResult(
            output="", exit_code=3, backend_name=backend_label, failure_kind="diff-too-large", failure_reason=reason
        )
    except RuntimeError as exc:
        reason = f"gh pr diff の取得に失敗しました（{exc}）"
        print(f"ERROR: {reason}。custom レビューを中止します。", file=sys.stderr)
        return BackendResult(
            output="", exit_code=3, backend_name=backend_label, failure_kind="permanent", failure_reason=reason
        )

    if len(diff.encode("utf-8")) > DIFF_TRUNCATE_BYTES:
        print(
            f"==> diff が {len(diff.encode('utf-8'))} バイト（>500KB）のため先頭 500KB に切り詰めます"
            "（プロンプト肥大防止）。",
            file=sys.stderr,
        )
        encoded = diff.encode("utf-8")[:DIFF_TRUNCATE_BYTES]
        diff = encoded.decode("utf-8", errors="ignore") + "\n\n[... diff truncated due to size ...]"

    full_prompt = f"{prompt}\n{diff}\n"

    print(f"==> custom backend（{backend_label}）でレビューを試みます...", file=sys.stderr)
    try:
        with measure_step(pr_num, "backend-subprocess-execution"):
            review_output = call_chat_completion(
                base_url=base_url,
                model=model,
                api_key=api_key,
                prompt=full_prompt,
                timeout=BACKEND_SUBPROCESS_TIMEOUT_SEC,
            )
    except LLMClientError as exc:
        status = exc.status_code
        if status in (401, 403):
            failure_kind = "permanent"
        elif status == 429:
            failure_kind = "transient"
            record_quota_exceeded("custom")
        else:
            # 5xx・タイムアウト・接続失敗・応答不備（choices 欠落等）
            failure_kind = "transient"
        reason = str(exc)
        print(f"ERROR: custom backend 呼び出しに失敗しました（{reason}）。", file=sys.stderr)
        _save_backend_log("custom-review", reason, pr_num=pr_num)
        return BackendResult(
            output="", exit_code=3, backend_name=backend_label, failure_kind=failure_kind, failure_reason=reason
        )

    log_path = _save_backend_log("custom-review", review_output, pr_num=pr_num)
    if log_path is not None:
        print(f"==> custom 生ログ保存: {log_path}", file=sys.stderr)

    _write_backend_name(backend_label, pr_num=pr_num)
    return BackendResult(output=review_output, exit_code=0, backend_name=backend_label)


# ── ディスパッチャ ─────────────────────────────────────────────────────────────


def _agy_enabled() -> bool:
    """agy backend が有効かどうかを返す.

    Issue #2495: env var AI_REVIEW_AGY_ENABLED を廃止し、
    ~/.config/tidd_tools/config.json の "ai-review-agy" キーで管理する。
    キーなし / ファイルなし → True（default ON）。
    """
    return _get_backend_config_value("ai-review-agy", default=True)


def _codex_enabled() -> bool:
    """codex backend が有効かどうかを返す.

    Issue #2495: env var AI_REVIEW_CODEX_ENABLED を廃止し、
    ~/.config/tidd_tools/config.json の "ai-review-codex" キーで管理する。
    キーなし / ファイルなし → True（default ON）。
    """
    return _get_backend_config_value("ai-review-codex", default=True)


def _custom_enabled() -> bool:
    """custom backend が有効かどうかを返す（Issue #3116）.

    agy/codex とは逆に default **False**（opt-in）。config.json に
    "ai-review-custom": true が明示されない限り無効。
    """
    return _get_backend_config_value("ai-review-custom", default=False)


def _get_custom_backend_config() -> dict[str, str] | None:
    """config.json の "custom-backend" 設定を読み取る（Issue #3116）.

    ``base-url`` / ``model`` / ``api-key-env`` のいずれかが欠けている（または
    文字列以外・空文字列）場合は custom-backend 未定義として ``None`` を返す。
    ``display-name`` は省略可（設定されている場合のみ結果に含める）。
    """
    config = _read_backend_config("custom-backend", default_for_warn="未定義")
    if config is None:
        return None
    raw = config.get("custom-backend")
    if not isinstance(raw, dict):
        return None

    base_url = raw.get("base-url")
    model = raw.get("model")
    api_key_env = raw.get("api-key-env")
    if not (isinstance(base_url, str) and base_url):
        return None
    if not (isinstance(model, str) and model):
        return None
    if not (isinstance(api_key_env, str) and api_key_env):
        return None

    result = {"base-url": base_url, "model": model, "api-key-env": api_key_env}
    display_name = raw.get("display-name")
    if isinstance(display_name, str) and display_name:
        result["display-name"] = display_name
    return result


def validate_backend(backend: str) -> None:
    """``AI_REVIEW_BACKEND`` 値を検証する.

    Raises:
        ValueError: 未対応・無効化されたバックエンドが指定された場合
    """
    if backend not in {"auto", "agy", "agy-sonnet", "codex", "custom"}:
        raise ValueError(f"未対応のバックエンド: {backend}（対応値: auto / agy / agy-sonnet / codex / custom）")
    if backend in {"agy", "agy-sonnet"} and not _agy_enabled():
        raise ValueError(f"無効なバックエンド: {backend}（config.json の ai-review-agy=false で無効化されています）")
    if backend == "codex" and not _codex_enabled():
        raise ValueError(f"無効なバックエンド: {backend}（config.json の ai-review-codex=false で無効化されています）")
    if backend == "custom" and not _custom_enabled():
        raise ValueError(
            f"無効なバックエンド: {backend}"
            "（config.json の ai-review-custom=true を明示的に設定してください。default は false です）"
        )


def _try_codex(
    pr_num: str,
    repo: str,
    app_token: str,
    *,
    repo_root: Path | None,
    attempt: int,
    prev_issues_file: Path | None,
) -> BackendResult:
    """codex を実行して結果を返す。

    成功時は exit_code=0 の BackendResult を返す。
    失敗時は exit_code=3・failure_kind/failure_reason を設定した BackendResult を返す。
    Issue #2536: failure_kind で一時的/恒久的を区別するため None を返さず BackendResult を返す。
    """
    if not _codex_enabled():
        reason = "codex は無効（config.json の ai-review-codex=false）"
        print(f"==> {reason}。終了コード 3 を返します。", file=sys.stderr)
        # Issue #2541: 明示的な無効化は破損ではなく利用者の意思表示。
        # "permanent" ではなく "disabled" を使用し BACKEND_BROKEN / loop-error 発火を防ぐ。
        return BackendResult(
            output="",
            exit_code=3,
            backend_name="codex",
            failure_kind="disabled",
            failure_reason=reason,
        )
    codex_path = shutil.which("codex")
    if codex_path is None:
        reason = "codex コマンドが見つかりません"
        print(f"==> {reason}。終了コード 3 を返します。", file=sys.stderr)
        return BackendResult(
            output="",
            exit_code=3,
            backend_name="codex",
            failure_kind="permanent",
            failure_reason=reason,
        )
    if is_codex_stub(codex_path):
        reason = f"codex backend が stub（shell script）のため除外します（{codex_path}）"
        print(
            f"==> {reason}。"
            " fake APPROVE を防ぐため backend 利用不可として扱います。"
            " 実 codex CLI をインストールするか"
            " `tidd config disable ai-review-codex --repo または --machine` を実行してください。",
            file=sys.stderr,
        )
        return BackendResult(
            output="",
            exit_code=3,
            backend_name="codex",
            failure_kind="permanent",
            failure_reason=reason,
        )
    print("==> codex でレビューを試みます...", file=sys.stderr)
    result = run_codex_review(
        pr_num,
        repo,
        app_token,
        repo_root=repo_root,
        attempt=attempt,
        prev_issues_file=prev_issues_file,
    )
    if result.exit_code == 0:
        return result
    # Issue #3743: diff-too-large は全バックエンド共通の原因のため failure_kind を維持して
    # 返す（permanent に変換して BACKEND_BROKEN と誤帰属しないように）。
    if result.failure_kind == "diff-too-large":
        print(
            "==> codex: PR diff が20000行上限を超えています（DIFF_TOO_LARGE）。終了コード 3 を返します。",
            file=sys.stderr,
        )
        return result
    # Issue #3157: failure_reason に実際の stderr 内容を含め、GitHub Issue 上で真因を可視化する。
    # run_codex_review は非 zero 終了時に failure_reason に stderr を設定して返す。
    stderr_summary = result.failure_reason.strip() if result.failure_reason else ""
    reason = f"codex の実行に失敗しました（終了コード: {result.exit_code}）"
    if stderr_summary:
        reason = f"{reason}\n{stderr_summary}"
    print(
        f"==> codex の実行に失敗しました（終了コード: {result.exit_code}）。終了コード 3 を返します。",
        file=sys.stderr,
    )
    return BackendResult(
        output="",
        exit_code=3,
        backend_name="codex",
        failure_kind="permanent",
        failure_reason=reason,
    )


def _try_agy_step(
    label: str,
    backend_key: str,
    pr_num: str,
    repo: str,
    app_token: str,
    *,
    agy_model: str | None,
    repo_root: Path | None,
    attempt: int,
    prev_issues_file: Path | None,
    next_label: str,
) -> BackendResult | None:
    """agy(gemini) または agy(sonnet) を 1 ステップ実行する.

    成功（exit 0 かつクォータ超過していない）時のみ result を返す。
    クォータ超過は ``record_quota_exceeded`` を呼んだうえで None を返す。
    """
    if is_quota_skip_active(backend_key):
        print(
            f"==> {label} をスキップします（クォータ超過記録あり・リセット待機中）。{next_label} を試みます。",
            file=sys.stderr,
        )
        return None
    print(f"==> {label} でレビューを試みます...", file=sys.stderr)
    result = run_agy_review(
        pr_num,
        repo,
        app_token,
        agy_model=agy_model,
        repo_root=repo_root,
        attempt=attempt,
        prev_issues_file=prev_issues_file,
    )
    # Issue #3743: diff が20000行上限超過（DIFF_TOO_LARGE）は全バックエンド共通の原因のため、
    # 空出力・クォータ判定より先に検出して結果を伝播する（以降のチェーンを試さない）。
    if result.failure_kind == "diff-too-large":
        print(
            f"==> {label}: PR diff が20000行上限を超えています（DIFF_TOO_LARGE）。"
            f"{next_label} へのフォールバックは行わず終了コード 3 を返します。",
            file=sys.stderr,
        )
        return result
    # Issue #2535: 空出力はクォータとは別扱い（クォータキャッシュに記録しない）。
    # 空出力の原因は認証切れ・permission プロンプト・クラッシュ等多岐にわたるため、
    # クォータ（時間経過で回復する前提の状態）とは区別して loop-error に記録する。
    if not result.output.strip():
        print(
            f"==> {label}: agy が空レスポンスを返しました。{next_label} へフォールバックします。",
            file=sys.stderr,
        )
        record_loop_error(
            pr_num,
            f"ai-review:agy-empty-output:{backend_key}",
            f"{label} が空出力を返しました（認証切れ・クラッシュ等の可能性）。フォールバックします。",
            repo_root=repo_root,
        )
        return None
    # Issue #2535: VERDICT を含む正常レビューはクォータ判定の対象外にする。
    # 旧 sh scripts/ai-review.sh:727-729 のガードを復活させる。
    # quota.py の QUOTA_PATTERN はベア部分文字列マッチのため、
    # quota.py 自身を変更する PR の diff を含む正常レビュー本文が誤検知される。
    verdict_in_output = bool(parse_verdict(result.output))
    quota_hit = is_quota_exceeded(result.output) and not verdict_in_output
    if result.exit_code == 0 and not quota_hit:
        return result
    if result.exit_code != 0:
        print(
            f"==> {label} が非ゼロ終了（exit {result.exit_code}）。{next_label} へフォールバックします。",
            file=sys.stderr,
        )
    else:
        print(f"==> {label} クォータ超過。{next_label} へフォールバックします。", file=sys.stderr)
    if quota_hit:
        # Issue #2745: クォータエラーメッセージからリセット時間を解析して正確にスキップする。
        # 例: "Resets in 42h47m0s." → 154020 秒 → 42h47m でスキップ解除。
        # パターンが見つからない場合は従来の QUOTA_WINDOW_SECONDS（24時間）を使用。
        reset_secs = extract_reset_seconds(result.output)
        record_quota_exceeded(backend_key, reset_seconds=reset_secs)
    return None


def _merge_failure_kind(a: str, b: str) -> str:
    """2 つの failure_kind を合成し、より深刻な方を返す.

    Issue #2536: 「permanent が 1 つでもあれば permanent」ルール。
    Issue #2541: "disabled"（利用者による明示無効化）は "permanent" に昇格させない。

    値の優先度: "permanent" > "parse-failure" > "transient" > "disabled" > ""

    合成ルール:
    - permanent + any    → permanent（真の破損は最優先）
    - transient + disabled → transient（一時的回復可能 > 意図的無効）
    - disabled + disabled → disabled（両方意図的無効）
    - disabled + ""      → disabled
    """
    if a == "permanent" or b == "permanent":
        return "permanent"
    if a == "parse-failure" or b == "parse-failure":
        return "parse-failure"
    if a == "transient" or b == "transient":
        return "transient"
    if a == "disabled" or b == "disabled":
        return "disabled"
    return ""


def _run_agy_variant_element(
    name: str,
    pr_num: str,
    repo: str,
    app_token: str,
    *,
    repo_root: Path | None,
    attempt: int,
    prev_issues_file: Path | None,
    next_label: str,
) -> tuple[BackendResult | None, str, str]:
    """agy-gemini / agy-sonnet チェーン要素を 1 つ実行する.

    Issue #2536/#2541 の既存挙動（無効化は "disabled"・コマンド未検出は
    "permanent"・失敗時は "transient" 扱い）をチェーン要素単位に踏襲する。

    Returns:
        (result, kind, reason) — 成功時は ``(BackendResult, "", "")``。
        失敗/スキップ時は ``(None, failure_kind, failure_reason)``。
    """
    if not _agy_enabled():
        reason = "agy は無効（config.json の ai-review-agy=false）"
        print(f"==> {reason}。{next_label} へフォールバックします。", file=sys.stderr)
        return None, "disabled", reason

    if shutil.which("agy") is None:
        reason = "agy コマンドが見つかりません"
        print(f"==> {reason}。{next_label} へフォールバックします。", file=sys.stderr)
        return None, "permanent", reason

    if name == "agy-gemini":
        label = "agy(gemini)"
        agy_model = os.environ.get("AGY_MODEL") or None
    else:
        label = "agy(sonnet)"
        agy_model = "Claude Sonnet 4.6 (Thinking)"

    result = _try_agy_step(
        label,
        name,
        pr_num,
        repo,
        app_token,
        agy_model=agy_model,
        repo_root=repo_root,
        attempt=attempt,
        prev_issues_file=prev_issues_file,
        next_label=next_label,
    )
    if result is not None:
        # Issue #3743: diff-too-large は「成功」ではなく全バックエンド共通の失敗のため、
        # チェーンを短絡させる（_run_fallback_chain_from_agy が DIFF_TOO_LARGE で終了する）。
        if result.failure_kind == "diff-too-large":
            return None, "diff-too-large", result.failure_reason
        return result, "", ""
    # Issue #2536: 従来通り、クォータ超過も非ゼロ終了も等しく "transient" 扱いにする
    # （最終的な failure_kind は他要素との合成で決まる）。
    return None, "transient", ""


def _run_codex_element(
    pr_num: str,
    repo: str,
    app_token: str,
    *,
    repo_root: Path | None,
    attempt: int,
    prev_issues_file: Path | None,
) -> tuple[BackendResult | None, str, str]:
    """codex チェーン要素を実行する（既存 ``_try_codex`` の failure_kind をそのまま使う）."""
    result = _try_codex(
        pr_num,
        repo,
        app_token,
        repo_root=repo_root,
        attempt=attempt,
        prev_issues_file=prev_issues_file,
    )
    if result.exit_code == 0:
        return result, "", ""
    return None, result.failure_kind, result.failure_reason


def _run_custom_element(
    pr_num: str,
    repo: str,
    app_token: str,
    *,
    repo_root: Path | None,
    attempt: int,
    prev_issues_file: Path | None,
    next_label: str,
) -> tuple[BackendResult | None, str, str]:
    """custom チェーン要素を実行する（Issue #3117: chain に明示追加された場合のみ）."""
    if not _custom_enabled():
        reason = "custom は無効（config.json の ai-review-custom=false）"
        print(f"==> {reason}。{next_label} へフォールバックします。", file=sys.stderr)
        return None, "disabled", reason

    result = run_custom_review(
        pr_num,
        repo,
        app_token,
        repo_root=repo_root,
        attempt=attempt,
        prev_issues_file=prev_issues_file,
    )
    if result.exit_code == 0:
        return result, "", ""
    return None, result.failure_kind, result.failure_reason


def _run_chain_element(
    name: str,
    pr_num: str,
    repo: str,
    app_token: str,
    *,
    repo_root: Path | None,
    attempt: int,
    prev_issues_file: Path | None,
    next_label: str,
) -> tuple[BackendResult | None, str, str]:
    """1 チェーン要素（正準名）を実行するディスパッチャ."""
    if name in ("agy-gemini", "agy-sonnet"):
        return _run_agy_variant_element(
            name,
            pr_num,
            repo,
            app_token,
            repo_root=repo_root,
            attempt=attempt,
            prev_issues_file=prev_issues_file,
            next_label=next_label,
        )
    if name == "codex":
        return _run_codex_element(
            pr_num,
            repo,
            app_token,
            repo_root=repo_root,
            attempt=attempt,
            prev_issues_file=prev_issues_file,
        )
    # name == "custom"
    return _run_custom_element(
        pr_num,
        repo,
        app_token,
        repo_root=repo_root,
        attempt=attempt,
        prev_issues_file=prev_issues_file,
        next_label=next_label,
    )


def _run_fallback_chain_from_agy(
    pr_num: str,
    repo: str,
    app_token: str,
    *,
    repo_root: Path | None,
    attempt: int,
    prev_issues_file: Path | None,
) -> BackendResult:
    """config.json の ``ai-review-chain``（Issue #3117）が指定する順序でチェーンを実行する.

    デフォルト順序（キー未設定時）: agy(gemini) → agy(sonnet) → codex → 終了コード 3。
    全要素が失敗した場合は各要素の failure_kind を :func:`_merge_failure_kind` で合成する
    （Issue #2536: permanent > transient > disabled > "" の優先順位）。
    """
    order = _resolve_chain_order()
    merged_kind = ""
    reasons: list[str] = []

    for idx, name in enumerate(order):
        next_name = order[idx + 1] if idx + 1 < len(order) else None
        next_label = _CHAIN_DISPLAY_LABELS[next_name] if next_name else "終了"

        result, kind, reason = _run_chain_element(
            name,
            pr_num,
            repo,
            app_token,
            repo_root=repo_root,
            attempt=attempt,
            prev_issues_file=prev_issues_file,
            next_label=next_label,
        )
        if result is not None:
            if parse_verdict(result.output):
                return result
            label = _CHAIN_DISPLAY_LABELS[name]
            print(
                f"ERROR: {label} の VERDICT をパースできず、レビューを取得できませんでした。"
                f"次のバックエンドへフォールバックします。",
                file=sys.stderr,
            )
            print(f"--- {label} 出力 ---", file=sys.stderr)
            sys.stderr.write(result.output)
            if not result.output.endswith("\n"):
                sys.stderr.write("\n")
            merged_kind = _merge_failure_kind(merged_kind, "parse-failure")
            reasons.append(f"{label} の VERDICT パース失敗")
            continue

        # Issue #3743: diff-too-large は全バックエンド共通の PR 構造上の制約のため、
        # 残りのチェーンを試さず exit 3 + DIFF_TOO_LARGE で終了する。
        if kind == "diff-too-large":
            return BackendResult(
                output="",
                exit_code=3,
                backend_name="",
                failure_kind="diff-too-large",
                failure_reason=reason,
            )

        merged_kind = _merge_failure_kind(merged_kind, kind)
        if reason and (not reasons or reasons[-1] != reason):
            reasons.append(reason)

    return BackendResult(
        output="",
        exit_code=3,
        backend_name="",
        failure_kind=merged_kind,
        failure_reason="; ".join(reasons),
    )


def _normalize_agy_fixed_mode_result(
    result: BackendResult,
    *,
    backend_key: str,
) -> BackendResult:
    """固定モード（``AI_REVIEW_BACKEND=agy`` / ``agy-sonnet``）の実行結果を exit code 契約に正規化する（Issue #4082）.

    agy 固定モードはフォールバックチェーンを持たないため、バックエンド障害を
    ``exit 1``（= REQUEST_CHANGES と誤解釈され修正ループを招く）で返してはいけない。
    codex 固定モード・``run_custom_review()`` と同じ契約（失敗時は exit 3 +
    ``failure_kind``）へ揃える。

    - 成功（``parse_verdict`` が VERDICT を抽出できる）→ exit_code を 0 に正規化して返す
      （非ゼロ終了でもレビュー本文が得られていれば REQUEST_CHANGES として扱う）
    - ``diff-too-large``（#3743）→ そのまま返す（既に exit 3）
    - 失敗 → ``exit_code=3`` + ``failure_kind`` へ変換する
    """
    if parse_verdict(result.output):
        if result.exit_code == 0:
            return result
        return dataclasses.replace(result, exit_code=0)
    if result.failure_kind == "diff-too-large":
        return result

    if shutil.which("agy") is None:
        failure_kind = "permanent"
        reason = "agy コマンドが見つかりません（agy 固定モード）"
    elif is_quota_exceeded(result.output):
        failure_kind = "transient"
        reason = "agy クォータ超過（agy 固定モード）"
        record_quota_exceeded(
            backend_key,
            reset_seconds=extract_reset_seconds(result.output),
        )
    elif not result.output.strip():
        failure_kind = "transient"
        reason = "agy が空レスポンスを返しました（agy 固定モード）"
    elif result.exit_code != 0:
        failure_kind = "transient"
        reason = f"agy の実行に失敗しました（終了コード: {result.exit_code}）（agy 固定モード）"
    else:
        failure_kind = "parse-failure"
        reason = "agy の VERDICT をパースできませんでした（agy 固定モード）"

    detail = result.output.strip()
    if detail:
        reason = f"{reason}\n{detail[:500]}"
    print(f"==> {reason}。終了コード 3 を返します。", file=sys.stderr)
    return BackendResult(
        output="",
        exit_code=3,
        backend_name=result.backend_name or "agy",
        failure_kind=failure_kind,
        failure_reason=reason,
    )


def run_backend_review(
    pr_num: str,
    repo: str,
    app_token: str = "",
    *,
    backend: str | None = None,
    repo_root: Path | None = None,
    attempt: int = 1,
    prev_issues_file: Path | None = None,
) -> BackendResult:
    """``AI_REVIEW_BACKEND`` に応じてバックエンドを起動する.

    Raises:
        ValueError: 未対応バックエンドが指定された場合（``validate_backend`` 経由）
    """
    backend = backend or os.environ.get("AI_REVIEW_BACKEND", "auto")
    validate_backend(backend)

    if backend == "auto":
        return _run_fallback_chain_from_agy(
            pr_num,
            repo,
            app_token,
            repo_root=repo_root,
            attempt=attempt,
            prev_issues_file=prev_issues_file,
        )
    if backend in ("agy", "agy-sonnet"):
        agy_model = os.environ.get("AGY_MODEL") or None if backend == "agy" else "Claude Sonnet 4.6 (Thinking)"
        result = run_agy_review(
            pr_num,
            repo,
            app_token,
            agy_model=agy_model,
            repo_root=repo_root,
            attempt=attempt,
            prev_issues_file=prev_issues_file,
        )
        backend_key = "agy-gemini" if backend == "agy" else "agy-sonnet"
        return _normalize_agy_fixed_mode_result(result, backend_key=backend_key)
    if backend == "custom":
        return run_custom_review(
            pr_num,
            repo,
            app_token,
            repo_root=repo_root,
            attempt=attempt,
            prev_issues_file=prev_issues_file,
        )
    # codex 固定モード（Issue #2536: failure_kind を permanent で設定する）
    codex_path = shutil.which("codex")
    if codex_path is None:
        reason = "codex コマンドが見つかりません（codex 固定モード）"
        print(
            f"ERROR: {reason}。終了コード 3 を返します。",
            file=sys.stderr,
        )
        return BackendResult(
            output="",
            exit_code=3,
            backend_name="codex",
            failure_kind="permanent",
            failure_reason=reason,
        )
    if is_codex_stub(codex_path):
        reason = f"codex backend が stub（shell script）のため除外します（{codex_path}）（codex 固定モード）"
        print(
            f"ERROR: {reason}。"
            " fake APPROVE を防ぐため終了コード 3 を返します。"
            " 実 codex CLI をインストールするか"
            " `tidd config disable ai-review-codex --repo または --machine` を実行してください。",
            file=sys.stderr,
        )
        return BackendResult(
            output="",
            exit_code=3,
            backend_name="codex",
            failure_kind="permanent",
            failure_reason=reason,
        )
    result = run_codex_review(
        pr_num,
        repo,
        app_token,
        repo_root=repo_root,
        attempt=attempt,
        prev_issues_file=prev_issues_file,
    )
    if result.exit_code == 0:
        return result
    # Issue #3743: diff-too-large は全バックエンド共通の原因のため failure_kind を維持して
    # 返す（permanent に変換して BACKEND_BROKEN と誤帰属しないように。_try_codex と同様）。
    if result.failure_kind == "diff-too-large":
        print(
            "==> codex: PR diff が20000行上限を超えています（DIFF_TOO_LARGE）。"
            "終了コード 3 を返します。（codex 固定モード）",
            file=sys.stderr,
        )
        return result
    reason = f"codex の実行に失敗しました（終了コード: {result.exit_code}）（codex 固定モード）"
    print(
        f"==> {reason}。終了コード 3 を返します。",
        file=sys.stderr,
    )
    return BackendResult(
        output="",
        exit_code=3,
        backend_name="codex",
        failure_kind="permanent",
        failure_reason=reason,
    )
