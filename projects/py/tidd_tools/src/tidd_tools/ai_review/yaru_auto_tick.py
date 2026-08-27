"""Issue #1534: ai-review APPROVE 時に closes #N の やること を evidence-based で auto-tick.

**設計原則:**
    Hallucination Safety First. LLM が「delivered」と主張しても、
    Python 側で evidence.quote が PR diff に literal に存在することを再検証してから
    tick する。1 つでも欠ければ unchecked のまま放置（安全側 default）。

**フロー:**
    1. config.json の ``"yaru-auto-tick"``（優先）または env
       ``AI_REVIEW_YARU_AUTO_TICK``（deprecated）から mode を判定 (off / enabled / dry-run)
    2. off なら副作用ゼロで return
    3. PR body から ``closes #N`` を抽出
    4. 各 Issue について:
       a. Issue body の ``## やること`` から未チェック項目を抽出
          （``[手動]`` / ``[AI確認]`` / ``[AI確認-post-merge]`` prefix は除外）
       b. ``.claude/agents/yaru-verifier.md`` を system prompt として agy を呼び出し
          verdict / confidence / evidence を含む JSON を得る
       c. 各 result について ``_should_tick()`` の AND 条件を全て満たすか検証
       d. tick 対象のみ Issue body を更新（dry-run では実 update せず）
       e. audit log を ``shared/paths.cache_dir() / "yaru-auto-tick" / <PR>.jsonl`` に append

**関連:**
    - Issue #1534 — 本モジュールの設計（ハルシネーション対策 8 項目）
    - ``.claude/agents/yaru-verifier.md`` — subagent persona 定義
    - ``.claude/hooks/require-yaru-consistency.py`` — 最終防波堤 (Issue #1533)
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess  # noqa: S404 — agy CLI subprocess は既存 backends.py と同パターン
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from tidd_tools.ai_review.backends import extract_codex_text as _extract_codex_text
from tidd_tools.shared import app_config, paths
from tidd_tools.shared import gh_client as gh
from tidd_tools.shared.errors import DiffTooLargeError
from tidd_tools.shared.issue_body import (
    _UNCHECKED_ITEM_RE,
    extract_closes_issues,
    extract_section,
    iter_unchecked_items,
)
from tidd_tools.subagent_routing_resolver import resolve as resolve_subagent_routing
from tidd_tools.tick_evidence import format_evidence_comment

# ── 定数（Issue #2943: closes/セクション/未チェック項目抽出は shared/issue_body.py へ集約） ──

_EXCLUDE_PREFIX_RE = re.compile(r"^\s*\[(手動|AI確認(-post-merge)?)\]")
# Issue #2376: 見送り明記項目の除外パターン（「本 Issue では対応しない」「見送り」等）
_WAIVE_PATTERN_RE = re.compile(r"本\s*Issue\s*では?(対応しない|見送り)|見送り")

_VERIFIER_MD_PATH = ".claude/agents/yaru-verifier.md"
_AGY_TIMEOUT_SEC = 120
_CODEX_TIMEOUT_SEC = 120
_CLAUDE_TIMEOUT_SEC = 120


# ── 型 ───────────────────────────────────────────────────────────────────────


class Mode(StrEnum):
    OFF = "off"
    ENABLED = "enabled"
    DRY_RUN = "dry-run"


@dataclass
class TickResult:
    issue_number: int
    ticked_items: list[str] = field(default_factory=list)
    skipped_items: list[dict[str, Any]] = field(default_factory=list)
    skipped_hallucination: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunResult:
    mode: Mode
    tick_results: list[TickResult] = field(default_factory=list)
    subagent_available: bool = True


# ── mode 解決（config.json 優先・env var は deprecated）──────────────


def _hooks_config_path_from_env(env: dict[str, str]) -> str | None:
    """env dict の HOME / XDG_CONFIG_HOME / APPDATA から config.json パスを解決する.

    Issue #2947: パス解決ロジックは shared/app_config.py の resolve_config_path()
    （os.environ ではなく **渡された env dict のみ** を参照し、read_mode({}) 等の旧テストの
    決定性を維持する版）に集約。解決できなければ None（config なし扱い）。
    """
    resolved = app_config.resolve_config_path(env)
    return str(resolved) if resolved is not None else None


def _read_config_mode(env: dict[str, str]) -> Mode | None:
    """config.json の ``"yaru-auto-tick"`` 値から Mode を返す。未設定なら None.

    hook_io.get_hook_config("yaru-auto-tick") と同じ値解釈:
    ``"enabled"``/true → ENABLED、``"dry-run"`` → DRY_RUN、false → OFF、
    未知値 → WARN + OFF（安全側）。
    """
    path = _hooks_config_path_from_env(env)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(config, dict) or "yaru-auto-tick" not in config:
        return None
    value = config["yaru-auto-tick"]
    if value is True or value == "enabled":
        return Mode.ENABLED
    if value == "dry-run":
        return Mode.DRY_RUN
    if value is False:
        return Mode.OFF
    _log_stderr(f"WARN: config.json の 'yaru-auto-tick' に未知の値 {value!r} が設定されています。off として扱います。")
    return Mode.OFF


def read_mode(env: dict[str, str] | None = None) -> Mode:
    """config.json（優先）と env var（deprecated）から Mode を返す（Issue #1994）.

    優先順位:
        1. config.json の ``"yaru-auto-tick"``
        2. env var ``AI_REVIEW_YARU_AUTO_TICK``（deprecated・後方互換のみ）:
           ``"1"`` → ENABLED、``"dry-run"`` → DRY_RUN
        3. どちらも未設定・未知値 → OFF（安全側 default）
    """
    if env is None:
        env = dict(os.environ)
    config_mode = _read_config_mode(env)
    if config_mode is not None:
        return config_mode
    val = env.get("AI_REVIEW_YARU_AUTO_TICK", "")
    if val == "1":
        return Mode.ENABLED
    if val == "dry-run":
        return Mode.DRY_RUN
    return Mode.OFF


# ── パース系（Issue #2943: closes/セクション抽出は shared/issue_body.py へ集約） ──────────


def _item_hash(item_text: str) -> str:
    """item_text の sha256 先頭 8 文字をハッシュキーとして返す（Issue #2022）."""
    return hashlib.sha256(item_text.encode()).hexdigest()[:8]


def parse_yaru_items(issue_body: str) -> dict[str, str]:
    """Issue body の ``## やること`` セクションから **未チェック** 項目を抽出する.

    ``[手動]`` / ``[AI確認]`` / ``[AI確認-post-merge]`` prefix が付いた項目は
    auto-tick 対象外なので除外する。

    Returns:
        ``{item_id: item_text}`` の辞書。item_id は ``sha256(item_text)[:8]``（Issue #2022）。
        LLM は item_id のみを返すため文字列品質に依存せず照合できる。
    """
    if not issue_body:
        return {}
    section = extract_section(issue_body, "やること")
    if section is None:
        return {}

    items: dict[str, str] = {}
    for unchecked in iter_unchecked_items(section):
        text = unchecked.text
        if _EXCLUDE_PREFIX_RE.match(text):
            continue
        # Issue #2376: 見送り明記項目は auto-tick 対象外（merge gate でも除外）
        if _WAIVE_PATTERN_RE.search(text):
            continue
        items[_item_hash(text)] = text
    return items


def verify_evidence_quote(quote: str, pr_diff: str) -> bool:
    """LLM 自己申告の ``quote`` が PR diff の追加行（+ 行）に literal に存在するか.

    ハルシネーション対策の核心。LLM が「delivered」と主張しても
    diff に存在しない引用なら False を返し、Python 側で tick を拒否する。
    削除行（- 行）は「実装済み」ではないので対象外。
    """
    if not quote or not pr_diff:
        return False
    for raw_line in pr_diff.splitlines():
        if not raw_line.startswith("+"):
            continue
        if raw_line.startswith("+++"):  # diff header
            continue
        # 先頭 + を剥がして literal 比較
        added = raw_line[1:]
        if quote in added:
            return True
    return False


def verify_evidence_quote_deleted(quote: str, pr_diff: str) -> bool:
    """LLM 自己申告の ``quote`` が PR diff の削除行（- 行）に literal に存在するか.

    Issue #2376: 「撤去型」やること項目（「X を削除する」等）のエビデンス検証。
    削除行（- 行）に quote が含まれれば撤去が確認できる。

    ``---`` ヘッダ行は除外。
    """
    if not quote or not pr_diff:
        return False
    for raw_line in pr_diff.splitlines():
        if not raw_line.startswith("-"):
            continue
        if raw_line.startswith("---"):  # diff header
            continue
        # 先頭 - を剥がして literal 比較
        removed = raw_line[1:]
        if quote in removed:
            return True
    return False


# ── Issue body 更新 ─────────────────────────────────────────────────────────


def apply_tick(issue_body: str, item_text: str) -> str:
    """``- [ ] <item_text>`` 行を ``- [x] <item_text>`` に置換する.

    line-by-line で ``parse_yaru_items`` と同じ regex (``_UNCHECKED_ITEM_RE``) を
    使い、item_text 部分だけの完全一致で判定する。leading whitespace は保持されるため
    インデントされた nested checkbox も正しく tick される。
    regex メタキャラ・バッククォート・特殊文字を含む item_text でも安全。
    既に checked / 存在しない項目は unchanged。
    """
    if not item_text:
        return issue_body
    out_lines: list[str] = []
    for line in issue_body.splitlines(keepends=True):
        stripped = line.rstrip("\n").rstrip("\r")
        m = _UNCHECKED_ITEM_RE.match(stripped)
        if m and m.group(1) == item_text:
            # leading whitespace + "- [x] " + item_text + 元の末尾 (改行含む)
            newline = line[len(stripped) :]
            indent_end = stripped.index("-")
            indent = stripped[:indent_end]
            out_lines.append(f"{indent}- [x] {item_text}{newline}")
        else:
            out_lines.append(line)
    return "".join(out_lines)


# ── audit log ────────────────────────────────────────────────────────────────


def _audit_log_path(pr_num: str) -> Path:
    # Issue #2950: Path.home() / ".cache" ハードコードを shared/paths.cache_dir() へ統一
    return paths.cache_dir() / "yaru-auto-tick" / f"{pr_num}.jsonl"


def write_audit_log(pr_num: str, records: list[dict[str, Any]]) -> None:
    """audit log を ``shared/paths.cache_dir() / "yaru-auto-tick" / <PR>.jsonl`` に append."""
    path = _audit_log_path(pr_num)
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            payload = dict(rec)
            payload.setdefault("ts", ts)
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


# ── LLM 呼び出し (agy CLI) ─────────────────────────────────────────────────


def _find_repo_root(start: Path | None = None) -> Path | None:
    """git repo root を上方向に探索."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _load_verifier_persona() -> str | None:
    """``.claude/agents/yaru-verifier.md`` の中身を返す。読み取り失敗時 None（stderr に理由を出力）.

    Issue #3763: 従来は失敗理由が呼び出し元の汎用メッセージ
    （"yaru-verifier subagent unavailable"）でしか通知されず、consumer が
    「copier 配布漏れ」に気付けなかった。探索したパス・見つからない旨を明示する。
    """
    root = _find_repo_root()
    if root is None:
        _log_stderr(
            "==> yaru-verifier subagent unavailable: git repo root が見つからないため "
            f"{_VERIFIER_MD_PATH} を探索できません"
        )
        return None
    path = root / _VERIFIER_MD_PATH
    if not path.is_file():
        _log_stderr(f"==> yaru-verifier subagent unavailable: {path} が見つかりません")
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _log_stderr(f"==> yaru-verifier subagent unavailable: {path} の読み取りに失敗しました ({exc})")
        return None


_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_block(output: str) -> dict[str, Any] | None:
    """agy 出力の末尾 ``` json ...``` ブロックを抽出."""
    matches = list(_JSON_BLOCK_RE.finditer(output))
    if not matches:
        return None
    try:
        parsed: dict[str, Any] = json.loads(matches[-1].group(1))
        return parsed
    except (json.JSONDecodeError, ValueError):
        return None


def _routing_can_launch(routing: dict[str, Any], native_mechanism: str) -> bool:
    """routing が指定 mechanism と利用可能な agent_type を返したか."""
    agent_type = routing.get("agent_type")
    return routing.get("native_mechanism") == native_mechanism and isinstance(agent_type, str) and bool(agent_type)


def _codex_spawn_prompt(prompt: str, routing: dict[str, Any]) -> str:
    """Codex の native spawn_agent 呼び出しへ routing と検証 prompt を渡す."""
    agent_type = json.dumps(routing["agent_type"], ensure_ascii=False)
    message = json.dumps(prompt, ensure_ascii=False)
    model_argument = ""
    if routing.get("model") is not None:
        model_argument = f", model={json.dumps(routing['model'], ensure_ascii=False)}"
    return (
        "spawn_agent をちょうど1回呼び出し、完了を待って subagent の最終応答だけをそのまま返してください。\n"
        f"spawn_agent(agent_type={agent_type}, task_name={agent_type}, message={message}, "
        f'fork_turns="none"{model_argument})'
    )


# Issue #2963: _extract_codex_text は backends.py の公開 API（extract_codex_text）
# へ一本化した（本モジュールの独自実装は廃止）。上記 import で同一関数を参照する。


def _call_verifier_codex(prompt: str) -> list[dict[str, Any]] | None:
    """codex で yaru-verifier prompt を実行する。失敗時 None。

    codex exec は orchestrator として起動し、resolver が返した custom agent へ
    native ``spawn_agent`` で委譲する。
    """
    routing = resolve_subagent_routing("codex", "yaru-verifier")
    if routing is None or not _routing_can_launch(routing, "spawn_agent"):
        return None
    codex_path = shutil.which("codex")
    if codex_path is None:
        return None

    home = Path.home()
    auth_src = home / ".codex" / "auth.json"

    try:
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
            cmd = [
                codex_path,
                "exec",
                "--json",
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--ignore-rules",
            ]
            if routing["model"] is not None:
                cmd.extend(["--model", routing["model"]])
            cmd.append("-")
            completed = subprocess.run(  # noqa: S603
                cmd,
                input=_codex_spawn_prompt(prompt, routing),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                check=False,
                timeout=_CODEX_TIMEOUT_SEC,
            )
    except (OSError, subprocess.SubprocessError, FileNotFoundError):
        return None

    raw_output = (completed.stdout or "") + (completed.stderr or "")
    text = _extract_codex_text(raw_output) or raw_output
    if not text.strip():
        return None
    parsed = _extract_json_block(text)
    if parsed is None:
        return None
    results = parsed.get("results")
    if not isinstance(results, list):
        return None
    return results


def _call_verifier_claude(prompt: str) -> list[dict[str, Any]] | None:
    """claude CLI で yaru-verifier prompt を実行する。失敗時 None（Issue #1886）."""
    routing = resolve_subagent_routing("claude_code", "yaru-verifier")
    if routing is None or not _routing_can_launch(routing, "Agent tool"):
        return None
    claude_path = shutil.which("claude")
    if claude_path is None:
        return None
    try:
        cmd = [
            claude_path,
            "--print",
            "--output-format",
            "text",
            # prompt は PR diff（非信頼入力）を含むため、実行系 tool の禁止 +
            # settings 非ロード（permission allow ルール排除）で
            # prompt injection の実行経路を遮断する。
            # --disallowedTools は旧バージョン CLI でも有効な長期サポートフラグ
            "--disallowedTools",
            "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,Agent",
            "--setting-sources",
            "",
            "--agent",
            routing["agent_type"],
        ]
        if routing["model"] is not None:
            cmd.extend(["--model", routing["model"]])
        completed = subprocess.run(  # noqa: S603
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_CLAUDE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or "") + (completed.stderr or "")
    parsed = _extract_json_block(output)
    if parsed is None:
        return None
    results = parsed.get("results")
    if not isinstance(results, list):
        return None
    return results


def call_yaru_verifier(items: dict[str, str], pr_diff: str) -> list[dict[str, Any]] | None:
    """agy CLI に yaru-verifier persona を渡して JSON を得る。
    agy クォータ枯渇時は codex → claude CLI の順にフォールバックする（Issue #1868・#1886）。

    Args:
        items: ``{item_id: item_text}`` の辞書（Issue #2022 hash キー方式）。
               LLM には ``{"id": item_id, "text": item_text}`` 形式で渡す。
        pr_diff: PR の diff 文字列。

    Returns:
        results 配列。全バックエンド利用不可の場合は None。
    """
    from tidd_tools.ai_review.quota import (
        is_quota_exceeded,
        is_quota_skip_active,
        record_quota_exceeded,
    )

    if not items:
        return []

    persona = _load_verifier_persona()
    if persona is None:
        return None

    items_block = "\n".join(
        json.dumps({"id": item_id, "text": item_text}, ensure_ascii=False) for item_id, item_text in items.items()
    )
    prompt = (
        f"{persona}\n\n"
        f"---\n"
        f"## 対象の やること 項目 (id は改変禁止・そのまま item_id として返すこと)\n\n"
        f"{items_block}\n\n"
        f"## PR diff\n\n"
        f"```diff\n{pr_diff}\n```\n"
    )

    # agy フェーズ: 24 時間以内にクォータ枯渇済みでなく、かつコマンドが存在する場合のみ実行
    quota_active = is_quota_skip_active("agy-gemini")
    if quota_active:
        _log_stderr("yaru-verifier: agy クォータ枯渇 → codex フォールバック")
    elif shutil.which("agy") is not None:
        try:
            completed = subprocess.run(  # noqa: S603
                ["agy", "--prompt", prompt],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_AGY_TIMEOUT_SEC,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            _log_stderr("yaru-verifier: agy クォータ枯渇 → codex フォールバック")
        else:
            output = (completed.stdout or "") + (completed.stderr or "")
            if is_quota_exceeded(output):
                record_quota_exceeded("agy-gemini")
                _log_stderr("yaru-verifier: agy クォータ枯渇 → codex フォールバック")
            else:
                parsed = _extract_json_block(output)
                if parsed is not None:
                    results = parsed.get("results")
                    if isinstance(results, list):
                        _log_stderr(f"yaru-verifier: agy から {len(items)} 件の項目を検証しました")
                        return results
                # agy 成功だが JSON parse 失敗 → codex フォールバック
                _log_stderr("yaru-verifier: agy クォータ枯渇 → codex フォールバック")

    # codex フォールバック
    codex_results = _call_verifier_codex(prompt)
    if codex_results is not None:
        return codex_results

    # claude CLI フォールバック（Issue #1886）
    _log_stderr("yaru-verifier: codex 利用不可 → claude フォールバック")
    claude_results = _call_verifier_claude(prompt)
    if claude_results is not None:
        _log_stderr(f"yaru-verifier: claude から {len(items)} 件の項目を検証しました")
        return claude_results

    _log_stderr("yaru-verifier: 全バックエンド利用不可のためスキップします")
    return None


# ── tick 判定 (Python 側の再検証) ───────────────────────────────────────────


def _should_tick(entry: dict[str, Any], pr_diff: str) -> bool:
    """LLM 応答を Python 側で再検証。AND 条件を全部満たすなら True.

    条件:
        1. verdict == "delivered"
        2. confidence == "high"
        3. evidence に以下のいずれかが最低 1 件:
           - ``file_line`` 型: quote が PR diff の追加行（+ 行）に literal に存在する
           - ``deleted_line`` 型（Issue #2376）: quote が PR diff の削除行（- 行）に literal に存在する
           - ``absence`` 型（Issue #2376）: literal quote 検証不要（grep 0 件等）
    """
    if entry.get("verdict") != "delivered":
        return False
    if entry.get("confidence") != "high":
        return False
    evidence = entry.get("evidence")
    if not isinstance(evidence, list):
        return False
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        ev_type = ev.get("type")
        if ev_type == "file_line":
            quote = ev.get("quote", "")
            if verify_evidence_quote(quote, pr_diff):
                return True
        elif ev_type == "deleted_line":
            # Issue #2376: 撤去型エビデンス（- 行）
            quote = ev.get("quote", "")
            if verify_evidence_quote_deleted(quote, pr_diff):
                return True
        elif ev_type == "absence":
            # Issue #2376: 0 件エビデンス（grep 等の検索結果が 0 件であることを確認）
            # literal quote 検証不要: LLM の判断（delivered + high + absence）を信頼する
            return True
    return False


# ── PR diff fetch ───────────────────────────────────────────────────────────


def _fetch_pr_diff(pr_num: str, repo: str) -> str:
    """``gh_client.pr_diff()``（#2940）で PR diff を取得する。失敗時空文字列（Issue #2963）."""
    try:
        return gh.pr_diff(pr_num, repo=repo)
    except DiffTooLargeError as exc:
        # Issue #3743: diff 20000行上限超過（DIFF_TOO_LARGE）は対象外。空文字列で graceful skip
        # し、外側の except Exception に頼らず専用メッセージを出す（#3746 レビュー指摘）。
        _log_stderr(
            f"==> yaru-auto-tick: PR diff が20000行上限を超えています（DIFF_TOO_LARGE）。スキップします（{exc}）"
        )
        return ""


# ── メインエントリ ──────────────────────────────────────────────────────────


def _log_stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def run(pr_num: str, repo: str, *, env: dict[str, str] | None = None) -> RunResult:
    """PR APPROVE 時に closes #N の やること を evidence-based で auto-tick する.

    env が None の場合は ``os.environ`` を使用。副作用は以下に限定:
        - Issue body 更新 (mode=ENABLED 時のみ)
        - audit log 追記 (mode != OFF の時)
        - stderr へのメッセージ出力
    """
    mode = read_mode(env)
    if mode == Mode.OFF:
        return RunResult(mode=mode)

    try:
        pr_data = gh.pr_view(pr_num, repo=repo, fields=("body",))
    except Exception:  # noqa: BLE001 — fail-open: env=off 相当で戻る
        _log_stderr("==> yaru-auto-tick: PR body 取得失敗のため skip")
        return RunResult(mode=mode, subagent_available=False)
    pr_body = str(pr_data.get("body") or "")

    issue_numbers = extract_closes_issues(pr_body)
    if not issue_numbers:
        return RunResult(mode=mode)

    pr_diff = _fetch_pr_diff(pr_num, repo)

    result = RunResult(mode=mode)
    for issue_num in issue_numbers:
        tick_result = _process_issue(
            issue_num=issue_num,
            pr_num=pr_num,
            repo=repo,
            pr_diff=pr_diff,
            mode=mode,
            subagent_available_ref=result,
        )
        if tick_result is not None:
            result.tick_results.append(tick_result)
    return result


def _format_evidence_cell(evidence: Any) -> str:
    """検証済み file_line evidence を ``path:line`` — ``quote`` 形式の 1 セルに整形する."""
    if not isinstance(evidence, list):
        return ""
    parts: list[str] = []
    for ev in evidence:
        if not isinstance(ev, dict) or ev.get("type") != "file_line":
            continue
        parts.append(f"`{ev.get('path')}:{ev.get('line')}` — `{ev.get('quote')}`")
    return "<br>".join(parts)


def _post_evidence_comment(*, issue_num: int, pr_num: str, repo: str, evidence_rows: list[tuple[str, str]]) -> None:
    """tick 済み項目の evidence 表コメントを Issue に 1 件投稿する（失敗は fail-open）."""
    comment = format_evidence_comment(evidence_rows) + f"\n_yaru-auto-tick による自動 tick（PR #{pr_num}）_\n"
    try:
        gh.issue_comment(issue_num, repo, comment)
    except Exception as exc:  # noqa: BLE001 — コメント投稿失敗は tick 結果に影響させない
        _log_stderr(f"==> yaru-auto-tick: Issue #{issue_num} への evidence コメント投稿失敗: {exc}")


@dataclass
class _EntryOutcome:
    """`_process_verifier_entry` の処理結果（audit record + tick 反映情報）."""

    audit_record: dict[str, Any]
    ticked: bool
    item_text: str
    evidence: Any = None


def _skip_outcome(
    entry: dict[str, Any],
    *,
    item_id: str,
    item_text: str,
    issue_num: int,
    tick_result: TickResult,
) -> _EntryOutcome:
    """`_should_tick` が False のエントリを詳細分類し audit record を組み立てる."""
    if entry.get("verdict") != "delivered" or entry.get("confidence") != "high":
        reason = f"verdict={entry.get('verdict')} confidence={entry.get('confidence')}"
        tick_result.skipped_items.append({"item": item_text, "reason": reason})
        action = "skipped"
        _log_stderr(f"==> auto-tick skipped: {reason}")
    else:
        # evidence 不足 or hallucination
        reason = "evidence quote not found in PR diff"
        tick_result.skipped_hallucination.append({"item": item_text, "reason": reason})
        action = "skipped_hallucination"
        _log_stderr("==> auto-tick skipped: evidence quote not found in PR diff, refusing to tick")
    return _EntryOutcome(
        audit_record={
            "issue": issue_num,
            "item_id": item_id,
            "item_text": item_text,
            "action": action,
            "reason": reason,
        },
        ticked=False,
        item_text=item_text,
    )


def _process_verifier_entry(
    entry: Any,
    *,
    items: dict[str, str],
    issue_num: int,
    pr_diff: str,
    mode: Mode,
    tick_result: TickResult,
) -> _EntryOutcome | None:
    """verifier 結果 1 件を判定し `_EntryOutcome` を返す（不正な entry は None）."""
    if not isinstance(entry, dict):
        return None
    item_id = entry.get("item_id", "")
    if not isinstance(item_id, str) or item_id not in items:
        # item_id が hash 辞書に存在しない → LLM が hallucinate した可能性 → skip
        tick_result.skipped_hallucination.append({"item": str(item_id), "reason": "item_id mismatch"})
        return _EntryOutcome(
            audit_record={
                "issue": issue_num,
                "item_id": str(item_id),
                "item_text": "",
                "action": "skipped_hallucination",
                "reason": "item_id mismatch",
            },
            ticked=False,
            item_text="",
        )
    # item_id から元の item_text を逆引き
    item_text = items[item_id]

    if not _should_tick(entry, pr_diff):
        # 詳細分類: 判定条件が満たされない原因を audit に残す
        return _skip_outcome(entry, item_id=item_id, item_text=item_text, issue_num=issue_num, tick_result=tick_result)

    # tick 対象
    tick_result.ticked_items.append(item_text)
    action = "dry_ticked" if mode == Mode.DRY_RUN else "ticked"
    return _EntryOutcome(
        audit_record={
            "issue": issue_num,
            "item_id": item_id,
            "item_text": item_text,
            "action": action,
            "confidence": entry.get("confidence"),
            "evidence": entry.get("evidence"),
        },
        ticked=True,
        item_text=item_text,
        evidence=entry.get("evidence"),
    )


def _finalize_tick(
    *,
    issue_num: int,
    pr_num: str,
    repo: str,
    mode: Mode,
    tick_result: TickResult,
    issue_body: str,
    updated_body: str,
    evidence_rows: list[tuple[str, str]],
) -> None:
    """tick 結果を Issue body 更新・evidence コメント投稿へ反映する（実 update は ENABLED のみ）."""
    if mode == Mode.ENABLED and updated_body != issue_body and tick_result.ticked_items:
        try:
            gh.issue_edit_body(issue_num, repo, updated_body)
        except Exception as exc:  # noqa: BLE001 — 個別 tick 失敗は全体を止めない
            _log_stderr(f"==> yaru-auto-tick: Issue #{issue_num} body 更新失敗: {exc}")
        else:
            _post_evidence_comment(issue_num=issue_num, pr_num=pr_num, repo=repo, evidence_rows=evidence_rows)
    if mode == Mode.DRY_RUN and tick_result.ticked_items:
        _log_stderr(f"==> would tick: {len(tick_result.ticked_items)} items")


def _process_issue(
    *,
    issue_num: int,
    pr_num: str,
    repo: str,
    pr_diff: str,
    mode: Mode,
    subagent_available_ref: RunResult,
) -> TickResult | None:
    """1 Issue 分の tick 処理."""
    try:
        issue_data = gh.issue_view(issue_num, repo=repo, fields=("body",))
    except Exception:  # noqa: BLE001
        _log_stderr(f"==> yaru-auto-tick: Issue #{issue_num} 取得失敗のため skip")
        return None
    issue_body = str(issue_data.get("body") or "")
    items = parse_yaru_items(issue_body)

    tick_result = TickResult(issue_number=issue_num)
    if not items:
        return tick_result

    verifier_results = call_yaru_verifier(items, pr_diff)
    if verifier_results is None:
        _log_stderr("==> yaru-verifier subagent unavailable")
        subagent_available_ref.subagent_available = False
        return tick_result

    audit_records: list[dict[str, Any]] = []
    evidence_rows: list[tuple[str, str]] = []
    updated_body = issue_body
    for entry in verifier_results:
        outcome = _process_verifier_entry(
            entry, items=items, issue_num=issue_num, pr_diff=pr_diff, mode=mode, tick_result=tick_result
        )
        if outcome is None:
            continue
        audit_records.append(outcome.audit_record)
        if outcome.ticked and mode != Mode.DRY_RUN:
            updated_body = apply_tick(updated_body, outcome.item_text)
            evidence_rows.append((outcome.item_text, _format_evidence_cell(outcome.evidence)))

    _finalize_tick(
        issue_num=issue_num,
        pr_num=pr_num,
        repo=repo,
        mode=mode,
        tick_result=tick_result,
        issue_body=issue_body,
        updated_body=updated_body,
        evidence_rows=evidence_rows,
    )

    if audit_records:
        try:
            write_audit_log(pr_num, audit_records)
        except OSError:
            _log_stderr("==> yaru-auto-tick: audit log 書き込み失敗")

    return tick_result
