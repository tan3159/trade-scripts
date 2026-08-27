"""`tidd watch-circleci-failures` サブコマンド（旧 `scripts/watch-circleci-failures.sh` の Python 移植）.

CircleCI ワークフローの失敗ステップ（セットアップ・lint・test を含む全ステップ）を検知し、
失敗ステップ単位で GitHub Issue を自動起票する（Issue #1059・#1848）。

重複起票防止: `workflow_name + job_name + failed_step_name + エラー出力先頭1行` から SHA-256
先頭 12 文字の fingerprint を算出し、同じ fingerprint をタイトルに含む OPEN Issue があれば
新規 create せず既存 Issue にコメントするだけ。別 workflow・別エラー文では別 Issue となる。

API チェーン:
1. GET /api/v2/pipeline?branch={branch}                         → 最新パイプライン一覧
2. GET /api/v2/pipeline/{id}/workflow                           → ワークフロー一覧（status=failed を選ぶ）
3. GET /api/v2/workflow/{id}/job                                → ジョブ一覧（status=failed を選ぶ）
4. GET /api/v1.1/project/{vcs-slug}/{job-number}                → ジョブ詳細（steps[].actions[]）。
                                                                   v2 の同名エンドポイントはモダン CircleCI
                                                                   組織で `steps` を返さないため v1.1 を使う
                                                                   （Issue #1333）
5. GET {output_url}                                             → ステップの stdout/stderr

テスト用ファサード: `items[0]` に `failed_step_name` を含む形式を検知すると
チェーンを辿らずそのまま読み取る。

環境変数:
- `CIRCLECI_TOKEN`                       未設定なら skip して exit 0（`WATCH_CIRCLECI_STRICT=1`
                                          の時は exit 1・Issue #1386）
- `CIRCLECI_PROJECT_SLUG`                未設定時は git remote から推定（`gh/<owner>/<repo>`）。
                                          モダン CircleCI 組織では `circleci/<org-uuid>/<project-uuid>`
                                          を明示する必要がある（Issue #1333）。未設定時は起動時に
                                          stderr へ WARN を出す
- `CIRCLECI_BRANCH`                      既定 `main`
- `WATCH_CIRCLECI_DRY_RUN`               1 で Issue 操作をスキップ
- `WATCH_CIRCLECI_STATE_DIR`             既定 `~/.cache/watch-circleci-failures`
- `WATCH_CIRCLECI_EXCLUDE_STEPS_REGEX`   既定は空＝全ステップが起票対象（Issue #1848 で allowlist 廃止）。
                                          設定時のみマッチするステップ名を除外する
- `WATCH_CIRCLECI_STRICT`                1 で 404/5xx と `CIRCLECI_TOKEN` 未設定を exit 非ゼロに
                                          して CircleCI step を failed 扱いにする（Issue #1333・#1386
                                          の silent skip 対策）
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tidd_tools.shared import gh_client as gh
from tidd_tools.shared.cli import add_common_flags
from tidd_tools.shared.errors import GhCommandError
from tidd_tools.shared.paths import cache_dir as _cache_dir
from tidd_tools.shared.subprocess_runner import run as run_subprocess
from tidd_tools.shared.subprocess_timeouts import QUICK_TIMEOUT_SEC

logger = logging.getLogger(__name__)

DEFAULT_BRANCH = "main"
# Issue #1848: 既定は空＝除外なし（全ステップ監視）。旧既定 `^Run tidd_tools pytest$` は
# lint / test ステップの失敗が自動起票されない原因だったため廃止した。
DEFAULT_EXCLUDE_RE = ""
DEFAULT_STATE_DIRNAME = "watch-circleci-failures"
ERROR_SUMMARY_MAX = 120
STEP_OUTPUT_MAX = 2000
HTTP_TIMEOUT_SECS = 30

# Issue #1456: GitHub API 障害を separate exit code で識別する
EXIT_GH_AUTH_FAIL = 10
EXIT_GH_RATE_LIMIT = 11


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "watch-circleci-failures",
        help="CircleCI ワークフロー失敗を検知して Issue を自動起票する（旧 scripts/watch-circleci-failures.sh）",
        description=__doc__,
    )
    add_common_flags(parser)
    parser.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    strict_mode = os.environ.get("WATCH_CIRCLECI_STRICT", "0") == "1"
    token = os.environ.get("CIRCLECI_TOKEN", "")
    if not token:
        print(
            "==> CIRCLECI_TOKEN 未設定。CircleCI 失敗の自動起票は skip します",
            file=sys.stderr,
        )
        # Issue #1386: strict モードでは TOKEN 未設定を silent skip せず exit 1 にする。
        # CircleCI project settings 側で TOKEN が誤って剥がれた・期限切れ等のケースで
        # `Detect CircleCI step failures` step が silent success になり、
        # setup step の失敗が翌朝まで気付かれず放置される事故を防ぐ。
        if strict_mode:
            print(
                "WARN: WATCH_CIRCLECI_STRICT=1 のため exit 1 で終了します"
                " (CircleCI project settings の CIRCLECI_TOKEN を確認してください)",
                file=sys.stderr,
            )
            return 1
        return 0

    # CircleCI 環境では gh CLI の認証変数（GH_TOKEN / GITHUB_TOKEN）が未設定で
    # documented トークンが GH_PAT のみの場合があるため橋渡しする（label_pr_slo と同パターン・#1779）
    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")) and os.environ.get("GH_PAT"):
        os.environ["GH_TOKEN"] = os.environ["GH_PAT"]

    if not shutil.which("gh"):
        print("ERROR: gh コマンドが見つかりません", file=sys.stderr)
        return 1

    branch = os.environ.get("CIRCLECI_BRANCH", DEFAULT_BRANCH)
    dry_run_env = os.environ.get("WATCH_CIRCLECI_DRY_RUN", "0") == "1"
    dry_run = args.dry_run or dry_run_env
    state_dir = Path(os.environ.get("WATCH_CIRCLECI_STATE_DIR") or str(_cache_dir() / DEFAULT_STATE_DIRNAME))
    state_dir.mkdir(parents=True, exist_ok=True)

    slug_env_set = bool(os.environ.get("CIRCLECI_PROJECT_SLUG", ""))
    project_slug = _resolve_project_slug()
    if not project_slug:
        print(
            "WARN: CIRCLECI_PROJECT_SLUG を推定できませんでした。skip します。"
            " モダン CircleCI 組織では circleci/<org-uuid>/<project-uuid> の明示が必要です",
            file=sys.stderr,
        )
        return 0

    if not slug_env_set:
        print(
            "WARN: CIRCLECI_PROJECT_SLUG 未設定 — git remote から推定した "
            f"{project_slug!r} を使用しますが、モダン CircleCI 組織では"
            " circleci/<org-uuid>/<project-uuid> の明示が必要です（silent 404 の原因）",
            file=sys.stderr,
        )

    gh_repo = _project_slug_to_gh_repo(project_slug)

    # Issue #1513: 自 pipeline 内から呼ばれた場合（CIRCLE_WORKFLOW_ID が set）は
    # workflow 一覧 API の status="failed" filter では検出できない（自身は "running" のため）。
    # CircleCI 提供の env vars（CIRCLE_WORKFLOW_ID / CIRCLE_BUILD_NUM / CIRCLE_JOB）を使い、
    # job 詳細 API のみを直接叩いて失敗 step を抽出する。
    if os.environ.get("CIRCLE_WORKFLOW_ID"):
        print(
            "==> CircleCI env vars を検出（自 pipeline モード）。workflow 一覧 API をスキップします",
            file=sys.stderr,
        )
        failure, extract_exit = _extract_failure_self_pipeline(token, project_slug, strict=strict_mode)
        if failure is None:
            return extract_exit
        return _process_failure(failure, gh_repo=gh_repo, dry_run=dry_run)

    print(
        f"==> CircleCI API から最新ワークフロー情報を取得中（project: {project_slug}, branch: {branch}）...",
        file=sys.stderr,
    )

    pipeline_url = f"https://circleci.com/api/v2/project/{project_slug}/pipeline?branch={branch}"
    code, body = _cc_get(pipeline_url, token)
    skip = _handle_http(code, "project/pipeline", strict=strict_mode)
    if skip is not None:
        return skip
    try:
        pipelines_json: Any = json.loads(body or "{}")
    except json.JSONDecodeError:
        print("WARN: CircleCI API response is not valid JSON. skip します", file=sys.stderr)
        return 0
    if not isinstance(pipelines_json, dict):
        print("WARN: CircleCI API response is not a JSON object. skip します", file=sys.stderr)
        return 0

    failure, extract_exit = _extract_failure(pipelines_json, token, project_slug, strict=strict_mode)
    if failure is None:
        return extract_exit

    return _process_failure(failure, gh_repo=gh_repo, dry_run=dry_run)


# ── 失敗情報の抽出 ──────────────────────────────────────────────────────────


def _extract_step_from_job_detail(job_detail: Any, token: str) -> tuple[str, str, str] | None:
    """job 詳細 JSON から失敗 step 情報 (step_name, step_command, step_output) を抽出する.

    Returns None when:
    - 除外パターンで弾かれた
    - 失敗 step が見つからない
    - job_detail が dict でない
    """
    if not isinstance(job_detail, dict):
        return None
    exclude_pattern = os.environ.get("WATCH_CIRCLECI_EXCLUDE_STEPS_REGEX", DEFAULT_EXCLUDE_RE)
    # re.compile("") は全ステップ名にマッチしてしまうため、空文字列は「除外なし」として扱う（Issue #1848）
    exclude_re = re.compile(exclude_pattern) if exclude_pattern else None
    step_name = ""
    step_command = ""
    step_output_url = ""
    for step in job_detail.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        for action in step.get("actions", []) or []:
            if not isinstance(action, dict):
                continue
            # Issue #2738: "timedout"（no_output_timeout 到達）も失敗として検知する
            if action.get("status") not in ("failed", "timedout"):
                continue
            name = action.get("name", "") or ""
            if exclude_re is not None and exclude_re.search(name):
                continue
            step_name = name or "unknown-step"
            step_command = action.get("bash_command", "") or ""
            step_output_url = action.get("output_url", "") or ""
            break
        if step_name:
            break
    if not step_name:
        return None
    # ステップ出力取得
    if step_output_url:
        code, body = _cc_get(step_output_url, token)
        if code == 200:
            step_output = _parse_step_output(body)
        else:
            print(
                f"WARN: step output 取得失敗（HTTP {code}）。本文なしで起票します",
                file=sys.stderr,
            )
            step_output = "（ステップ出力を取得できませんでした。CircleCI 上で確認してください）"
    else:
        step_output = "（output_url が無いためステップ出力なし。CircleCI 上で確認してください）"
    return step_name, step_command, step_output


def _extract_failure_self_pipeline(
    token: str, project_slug: str, *, strict: bool = False
) -> tuple[dict[str, Any] | None, int]:
    """自 pipeline 内で CircleCI env vars を使って失敗 step を抽出する（Issue #1513）.

    自 pipeline の workflow status はまだ "running" のため旧チェーン (`status == "failed"`)
    では検出できない。CircleCI 提供の `CIRCLE_WORKFLOW_ID` / `CIRCLE_BUILD_NUM` / `CIRCLE_JOB`
    を使って直接 job 詳細 API のみを叩く。

    workflow 名（fingerprint 用）は workflow API から取得する。
    """
    build_num = os.environ.get("CIRCLE_BUILD_NUM", "")
    job_name = os.environ.get("CIRCLE_JOB", "") or "unknown-job"
    wf_id = os.environ.get("CIRCLE_WORKFLOW_ID", "")

    if not build_num:
        print(
            "WARN: CIRCLE_WORKFLOW_ID は set だが CIRCLE_BUILD_NUM 未設定。skip します",
            file=sys.stderr,
        )
        return None, 0

    # workflow 名を取得（fingerprint 用）
    wf_name = "unknown-workflow"
    pipeline_number = ""
    if wf_id:
        wf_url = f"https://circleci.com/api/v2/workflow/{wf_id}"
        code, body = _cc_get(wf_url, token)
        skip = _handle_http(code, f"workflow/{wf_id}", strict=strict)
        if skip is not None:
            return None, skip
        try:
            wf_json: Any = json.loads(body or "{}")
        except json.JSONDecodeError:
            print("WARN: workflow API response is not valid JSON. skip します", file=sys.stderr)
            return None, 0
        if isinstance(wf_json, dict):
            wf_name = wf_json.get("name") or "unknown-workflow"
            pipeline_number = str(wf_json.get("pipeline_number", "") or "")

    # job 詳細（v1.1 API）
    v11_slug = _project_slug_to_v11(project_slug)
    job_detail_url = f"https://circleci.com/api/v1.1/project/{v11_slug}/{build_num}"
    code, body = _cc_get(job_detail_url, token)
    skip = _handle_http(code, f"v1.1/project/{v11_slug}/{build_num}", strict=strict)
    if skip is not None:
        return None, skip
    try:
        job_detail: Any = json.loads(body or "{}")
    except json.JSONDecodeError:
        print("WARN: job detail API response is not valid JSON. skip します", file=sys.stderr)
        return None, 0

    step_info = _extract_step_from_job_detail(job_detail, token)
    if step_info is None:
        print(
            "==> 失敗 step が見つかりません（自 pipeline モード・除外パターン適用済み）。skip します",
            file=sys.stderr,
        )
        return None, 0
    step_name, step_command, step_output = step_info

    return (
        {
            "workflow_name": wf_name,
            "job_name": job_name,
            "step_name": step_name,
            "step_command": step_command,
            "step_output": step_output,
            "pipeline_number": pipeline_number,
        },
        0,
    )


def _extract_failure(
    pipelines_json: dict[str, Any],
    token: str,
    project_slug: str,
    *,
    strict: bool = False,
) -> tuple[dict[str, Any] | None, int]:
    """ファサード or 実 API チェーンから失敗情報を抽出して dict にして返す.

    返却 tuple の 1 要素目は以下のキーを持つ dict:
    workflow_name, job_name, step_name, step_command, step_output, pipeline_number。
    skip すべき場合は None を返す。

    2 要素目は「failure が None のときに ``run_cli`` が返すべき exit code」。strict
    モードで API 呼び出しが失敗した場合は 1、そうでなければ 0。
    """
    items = pipelines_json.get("items") or []
    if not isinstance(items, list) or not items:
        print(
            "==> パイプライン情報なし（branch の最新パイプラインが見つかりません）。skip します",
            file=sys.stderr,
        )
        return None, 0
    first = items[0] if isinstance(items[0], dict) else {}

    # ファサード形式: items[0] に failed_step_name が直接入る（テスト・将来の集約 API 用）
    facade = _extract_failure_facade(first)
    if facade is not None:
        return facade

    # 実 API チェーン
    pipeline_id = first.get("id", "")
    pipeline_number = str(first.get("number", "") or "")
    if not pipeline_id:
        print(
            "==> パイプライン情報なし（branch の最新パイプラインが見つかりません）。skip します",
            file=sys.stderr,
        )
        return None, 0

    # workflow 一覧
    wf_url = f"https://circleci.com/api/v2/pipeline/{pipeline_id}/workflow"
    wf_json, skip = _fetch_json_or_skip(wf_url, token, f"pipeline/{pipeline_id}/workflow", strict=strict)
    if wf_json is None:
        return None, skip

    failed_wf = _first_failed(wf_json, key="status")
    if failed_wf is None:
        print(
            "==> 最新パイプラインに失敗ワークフローはありません。Issue 起票不要です。",
            file=sys.stderr,
        )
        return None, 0
    wf_name = failed_wf.get("name", "unknown-workflow") or "unknown-workflow"
    wf_id = failed_wf.get("id", "")
    if not wf_id:
        print("WARN: failed workflow id が取得できません。skip します", file=sys.stderr)
        return None, 0

    # job 一覧
    jobs_url = f"https://circleci.com/api/v2/workflow/{wf_id}/job"
    jobs_json, skip = _fetch_json_or_skip(jobs_url, token, f"workflow/{wf_id}/job", strict=strict)
    if jobs_json is None:
        return None, skip

    failed_job = _first_failed(jobs_json, key="status")
    if failed_job is None:
        print("WARN: failed job が取得できません。skip します", file=sys.stderr)
        return None, 0
    job_name = failed_job.get("name", "unknown-job") or "unknown-job"
    job_number = failed_job.get("job_number")
    if not job_number:
        print("WARN: failed job number が取得できません。skip します", file=sys.stderr)
        return None, 0

    # job 詳細（v1.1 API を使う。モダン CircleCI 組織では v2 が steps を返さないため。Issue #1333）
    v11_slug = _project_slug_to_v11(project_slug)
    job_detail_url = f"https://circleci.com/api/v1.1/project/{v11_slug}/{job_number}"
    job_detail, skip = _fetch_json_or_skip(
        job_detail_url, token, f"v1.1/project/{v11_slug}/{job_number}", strict=strict
    )
    if job_detail is None:
        return None, skip

    exclude_pattern = os.environ.get("WATCH_CIRCLECI_EXCLUDE_STEPS_REGEX", DEFAULT_EXCLUDE_RE)
    # re.compile("") は全ステップ名にマッチしてしまうため、空文字列は「除外なし」として扱う（Issue #1848）
    exclude_re = re.compile(exclude_pattern) if exclude_pattern else None

    step_name, step_command, step_output_url = _find_failed_step(job_detail, exclude_re)

    if not step_name:
        if exclude_re is not None:
            print(
                f"==> 失敗ステップが除外パターン ({exclude_re.pattern}) にマッチするか、"
                "起票対象の失敗ステップがありません。skip します",
                file=sys.stderr,
            )
        else:
            print("==> 起票対象の失敗ステップがありません。skip します", file=sys.stderr)
        return None, 0

    # ステップ出力取得
    step_output = _fetch_step_output(step_output_url, token)

    return (
        {
            "workflow_name": wf_name,
            "job_name": job_name,
            "step_name": step_name,
            "step_command": step_command,
            "step_output": step_output,
            "pipeline_number": pipeline_number,
        },
        0,
    )


def _extract_failure_facade(first: dict[str, Any]) -> tuple[dict[str, Any] | None, int] | None:
    """ファサード形式（items[0] に failed_step_name が直接入る）から失敗情報を返す.

    ファサード形式でない場合は None を返し、呼び出し元は実 API チェーンへ進む。
    """
    if "failed_step_name" not in first:
        return None
    status = first.get("status", "")
    if status != "failed":
        print(
            f"==> 最新ワークフローは status={status}（失敗ではない）。Issue 起票不要です。",
            file=sys.stderr,
        )
        return None, 0
    return (
        {
            "workflow_name": first.get("name", "unknown-workflow") or "unknown-workflow",
            "job_name": first.get("job_name", "unknown-job") or "unknown-job",
            "step_name": first.get("failed_step_name", "unknown-step") or "unknown-step",
            "step_command": first.get("failed_step_command", "") or "",
            "step_output": first.get("failed_step_output", "") or "",
            "pipeline_number": str(first.get("pipeline_number", "") or ""),
        },
        0,
    )


def _fetch_json_or_skip(
    url: str,
    token: str,
    label: str,
    *,
    strict: bool,
) -> tuple[dict[str, Any] | None, int]:
    """GET + JSON パース。skip 判定（strict 時 exit 1）は (None, skip_rc) で伝える."""
    code, body = _cc_get(url, token)
    skip = _handle_http(code, label, strict=strict)
    if skip is not None:
        return None, skip
    try:
        parsed: Any = json.loads(body or "{}")
    except json.JSONDecodeError:
        print(f"WARN: {label} API response is not valid JSON. skip します", file=sys.stderr)
        return None, 0
    return (parsed if isinstance(parsed, dict) else {}), 0


def _find_failed_step(job_detail: Any, exclude_re: re.Pattern[str] | None) -> tuple[str, str, str]:
    """job detail から失敗（failed/timedout）ステップの名前・コマンド・output_url を探す."""
    step_name = ""
    step_command = ""
    step_output_url = ""
    if isinstance(job_detail, dict):
        for step in job_detail.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            for action in step.get("actions", []) or []:
                if not isinstance(action, dict):
                    continue
                # Issue #2738: "timedout"（no_output_timeout 到達）も失敗として検知する
                if action.get("status") not in ("failed", "timedout"):
                    continue
                name = action.get("name", "") or ""
                if exclude_re is not None and exclude_re.search(name):
                    continue
                step_name = name or "unknown-step"
                step_command = action.get("bash_command", "") or ""
                step_output_url = action.get("output_url", "") or ""
                break
            if step_name:
                break
    return step_name, step_command, step_output_url


def _fetch_step_output(step_output_url: str, token: str) -> str:
    """ステップ出力を取得する（URL 無し・失敗時はフォールバック文言を返す）."""
    if not step_output_url:
        return "（output_url が無いためステップ出力なし。CircleCI 上で確認してください）"
    code, body = _cc_get(step_output_url, token)
    if code == 200:
        return _parse_step_output(body)
    print(
        f"WARN: step output 取得失敗（HTTP {code}）。本文なしで起票します",
        file=sys.stderr,
    )
    return "（ステップ出力を取得できませんでした。CircleCI 上で確認してください）"


def _first_failed(payload: Any, *, key: str) -> dict[str, Any] | None:
    """payload.items[*] の中で `.<key> == "failed"` の最初の要素を返す."""
    if not isinstance(payload, dict):
        return None
    items = payload.get("items") or []
    if not isinstance(items, list):
        return None
    for entry in items:
        if isinstance(entry, dict) and entry.get(key) == "failed":
            return entry
    return None


_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    """ANSI エスケープシーケンスを除去する（不完全なシーケンスも含む）.

    完全なシーケンス（CSI/ESC + 終端コード）を除去した後、
    残余の ESC 文字も除去して文字化けを防ぐ（Issue #3100）。
    """
    # 完全なシーケンス（例: \x1b[32m, \x1b[0m）を除去
    result = _ANSI_ESCAPE_RE.sub("", text)
    # 不完全なシーケンス（\x1b が残留している場合）を除去
    result = result.replace("\x1b", "")
    return result


def _parse_step_output(raw: str) -> str:
    """CircleCI のステップ出力（生 JSON 配列 or 文字列）を本文文字列に整形する."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, list):
        parts: list[str] = []
        for entry in data:
            if isinstance(entry, dict):
                msg = entry.get("message", "")
                if isinstance(msg, str):
                    parts.append(msg)
        result = "".join(parts)
    else:
        result = raw or ""
    result = _strip_ansi(result)
    if len(result) > STEP_OUTPUT_MAX:
        result = f"...（先頭省略）...{result[-STEP_OUTPUT_MAX:]}"
    return result


# ── Issue 起票処理 ─────────────────────────────────────────────────────────


def _process_failure(failure: dict[str, Any], *, gh_repo: str | None, dry_run: bool) -> int:
    wf_name: str = failure["workflow_name"]
    job_name: str = failure["job_name"]
    step_name: str = failure["step_name"]
    step_command: str = failure["step_command"]
    step_output: str = failure["step_output"]
    pipeline_number: str = failure["pipeline_number"]

    # エラー要約（タイトル検索用キー）
    first_line = step_output.splitlines()[0] if step_output else ""
    error_summary = first_line.replace("\r", "")
    if len(error_summary) > ERROR_SUMMARY_MAX:
        error_summary = error_summary[:ERROR_SUMMARY_MAX]

    fingerprint_raw = f"{wf_name}|{job_name}|{step_name}|{error_summary}"
    fingerprint = hashlib.sha256(fingerprint_raw.encode("utf-8")).hexdigest()[:12]

    title = f"🤖 fix: CircleCI {job_name} の {step_name} が失敗 [{fingerprint}]"
    print(
        f"==> 失敗を検知: workflow={wf_name}, job={job_name}, step={step_name}, fp={fingerprint}",
        file=sys.stderr,
    )

    existing_number = _find_existing_by_fingerprint(fingerprint, repo=gh_repo)

    if dry_run:
        if existing_number is not None:
            print(
                f"==> [dry-run] DRY_RUN=1 のため既存 Issue #{existing_number} へのコメントをスキップします",
                file=sys.stderr,
            )
        else:
            print(
                f"==> [dry-run] DRY_RUN=1 のため新規 Issue 作成をスキップします: {title}",
                file=sys.stderr,
            )
        return 0

    if existing_number is not None:
        comment_body = _comment_body(
            fingerprint=fingerprint,
            wf_name=wf_name,
            job_name=job_name,
            step_name=step_name,
            pipeline_number=pipeline_number,
            error_summary=error_summary,
        )
        _post_comment(existing_number, comment_body, repo=gh_repo)
        print(
            f"==> 既存 Issue #{existing_number} に再発コメントを追記しました",
            file=sys.stderr,
        )
        return 0

    template_body = _new_issue_body(
        wf_name=wf_name,
        job_name=job_name,
        pipeline_number=pipeline_number,
        step_name=step_name,
        fingerprint=fingerprint,
        step_command=step_command,
        step_output=step_output,
    )
    body = _enhance_issue_body_with_llm(
        wf_name=wf_name,
        job_name=job_name,
        step_name=step_name,
        step_command=step_command,
        step_output=step_output,
        template_body=template_body,
    )
    # Issue #1456: 401/429 の GitHub API 障害は separate exit code で伝播する
    create_result = _create_issue(title, body, repo=gh_repo)
    if create_result == "ok":
        print(f"==> Issue を作成しました: {title}", file=sys.stderr)
        return 0
    if create_result == "auth_fail":
        print(
            "==> GitHub API authentication failed. Exit 10 (fail-loud, Issue #1456).",
            file=sys.stderr,
        )
        return EXIT_GH_AUTH_FAIL
    if create_result == "rate_limit":
        print(
            "==> GitHub API rate limit exceeded. Exit 11 (fail-loud, Issue #1456).",
            file=sys.stderr,
        )
        return EXIT_GH_RATE_LIMIT
    print(
        "==> Issue 作成に失敗したためスキップ扱いで終了します（CI は止めません）",
        file=sys.stderr,
    )
    return 0


def _enhance_issue_body_with_llm(
    *,
    wf_name: str,
    job_name: str,
    step_name: str,
    step_command: str,
    step_output: str,
    template_body: str,
) -> str:
    """LLM で CircleCI 失敗 Issue 本文を強化する（Issue #1247）. フォールバックは template."""
    try:
        from tidd_tools.shared.llm_issue_body import enhance_issue_body
    except ImportError:
        return template_body

    proc = run_subprocess(
        ["git", "rev-parse", "--show-toplevel"],
        timeout=QUICK_TIMEOUT_SEC,
    )
    repo_root = Path(proc.stdout.strip()) if (proc.returncode == 0 and proc.stdout.strip()) else Path.cwd()

    context = (
        "# CircleCI ステップ失敗\n\n"
        f"workflow: {wf_name}\n"
        f"job: {job_name}\n"
        f"failed step: {step_name}\n\n"
        f"## 失敗コマンド\n\n{step_command}\n\n"
        f"## 失敗出力（先頭 2000 文字）\n\n{step_output[:2000]}\n"
    )
    result = enhance_issue_body(
        context=context,
        template_body=template_body,
        repo_root=repo_root,
        additional_prompt=(
            "関連しそうな .circleci/config.yml や run_once_install_packages.sh、"
            "pyproject.toml を read_file / check_file_exists で確認してから、"
            "根本原因と修正手順を含む本文を submit_issue_body に渡してください。"
        ),
    )
    if result.enhanced:
        print("==> LLM で Issue 本文を強化しました", file=sys.stderr)
    else:
        if result.reason == "no-key":
            print(
                "ANTHROPIC_API_KEY 未設定のためテンプレート生成にフォールバック",
                file=sys.stderr,
            )
        else:
            print(
                f"==> LLM 強化はスキップ（{result.reason}）。テンプレートを使用します。",
                file=sys.stderr,
            )
    return result.body


def _find_existing_by_fingerprint(fingerprint: str, *, repo: str | None) -> int | None:
    """`<fp> in:title` で OPEN Issue を検索し、タイトルに fingerprint を含む最初の番号を返す."""
    args = [
        "issue",
        "list",
        "--state",
        "open",
        "--json",
        "number,title",
        "--limit",
        "50",
        "--search",
        f"{fingerprint} in:title",
    ]
    if repo:
        args = ["-R", repo, *args]
    try:
        result = run_subprocess(["gh", *args])
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        data: Any = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    for entry in data:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title", "")
        number = entry.get("number")
        if isinstance(title, str) and fingerprint in title and isinstance(number, int):
            return number
    return None


def _post_comment(issue_number: int, body: str, *, repo: str | None) -> None:
    """`gh issue comment <num>` で既存 Issue にコメントを投稿する."""
    # gh issue comment は gh_client にエイリアスがないため subprocess 経由で実行する
    import tempfile

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md", encoding="utf-8") as tmp:
        tmp.write(body)
        path = tmp.name
    try:
        args = ["issue", "comment", str(issue_number), "--body-file", path]
        if repo:
            args = ["issue", "comment", str(issue_number), "--repo", repo, "--body-file", path]
        try:
            result = run_subprocess(["gh", *args])
        except OSError as exc:
            print(
                f"WARN: Issue #{issue_number} へのコメント投稿に失敗しました: {exc}",
                file=sys.stderr,
            )
            return
        if result.returncode != 0:
            print(
                f"WARN: Issue #{issue_number} へのコメント投稿に失敗しました",
                file=sys.stderr,
            )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _classify_gh_error(exc: GhCommandError) -> str:
    """Issue #1456: gh CLI エラーから (auth_fail / rate_limit / other) を判定する.

    gh CLI は 401 authentication を exit 4 で返し、429 rate limit も exit 4 で返すが
    stderr に "rate limit" 文字列を含む点で区別可能。
    """
    stderr_lower = (exc.stderr or "").lower()
    if "rate limit" in stderr_lower or "api rate limit" in stderr_lower:
        return "rate_limit"
    if exc.returncode == 4 or "authentication" in stderr_lower or "requires authentication" in stderr_lower:
        return "auth_fail"
    return "other"


def _create_issue(title: str, body: str, *, repo: str | None) -> str:
    """ラベル付きで起票し、失敗したらラベルなしで再試行する.

    Returns:
        ``"ok"`` — 正常起票 (200)
        ``"auth_fail"`` — 401/403 相当（exit code 10 に伝播）
        ``"rate_limit"`` — 429 rate limit（exit code 11 に伝播）
        ``"other"`` — その他の失敗（従来通り silent skip）
    """
    try:
        gh.issue_create(title, body, ["source: ci", "type: fix", "priority: high"], repo)
        return "ok"
    except GhCommandError as exc:
        first_kind = _classify_gh_error(exc)
        logger.debug("issue create with labels failed (%s): %s", first_kind, exc)
    try:
        gh.issue_create(title, body, [], repo)
        return "ok"
    except GhCommandError as exc:
        second_kind = _classify_gh_error(exc)
        # 401/429 の場合は最初の失敗理由も上流に伝えるため fail-loud で返す
        if second_kind == "auth_fail" or first_kind == "auth_fail":
            print(
                f"WARN: GitHub API authentication failed: {title} ({exc})",
                file=sys.stderr,
            )
            return "auth_fail"
        if second_kind == "rate_limit" or first_kind == "rate_limit":
            print(
                f"WARN: GitHub API rate limit exceeded: {title} ({exc})",
                file=sys.stderr,
            )
            return "rate_limit"
        print(f"WARN: Issue 作成に失敗しました: {title} ({exc})", file=sys.stderr)
        return "other"


# ── HTTP / project-slug ヘルパー ──────────────────────────────────────────────


def _cc_get(url: str, token: str) -> tuple[int, str]:
    """CircleCI API を GET し `(HTTP code, body)` を返す。接続失敗時は (0, "")."""
    req = urllib.request.Request(
        url,
        headers={"Circle-Token": token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.getcode() or 0, body
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        except (AttributeError, OSError):
            body = ""
        return exc.code, body
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.debug("CircleCI HTTP get failed for %s: %s", url, exc)
        return 0, ""


def _handle_http(code: int, ctx: str, *, strict: bool = False) -> int | None:
    """HTTP コードを評価する。継続可なら None、終了すべきなら exit code を返す.

    ``strict=True`` の時は 404 / 5xx などの「unexpected response」を exit 1 にして
    CircleCI 側の step を failed 扱いにする。429 (rate limit) や 401/403 (auth) は
    一時的・別問題のため strict でも exit 0 を維持する。
    """
    if code == 200:
        return None
    if code == 429:
        print(
            f"WARN: CircleCI API rate limit exceeded, skipping auto-issue (HTTP 429 at {ctx})",
            file=sys.stderr,
        )
        return 0
    if code in (401, 403):
        print(
            f"WARN: CircleCI API authentication error (HTTP {code} at {ctx}). CIRCLECI_TOKEN を確認してください",
            file=sys.stderr,
        )
        return 0
    print(
        f"WARN: CircleCI API unexpected response (HTTP {code} at {ctx}). skip します",
        file=sys.stderr,
    )
    if strict:
        print(
            "WARN: WATCH_CIRCLECI_STRICT=1 のため exit 1 で終了します"
            " (CIRCLECI_PROJECT_SLUG やベース URL の設定を確認してください)",
            file=sys.stderr,
        )
        return 1
    return 0


def _resolve_project_slug() -> str | None:
    """CIRCLECI_PROJECT_SLUG または git remote から `gh/<owner>/<repo>` を構築する."""
    slug = os.environ.get("CIRCLECI_PROJECT_SLUG", "")
    if slug:
        return slug
    try:
        result = run_subprocess(["git", "remote", "get-url", "origin"])
    except OSError:
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)(?:\.git)?", url)
    if not m:
        return None
    return f"gh/{m.group(1)}"


def _project_slug_to_gh_repo(slug: str) -> str | None:
    """`gh/<owner>/<repo>` または `github/<owner>/<repo>` から `<owner>/<repo>` を抽出する.

    モダン CircleCI 組織 (``circleci/<org-uuid>/<project-uuid>``) の場合は None を返す
    （GitHub 連動しないため gh CLI 側で ``--repo`` を指定できない）。
    """
    m = re.match(r"^(?:gh|github)/(.+)$", slug)
    if not m:
        return None
    return m.group(1)


def _project_slug_to_v11(slug: str) -> str:
    """v2 API slug を v1.1 API URL 用の vcs-slug 形式へ正規化する.

    - ``gh/<owner>/<repo>`` → ``github/<owner>/<repo>``（v1.1 の canonical 形式）
    - ``github/<owner>/<repo>`` はそのまま
    - モダン CircleCI 組織 ``circleci/<org-uuid>/<project-uuid>`` はそのまま（v1.1
      でも UUID ベースで動作する）
    """
    m = re.match(r"^gh/(.+)$", slug)
    if m:
        return f"github/{m.group(1)}"
    return slug


def _comment_body(
    *,
    fingerprint: str,
    wf_name: str,
    job_name: str,
    step_name: str,
    pipeline_number: str,
    error_summary: str,
) -> str:
    return f"""### 再発検知（自動）

CircleCI で同一の失敗が再発しました（fingerprint: `{fingerprint}`）。

- workflow: `{wf_name}`
- job: `{job_name}`
- step: `{step_name}`
- pipeline: #{pipeline_number}
- エラー要約: `{error_summary}`

---
*このコメントは `tidd watch-circleci-failures` によって自動投稿されました。*"""


def _new_issue_body(
    *,
    wf_name: str,
    job_name: str,
    pipeline_number: str,
    step_name: str,
    fingerprint: str,
    step_command: str,
    step_output: str,
) -> str:
    return f"""## 背景

CircleCI の `{wf_name}` ワークフロー（job: `{job_name}`, pipeline: #{pipeline_number}）で、
ステップ `{step_name}` が失敗しました。

本 Issue は `tidd watch-circleci-failures` が CircleCI API 経由で検知して
自動起票したものです（Issue #1848 でセットアップ・lint・test を含む全ステップが監視対象）。

**重複検知キー（fingerprint）:** `{fingerprint}` — このキーは `workflow_name` + `job_name` + \
`failed_step_name` + エラー出力先頭1行（先頭120文字）から算出されます。同じ workflow/job/step・\
同じエラー出力の再発時のみこの Issue にコメントが追記され、別エラー（例: pip install 失敗 vs \
apt-get 失敗）や別ワークフローでは新規 Issue が起票されます。

### 失敗コマンド

```
{step_command}
```

### 失敗ステップの出力

```
{step_output}
```

## やること

- [ ] 失敗の原因を特定する（依存パッケージ・ベースイメージ・ネットワーク・テスト等）
- [ ] `.circleci/config.yml`・テスト・周辺スクリプトを修正する
- [ ] [AI確認-post-merge] CircleCI 上で `{wf_name}` を手動 trigger して再現しないことを確認する

## 振る舞い

```gherkin
Feature: {step_name} の修正

  Scenario: 修正後にステップが成功する
    Given .circleci/config.yml が修正されている
    When CircleCI で {wf_name} ワークフローが実行される
    Then {step_name} ステップが exit 0 で完了する

  Scenario: 修正前は同じエラーが再発する（リグレッション防止）
    Given 修正前の状態
    When CircleCI で {wf_name} ワークフローが実行される
    Then {step_name} ステップが exit 1 で失敗する
```

---
*この Issue は `tidd watch-circleci-failures` によって自動作成されました。*"""
