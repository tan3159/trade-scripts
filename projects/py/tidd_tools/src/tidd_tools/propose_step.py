"""`tidd propose-step` サブコマンド（Issue #3118）.

Claude のトークン利用量上限（#3114）への対応として、TDD の RED / GREEN 各ステップの
「次の一手のコード提案」を外部 backend（agy / codex / custom）へ委譲する。実際の
Write/Edit・pytest 実行・commit は Claude Code 側に残す（#3114 A' 案）。

**使い方:**

.. code-block:: console

    tidd propose-step --phase red --issue 1234 --context tests/features/issue-1234.feature
    tidd propose-step --phase green --issue 1234 --test-output /tmp/pytest-fail.log

**設計判断:**

- backend の選択は ``~/.config/tidd_tools/config.json`` の ``impl-backend``
  キー（``agy`` / ``codex`` / ``custom``）で行う。未設定・未知値は exit 2。
- Issue 本文は ``gh issue view`` で取得し、必ず
  :func:`tidd_tools.sanitize.sanitize_untrusted_text` を通してからプロンプトに
  埋め込む（Issue #1845 のプロンプトインジェクション防御）。
- backend 呼び出しは ``tidd_tools.ai_review.backends`` の agy/codex/custom
  レビュー実装と同じ subprocess パターンを踏襲する（PR diff 取得・verdict 解析は
  行わない・レビュー用ロジックは呼ばない）。
- backend 応答が ``## proposal`` / ``### file:`` 形式に従わない場合は
  ``## raw-response`` 見出しの下にそのまま出力する（採否は呼び出し側が判断する）。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path

from tidd_tools import timing_log
from tidd_tools.ai_review.backends import (
    AGY_DEFAULT_MODEL,
    BACKEND_SUBPROCESS_TIMEOUT_SEC,
    CODEX_DEFAULT_MODEL,
    _get_backend_config_model,
    _get_custom_backend_config,
    extract_codex_text,
    is_codex_stub,
)
from tidd_tools.llm_client import LLMClientError, call_chat_completion
from tidd_tools.sanitize import sanitize_untrusted_text
from tidd_tools.shared import app_config, gh_client, paths
from tidd_tools.shared.errors import GhCommandError

_VALID_PHASES = ("red", "green", "refactor", "docs")
_VALID_BACKENDS = ("agy", "codex", "custom")

# --test-output が許可される phase のセット（Issue #3132）
_PHASES_WITH_TEST_OUTPUT = frozenset({"green"})

_PROPOSAL_HEADING_RE = re.compile(r"^##\s*proposal\b", re.MULTILINE)
_FILE_BLOCK_RE = re.compile(r"^###\s*file:", re.MULTILINE)

# `### file:` ブロックをパースするための正規表現（Issue #3133）
# グループ 1: 相対パス、グループ 2: コードブロック内容
_FILE_BLOCK_PARSE_RE = re.compile(
    r"^###\s*file:\s*(?P<path>\S+)\s*\n"
    r"```[^\n]*\n"
    r"(?P<content>.*?)"
    r"```",
    re.MULTILINE | re.DOTALL,
)

_PHASE_INSTRUCTIONS: dict[str, str] = {
    "red": (
        "あなたは TDD の RED フェーズを担当します。以下の GitHub Issue の "
        "`## 振る舞い` セクションに書かれた Scenario を **失敗（RED）** させる"
        "テストコードを提案してください。実装コードは含めないでください。"
    ),
    "green": (
        "あなたは TDD の GREEN フェーズを担当します。以下の `## test-output`"
        "（pytest の失敗出力）を **成功（GREEN）** させる実装コードを提案してください。"
        "テストコード自体は変更しないでください。"
    ),
    "refactor": (
        "あなたはリファクタリングを担当します。以下の GitHub Issue の `## やること` を参照し、"
        "既存のテストをすべて GREEN に保ったまま、コードを整理・改善する変更案を提案してください。"
        "テスト自体の変更は最小限に留め、振る舞いを変えない改善に集中してください。"
    ),
    "docs": (
        "あなたはドキュメント更新を担当します。以下の GitHub Issue の `## やること` を参照し、"
        "対象 Markdown ファイルの更新案を提案してください。"
        "既存の記述スタイルに合わせ、正確で読みやすい文章にしてください。"
    ),
}

_OUTPUT_FORMAT_INSTRUCTION = (
    "## 出力フォーマット（厳守）\n\n"
    "必ず以下の形式で提案を出力してください。\n\n"
    "## proposal\n"
    "### file: <リポジトリルート相対パス>\n"
    "```python\n"
    "<コード>\n"
    "```\n\n"
    "複数ファイルを提案する場合は `### file:` ブロックを繰り返してください。"
    "このフォーマットに従えない場合はそのまま自由記述で回答して構いません"
    "（呼び出し側が `## raw-response` として扱います）。"
)


# ── config 読み込み ────────────────────────────────────────────────────────────


def _is_impl_delegation_enabled() -> bool:
    """config.json の ``impl-delegation`` キーを読み取る（未設定は False・opt-in）.

    Issue #3569: リポジトリ root の ``.tidd/config.json`` に同キーがあれば
    マシン設定より優先する（優先順位: リポジトリ > マシン > default）。
    """
    config = app_config.read_effective_config()
    value = config.get("impl-delegation")
    return bool(value)


def _is_autoapply_enabled() -> bool:
    """config.json の ``impl-proposal-autoapply`` キーを読み取る（未設定は False・opt-in）.

    Issue #3133: autoapply 機能の config gate。default false により opt-in。
    Issue #3569: リポジトリ root の ``.tidd/config.json`` に同キーがあれば
    マシン設定より優先する（優先順位: リポジトリ > マシン > default）。
    """
    config = app_config.read_effective_config()
    value = config.get("impl-proposal-autoapply")
    return bool(value)


def _get_repo_root() -> Path:
    """git rev-parse --show-toplevel でリポジトリルートを取得する.

    Raises:
        RuntimeError: git コマンド失敗またはリポジトリでない場合。
    """
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"git コマンド実行失敗: {exc}") from exc
    if completed.returncode != 0:
        raise RuntimeError("git rev-parse --show-toplevel が失敗しました（git リポジトリではないかもしれません）")
    return Path(completed.stdout.strip())


def _apply_proposal(proposal_text: str, repo_root: Path) -> tuple[int, str]:
    """proposal テキストをパースしてファイルへ書き込む.

    ``## proposal`` / ``### file:`` 形式の提案を解析し、各ファイルブロックを
    リポジトリルート相対パスで書き込む。パス検証を行い、リポジトリルート外・
    絶対パス・``..`` 含みは exit 2 で拒否する（Issue #3133）。

    Returns:
        ``(exit_code, message)`` タプル。0 は成功、2 はパス検証エラー。
    """
    matches = list(_FILE_BLOCK_PARSE_RE.finditer(proposal_text))
    if not matches:
        return 2, "proposal 形式の `### file:` ブロックが見つかりません"

    # 全パス検証を書き込みより先に行い、一部だけ書き込まれる状態を防ぐ
    validated: list[tuple[Path, str]] = []
    for match in matches:
        raw_path = match.group("path")
        content = match.group("content")

        # 絶対パス・`..` 含みを拒否する
        if Path(raw_path).is_absolute() or ".." in Path(raw_path).parts:
            return 2, f"リポジトリルート外のパスは適用できません: {raw_path}"

        target = (repo_root / raw_path).resolve()
        try:
            target.relative_to(repo_root.resolve())
        except ValueError:
            return 2, f"リポジトリルート外のパスは適用できません: {raw_path}"

        validated.append((target, content))

    # 検証通過後にまとめて書き込む
    for target, content in validated:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    return 0, f"{len(validated)} 個のファイルを適用しました"


def _read_impl_backend() -> str | None:
    """config.json の ``impl-backend`` キーを読み取る（未設定・空文字は None）."""
    config = app_config.read_config()
    value = config.get("impl-backend")
    return value if isinstance(value, str) and value else None


# ── 入力収集 ───────────────────────────────────────────────────────────────────


def _fetch_issue_body(issue_number: str, repo: str | None) -> str:
    """gh issue view で Issue 本文を取得する.

    Raises:
        GhCommandError: gh コマンド実行に失敗した場合。
    """
    data = gh_client.issue_view(issue_number, repo=repo, fields=("body",))
    body = data.get("body")
    return body if isinstance(body, str) else ""


def _read_context_files(paths: Sequence[str]) -> list[tuple[str, str]]:
    """``--context`` で指定されたファイルを読み込む.

    Raises:
        OSError: いずれかのファイルの読み込みに失敗した場合。
    """
    return [(p, Path(p).read_text(encoding="utf-8")) for p in paths]


def _read_test_output(path: str | None) -> str | None:
    """``--test-output`` で指定されたファイルを読み込む（未指定なら None）.

    Raises:
        OSError: ファイルの読み込みに失敗した場合。
    """
    if path is None:
        return None
    return Path(path).read_text(encoding="utf-8")


def _build_prompt(
    phase: str,
    issue_number: str,
    issue_body: str,
    context_files: Sequence[tuple[str, str]],
    test_output: str | None,
) -> str:
    """backend へ渡す prompt を組み立てる（Issue #1845: Issue 本文はサニタイズ後に連結）."""
    sanitized_body = sanitize_untrusted_text(issue_body)
    parts = [
        _PHASE_INSTRUCTIONS[phase],
        _OUTPUT_FORMAT_INSTRUCTION,
        f"## Issue #{issue_number}\n\n{sanitized_body}",
    ]
    for context_path, content in context_files:
        parts.append(f"## context: {context_path}\n\n```\n{content}\n```")
    if test_output:
        parts.append(f"## test-output\n\n```\n{test_output}\n```")
    return "\n\n".join(parts)


def _format_output(raw_output: str) -> str:
    """backend 応答を整形する（規約フォーマットならそのまま、そうでなければ raw-response）."""
    if _PROPOSAL_HEADING_RE.search(raw_output) and _FILE_BLOCK_RE.search(raw_output):
        return raw_output
    return f"## raw-response\n\n{raw_output}"


# ── backend 呼び出し ───────────────────────────────────────────────────────────
# Issue #3118: ai_review/backends.py の run_agy_review / run_codex_review /
# run_custom_review と同じ subprocess パターンを踏襲する。PR diff 取得・
# verdict 解析等のレビュー専用ロジックは呼ばない。


def _call_agy(prompt: str) -> str:
    """agy CLI に prompt を渡し応答テキストを返す.

    Raises:
        RuntimeError: agy コマンド未検出・timeout・非 0 終了・空応答の場合。
    """
    if shutil.which("agy") is None:
        raise RuntimeError("agy コマンドが見つかりません")

    resolved_model = _get_backend_config_model("ai-review-agy-model", default=AGY_DEFAULT_MODEL)
    cmd = ["agy"]
    if os.environ.get("AI_REVIEW_AGY_ALLOW_PERMISSIONS_PROMPT") != "1":
        cmd.append("--dangerously-skip-permissions")
    if resolved_model:
        cmd.extend(["--model", resolved_model])

    try:
        completed = subprocess.run(  # noqa: S603 — argv はリスト構築済み
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=BACKEND_SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"agy 呼び出しが timeout しました（{BACKEND_SUBPROCESS_TIMEOUT_SEC}秒）") from exc
    except OSError as exc:
        raise RuntimeError(f"agy 呼び出し失敗（OSError: {exc}）") from exc

    if completed.returncode != 0:
        raise RuntimeError(f"agy が非 0 終了しました（exit code: {completed.returncode}）")

    output = (completed.stdout or "") + (completed.stderr or "")
    if not output.strip():
        raise RuntimeError("agy が空レスポンスを返しました")
    return output


def _resolve_volta_home() -> Path | None:
    """codex が Volta シムの場合に env へ引き継ぐ ``VOLTA_HOME`` を返す.

    Volta シムは実体バイナリの解決に ``$HOME``（または ``VOLTA_HOME``）配下の
    設定を参照する。``_call_codex`` が HOME を一時ディレクトリへ差し替えるため、
    Volta シムを実行できるよう ``VOLTA_HOME`` を明示する必要がある（Issue #4079）。

    Returns:
        ``VOLTA_HOME`` 環境変数の値。未設定なら既定の ``~/.volta``（存在する場合のみ）。
        どちらも無ければ ``None``。
    """
    volta_home = os.environ.get("VOLTA_HOME")
    if volta_home:
        return Path(volta_home)
    default = Path.home() / ".volta"
    if default.is_dir():
        return default
    return None


def _is_volta_shim(codex_path: str) -> bool:
    """codex バイナリが Volta シムかどうかを判定する.

    Volta シムは ``$VOLTA_HOME/bin``（未設定なら ``~/.volta/bin``）配下に置かれる
    ``volta-shim`` バイナリへの symlink。シム自身が Volta の bin ディレクトリ配下に
    あれば Volta シムとみなす。symlink の resolve 先は mise（aqua）等で別ディレクトリ
    に置かれた ``volta-shim`` 実体を指すことがあるため、resolve 先だけではなく
    シムが置かれている親ディレクトリ（＝ Volta bin 配下）も判定対象にする
    （Issue #4079）。
    """
    volta_home = _resolve_volta_home()
    if volta_home is None:
        return False
    volta_bin = (volta_home / "bin").resolve()
    try:
        candidate = Path(codex_path)
        if candidate.resolve().is_relative_to(volta_bin):
            return True
        return candidate.parent.resolve().is_relative_to(volta_bin)
    except (ValueError, OSError):
        return False


def _call_codex(prompt: str) -> str:
    """codex CLI に prompt を渡し応答テキストを返す.

    codex が Volta シムの場合は ``VOLTA_HOME`` を env へ引き継ぎ、HOME を一時
    ディレクトリへ差し替えても Volta が実体バイナリを解決できるようにする
    （Issue #4079）。

    Raises:
        RuntimeError: codex コマンド未検出・stub・timeout・非 0 終了・空応答の場合。
    """
    codex_path = shutil.which("codex")
    if codex_path is None:
        raise RuntimeError("codex コマンドが見つかりません")
    if is_codex_stub(codex_path):
        raise RuntimeError(f"codex backend が stub（shell script）のため利用できません（{codex_path}）")

    resolved_model = _get_backend_config_model("ai-review-codex-model", default=CODEX_DEFAULT_MODEL)
    home = Path.home()
    auth_src = home / ".codex" / "auth.json"

    with tempfile.TemporaryDirectory() as tmp_home_str:
        tmp_home = Path(tmp_home_str)
        (tmp_home / ".codex").mkdir(parents=True, exist_ok=True)
        if auth_src.is_file():
            with contextlib.suppress(OSError):
                (tmp_home / ".codex" / "auth.json").write_bytes(auth_src.read_bytes())

        env: dict[str, str] = {
            "HOME": str(tmp_home),
            "PATH": os.environ.get("PATH", ""),
            "TERM": os.environ.get("TERM", "xterm"),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        }
        # Issue #4079: codex が Volta シムの場合は、HOME 差し替え後も Volta が
        # 実体バイナリを解決できるよう VOLTA_HOME を引き継ぐ。
        if _is_volta_shim(codex_path):
            volta_home = _resolve_volta_home()
            if volta_home is not None:
                env["VOLTA_HOME"] = str(volta_home)
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
            "-",
        ]
        try:
            completed = subprocess.run(  # noqa: S603 — argv はリスト構築済み
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                check=False,
                timeout=BACKEND_SUBPROCESS_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"codex 呼び出しが timeout しました（{BACKEND_SUBPROCESS_TIMEOUT_SEC}秒）") from exc
        except OSError as exc:
            raise RuntimeError(f"codex 呼び出し失敗（OSError: {exc}）") from exc

    if completed.returncode != 0:
        raise RuntimeError(f"codex が非 0 終了しました（exit code: {completed.returncode}）")

    raw_output = "\n".join(filter(None, [completed.stdout or "", completed.stderr or ""]))
    text = extract_codex_text(raw_output) or raw_output
    if not text.strip():
        raise RuntimeError("codex が空レスポンスを返しました")
    return text


# ── backend キー解決（Issue #3696） ──────────────────────────────────────────
# `git worktree add` の fresh checkout には custom backend の API キー供給元で
# ある `.mise.toml`（gitignore・local）が存在しない。このため worktree 内で
# `tidd propose-step` を実行するとキーが解決できず、backend 呼び出しに進むと
# 応答待ちでハングする。環境変数 → 親リポジトリ（main worktree）の `.mise.toml`
# の順で解決し、解決できない場合は即時エラーにする。


def _find_main_worktree_path() -> Path | None:
    """`git worktree list --porcelain` の先頭（main worktree）パスを返す.

    worktree（fresh checkout）からでも親リポジトリの ``.mise.toml`` を参照する
    起点として使う。git コマンド失敗・タイムアウト・解析不能時は ``None``。
    """
    try:
        completed = subprocess.run(  # noqa: S603 — argv は固定リスト
            ["git", "worktree", "list", "--porcelain"],  # noqa: S607
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        if line.startswith("worktree "):
            candidate = line.split(" ", 1)[1].strip()
            if candidate:
                return Path(candidate)
    return None


def _resolve_custom_api_key(api_key_env: str) -> str | None:
    """custom backend の API キーを解決する（環境変数 → 親リポジトリ .mise.toml）.

    worktree（fresh checkout）では ``.mise.toml`` が存在せず環境変数が供給されない
    ため、親リポジトリ（main worktree）の ``.mise.toml`` の ``[env]`` をフォールバック
    に使う（Issue #3696）。解決できない場合は ``None``。

    Args:
        api_key_env: ``custom-backend.api-key-env`` が指す環境変数名。

    Returns:
        API キー文字列。環境変数・親リポジトリのどちらからも解決できない場合は ``None``。
    """
    direct = os.environ.get(api_key_env)
    if direct:
        return direct

    main_repo = _find_main_worktree_path()
    if main_repo is None:
        return None
    mise_toml = main_repo / ".mise.toml"
    if not mise_toml.is_file():
        return None
    try:
        with mise_toml.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError):
        return None
    env_table = data.get("env")
    if not isinstance(env_table, dict):
        return None
    value = env_table.get(api_key_env)
    return value if isinstance(value, str) and value else None


def _is_backend_resolvable(backend: str) -> bool:
    """impl-backend が現在の環境で実際に利用可能かどうかを返す.

    - ``agy``: agy CLI が PATH 上に存在する
    - ``codex``: codex CLI が PATH 上に存在し stub でない
    - ``custom``: ``custom-backend`` 設定があり、API キーが解決できる

    未知の backend は ``False``。
    """
    if backend == "agy":
        return shutil.which("agy") is not None
    if backend == "codex":
        codex_path = shutil.which("codex")
        return codex_path is not None and not is_codex_stub(codex_path)
    if backend == "custom":
        config = _get_custom_backend_config()
        if config is None:
            return False
        return _resolve_custom_api_key(config["api-key-env"]) is not None
    return False


def _call_custom(prompt: str) -> str:
    """custom backend（OpenAI 互換 API）に prompt を渡し応答テキストを返す.

    Raises:
        RuntimeError: custom-backend 未定義・api-key 未設定・API 呼び出し失敗の場合。
    """
    config = _get_custom_backend_config()
    if config is None:
        raise RuntimeError(
            "custom-backend が config.json に定義されていません（base-url / model / api-key-env が必要です）"
        )

    api_key = _resolve_custom_api_key(config["api-key-env"])
    if not api_key:
        raise RuntimeError(
            f"環境変数 {config['api-key-env']}（custom-backend.api-key-env が指す変数）が未設定です。"
            " 親リポジトリ（main worktree）の .mise.toml [env] からも解決できませんでした。"
            " .mise.toml を配置するか、tidd worktree-add の worktree-mise-stub-path を設定してください。"
        )

    try:
        return call_chat_completion(
            base_url=config["base-url"],
            model=config["model"],
            api_key=api_key,
            prompt=prompt,
            timeout=BACKEND_SUBPROCESS_TIMEOUT_SEC,
        )
    except LLMClientError as exc:
        raise RuntimeError(str(exc)) from exc


_BACKEND_CALLERS = {
    "agy": _call_agy,
    "codex": _call_codex,
    "custom": _call_custom,
}


# ── 呼び出しログ記録（Issue #3152） ───────────────────────────────────────────────
# impl-delegation の利用実態・成否をセッション後から検証できるよう、backend 呼び出し
# のたびに ~/.cache/ai-dev-handbook/propose-step/calls.jsonl へ 1 行追記する。


def _calls_log_path() -> Path:
    """呼び出しログの書き込み先パスを返す（テストでは monkeypatch で差し替える）."""
    return paths.cache_dir() / "propose-step" / "calls.jsonl"


def _record_call_log(
    *,
    issue_number: str,
    phase: str,
    backend: str,
    status: str,
    duration_sec: float,
) -> None:
    """propose-step の呼び出し記録を JSONL に 1 行追記する.

    書き込み失敗（disk full 等）は例外を握りつぶし stderr に WARN を出すのみで、
    呼び出し元の exit code 判定には影響させない（``bypass_audit.record_bypass``
    と同じ fail-open 方針）。
    """
    log_path = _calls_log_path()
    payload = {
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "issue": issue_number,
        "phase": phase,
        "backend": backend,
        "status": status,
        "duration_sec": round(duration_sec, 3),
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(
            f"propose-step: WARN: 呼び出しログの書き込みに失敗しました ({log_path}): {exc}",
            file=sys.stderr,
        )


# ── 中核ロジック ───────────────────────────────────────────────────────────────


def propose_step(
    phase: str,
    issue_number: str,
    *,
    repo: str | None = None,
    context_paths: Sequence[str] = (),
    test_output_path: str | None = None,
    apply: bool = False,
) -> tuple[int, str]:
    """RED/GREEN ステップのコード提案を外部 backend から取得する.

    Args:
        apply: True の場合、提案を直接ファイルへ書き込む（Issue #3133 autoapply モード）。
               config.json の ``impl-proposal-autoapply`` が true でないと exit 2。

    Returns:
        ``(exit_code, output)`` タプル。exit_code 0 は ``output`` に提案本文
        （または ``## raw-response``）、1/2 は ``output`` にエラーメッセージ。
    """
    if phase not in _VALID_PHASES:
        return 2, (f"ERROR: --phase は red/green/refactor/docs のいずれかを指定してください（指定値: {phase}）")

    issue_number = str(issue_number)
    if not issue_number.isdigit():
        return 2, f"ERROR: Issue 番号は数字である必要があります: {issue_number}"

    # --test-output は --phase green 専用（Issue #3132）
    if test_output_path is not None and phase not in _PHASES_WITH_TEST_OUTPUT:
        return 2, "--test-output は --phase green 専用です"

    # --apply は impl-proposal-autoapply が有効な場合のみ許可する（Issue #3133）
    if apply and not _is_autoapply_enabled():
        return 2, (
            "impl-proposal-autoapply が無効です。"
            "tidd config enable impl-proposal-autoapply --repo または --machine で有効化してください。"
        )

    if not _is_impl_delegation_enabled():
        return 2, (
            "ERROR: impl-delegation が無効です。"
            "tidd config enable impl-delegation --repo または --machine で有効化してください。"
        )

    backend = _read_impl_backend()
    if backend is None:
        return 2, (
            "ERROR: impl-backend が config.json に設定されていません。"
            "許可値（agy / codex / custom）のいずれかを設定してください。"
        )
    if backend not in _VALID_BACKENDS:
        return 2, (
            f"ERROR: impl-backend に未知の値が設定されています（指定値: {backend}）。許可値: agy / codex / custom"
        )

    try:
        issue_body = _fetch_issue_body(issue_number, repo)
    except GhCommandError as exc:
        return 1, f"ERROR: Issue #{issue_number} の本文取得に失敗しました: {exc}"

    try:
        context_files = _read_context_files(context_paths)
        test_output = _read_test_output(test_output_path)
    except OSError as exc:
        return 1, f"ERROR: ファイルの読み込みに失敗しました: {exc}"

    prompt = _build_prompt(phase, issue_number, issue_body, context_files, test_output)

    call_started = time.monotonic()
    try:
        raw_output = _BACKEND_CALLERS[backend](prompt)
    except RuntimeError as exc:
        _record_call_log(
            issue_number=issue_number,
            phase=phase,
            backend=backend,
            status="error",
            duration_sec=time.monotonic() - call_started,
        )
        return 1, f"ERROR: {exc}"

    _record_call_log(
        issue_number=issue_number,
        phase=phase,
        backend=backend,
        status="ok",
        duration_sec=time.monotonic() - call_started,
    )

    # Issue #3155: 委譲実行の事実を統一イベントログへ記録する（merge-summary が
    # 実装フェーズの主担当を backend 名へ解決するための第一ソース）。
    timing_log.record_event_safe(
        f"issue-{issue_number}",
        "impl-delegation-used",
        "point",
        "propose-step",
        meta={"backend": backend, "phase": phase},
    )

    formatted = _format_output(raw_output)

    # --apply モード: 提案をファイルへ直接書き込む（Issue #3133）
    if apply:
        # ## raw-response フォールバック時は apply 不可（proposal 形式でない）
        if formatted.startswith("## raw-response"):
            # noqa: E501 の回避: メッセージを変数に分離する
            _err = "proposal 形式でないため適用できません（`## raw-response` フォールバックは --apply 非対応）"
            return 1, _err

        try:
            repo_root = _get_repo_root()
        except RuntimeError as exc:
            return 1, f"ERROR: リポジトリルートの取得に失敗しました: {exc}"

        apply_exit, apply_msg = _apply_proposal(raw_output, repo_root)
        if apply_exit != 0:
            return apply_exit, apply_msg
        return 0, apply_msg

    return 0, formatted


# ── CLI ────────────────────────────────────────────────────────────────────────


def run_cli(args: argparse.Namespace) -> int:
    repo = os.environ.get("REPO")
    apply = getattr(args, "apply", False)
    exit_code, output = propose_step(
        args.phase,
        str(args.issue),
        repo=repo,
        context_paths=args.context or [],
        test_output_path=args.test_output,
        apply=apply,
    )
    if exit_code == 0:
        print(output)
    else:
        print(output, file=sys.stderr)
    return exit_code


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "propose-step",
        help="TDD ステップ単位のコード提案を外部 backend へ委譲する（Issue #3118）",
        description=__doc__,
    )
    parser.add_argument(
        "--phase",
        required=True,
        choices=list(_VALID_PHASES),
        help="フェーズ（red: テスト提案 / green: 実装提案 / refactor: 整理変更案 / docs: Markdown 更新案）",
    )
    parser.add_argument("--issue", required=True, help="対象 Issue 番号（例: 1234）")
    parser.add_argument(
        "--context",
        action="append",
        default=[],
        help="提案に必要な既存ファイル（.feature・関連ソース等）。複数指定可",
    )
    parser.add_argument(
        "--test-output",
        default=None,
        help="pytest の失敗出力ファイル（--phase green で使用）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "提案を直接ファイルへ書き込む（autoapply モード・Issue #3133）。"
            "config.json の impl-proposal-autoapply が true の場合のみ有効。"
        ),
    )
    parser.set_defaults(func=run_cli)
