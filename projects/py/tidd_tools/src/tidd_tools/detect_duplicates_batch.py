"""`tidd detect-duplicates-batch` サブコマンド (Issue #1466, #1626).

`/detect-duplicates` skill を大量 Issue (100+) に対応させるため、priority + created_at
順で並び替えた open Issue を 1 batch = 30 Issue 上限に分割し、各 batch の実行時間を
`shared/paths.cache_dir() / "detect-duplicates-perf" / "batch-<N>.json"` に記録する
（Issue #2950: Linux では `~/.cache/ai-dev-handbook/detect-duplicates-perf/`）。

**設計方針:**

- Issue 取得は `gh issue list` (subprocess) で 1 回まとめて行う
- 並び順: priority (critical → high → medium → low) → created_at (古い順)
- batch 分割: 30 Issue/batch (context window 安全域)
- 各 batch の実行時間・処理件数を JSON に記録
- skill 起動 (subprocess ai-review 経由) 部分は本 CLI の scope 外（skill 自体は SKILL.md に記載）
- 本 CLI は batch 分割 + perf 記録のバックエンド。skill 側から呼び出す

**Issue #1626: ローカル埋め込み pre-filter:**

- `--use-local-embeddings` フラグで pre-filter を有効化
- CPU ローカルモデル（model2vec / sentence-transformers）でベクトル化
- cosine 類似度 0.85 以上のペアのみ duplicate-detector subagent に渡す
- sqlite キャッシュ（`shared/paths.cache_dir() / "issue-embeddings.db"`）で未変更 Issue の再計算をスキップ

stdlib のみ使用（embedding_prefilter は optional 依存の model2vec / sentence-transformers を呼ぶ）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tidd_tools.shared import paths

DEFAULT_BATCH_SIZE = 30
_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_DEFAULT_EMBEDDING_THRESHOLD = 0.85


def _perf_dir() -> Path:
    override = os.environ.get("DETECT_DUPLICATES_PERF_DIR")
    if override:
        return Path(override)
    # Issue #2950: Path.home() / ".cache" ハードコードを shared/paths.cache_dir() へ統一
    return paths.cache_dir() / "detect-duplicates-perf"


def _extract_priority(labels: list[dict[str, Any]]) -> str:
    """label array から priority を抽出する（見つからなければ low）."""
    for lbl in labels:
        name: str = str(lbl.get("name", "")) if isinstance(lbl, dict) else str(lbl)
        if name.startswith("priority:"):
            return name.split(":", 1)[1].strip().lower()
    return "low"


def sort_issues_by_priority(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """priority (critical → low) → created_at (古い順) でソートする."""

    def key(issue: dict[str, Any]) -> tuple[int, str]:
        pri = _extract_priority(issue.get("labels", []))
        pri_ord = _PRIORITY_ORDER.get(pri, 99)
        created = str(issue.get("createdAt", "9999"))
        return (pri_ord, created)

    return sorted(issues, key=key)


def split_into_batches(
    issues: list[dict[str, Any]], batch_size: int = DEFAULT_BATCH_SIZE
) -> list[list[dict[str, Any]]]:
    """Issue リストを batch_size ごとに分割する."""
    if batch_size <= 0:
        raise ValueError(f"batch_size must be > 0, got {batch_size}")
    return [issues[i : i + batch_size] for i in range(0, len(issues), batch_size)]


def _run_with_embeddings_safe(
    issues: list[dict[str, Any]],
    db_path: Path,
    threshold: float,
) -> list[dict[str, Any]]:
    """embedding_prefilter.run_with_embeddings の薄いラッパー（テスト用 patch 対象）."""
    from tidd_tools.embedding_prefilter import run_with_embeddings

    return run_with_embeddings(issues, db_path=db_path, threshold=threshold)


def _fetch_open_issues(repo: str | None = None) -> list[dict[str, Any]]:
    """`gh issue list --state open` で open Issue を取得する."""
    args = [
        "gh",
        "issue",
        "list",
        "--state",
        "open",
        "--limit",
        "1000",
        "--json",
        "number,title,body,labels,createdAt,updatedAt",
    ]
    if repo:
        args += ["--repo", repo]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: gh issue list failed: {exc}", file=sys.stderr)
        raise
    if proc.returncode != 0:
        print(
            f"ERROR: gh issue list failed (exit={proc.returncode}): {proc.stderr[:400]}",
            file=sys.stderr,
        )
        raise RuntimeError(f"gh issue list failed exit={proc.returncode}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"ERROR: gh issue list JSON parse failed: {exc}", file=sys.stderr)
        raise
    if not isinstance(data, list):
        raise TypeError(f"expected list, got {type(data).__name__}")
    return data


def _record_batch_metrics(batch_num: int, batch: list[dict[str, Any]], duration_seconds: float) -> Path:
    d = _perf_dir()
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"batch-{batch_num}.json"
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    numbers = [i.get("number") for i in batch if "number" in i]
    payload: dict[str, Any] = {
        "timestamp": ts,
        "batch_num": batch_num,
        "issue_count": len(batch),
        "issue_numbers": numbers,
        "duration_seconds": round(duration_seconds, 3),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _record_pair_batch_metrics(
    batch_num: int,
    pairs: list[dict[str, Any]],
    duration_seconds: float,
) -> Path:
    """pre-filter 後のペアリストを含む perf メトリクスを記録する (Issue #1626).

    pairs に含まれる情報（a, b, similarity）をそのまま JSON に書き出すことで、
    後段の duplicate-detector subagent が「このペアのみを精査する」という
    コンテキストを得られる。
    """
    d = _perf_dir()
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"batch-{batch_num}.json"
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    payload: dict[str, Any] = {
        "timestamp": ts,
        "batch_num": batch_num,
        "pair_count": len(pairs),
        "pairs": pairs,  # [{a: int, b: int, similarity: float}, ...] を保持
        "duration_seconds": round(duration_seconds, 3),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def run(
    *,
    batch_size: int,
    repo: str | None,
    dry_run: bool,
    use_local_embeddings: bool = False,
    embedding_threshold: float = _DEFAULT_EMBEDDING_THRESHOLD,
    embedding_db: Path | None = None,
    subagent_fn: Callable[[list[dict[str, Any]]], None] | None = None,
) -> int:
    try:
        issues = _fetch_open_issues(repo=repo)
    except (
        RuntimeError,
        subprocess.SubprocessError,
        FileNotFoundError,
        json.JSONDecodeError,
        TypeError,  # AI review PR #1512 attempt 1 反映: gh JSON が list でない場合
    ):
        return 1

    if not issues:
        print("==> no open Issues found", file=sys.stderr)
        return 0

    # --- Issue #1626: ローカル埋め込み pre-filter ---
    # pre-filter が有効な場合、batch は「ペアリスト」を単位として分割する。
    # ペアリストをそのまま渡すことで、後段 LLM は
    # "3 ペアに絞り込み済み" の情報を受け取れる（Scenario 1 準拠）。
    if use_local_embeddings:
        db_path = embedding_db
        if db_path is None:
            db_env = os.environ.get("TIDD_EMBEDDING_DB")
            # Issue #2950: Path.home() / ".cache" / "tidd" ハードコードを shared/paths.cache_dir() へ統一
            db_path = Path(db_env) if db_env else paths.cache_dir() / "issue-embeddings.db"

        try:
            filtered_pairs = _run_with_embeddings_safe(issues, db_path=db_path, threshold=embedding_threshold)
        except ImportError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        print(
            f"==> embedding pre-filter: {len(filtered_pairs)} pairs above threshold={embedding_threshold}",
            file=sys.stderr,
        )
        if not filtered_pairs:
            print("no duplicate-suspect pairs found by pre-filter")
            return 0

        # ペアリストをバッチに分割して perf 記録する
        # 各 batch にはペア情報（a, b, similarity）を含む Issue のみが入る
        pair_batches = [filtered_pairs[i : i + batch_size] for i in range(0, len(filtered_pairs), batch_size)]

        print(
            f"==> {len(filtered_pairs)} filtered pairs → {len(pair_batches)} batches (size={batch_size})",
            file=sys.stderr,
        )

        # subagent_fn が未注入の場合、全 filtered_pairs を 1 つの JSON としてまとめて stdout 出力する。
        # バッチ数が複数になっても 1 度だけ出力することで、呼び出し側が常に単一の有効 JSON を受け取れる。
        if not dry_run and subagent_fn is None:
            print(json.dumps({"pairs": filtered_pairs}, ensure_ascii=False))

        for i, pair_batch in enumerate(pair_batches):
            start = time.perf_counter()
            if dry_run:
                numbers_in_batch = sorted({p["a"] for p in pair_batch} | {p["b"] for p in pair_batch})
                print(
                    f"==> [dry-run] batch {i}: {len(pair_batch)} pairs (issue numbers: {numbers_in_batch})",
                    file=sys.stderr,
                )
            elif subagent_fn is not None:
                # Issue #1778: pre-filter 後の filtered_pairs を duplicate-detector subagent に渡す。
                # subagent_fn は skill 側（/detect-duplicates SKILL.md）が注入するコールバック。
                subagent_fn(pair_batch)
            elapsed = time.perf_counter() - start
            # _record_pair_batch_metrics は perf 計測目的として常に実行する（Issue #1778）。
            metrics_path = _record_pair_batch_metrics(i, pair_batch, elapsed)
            print(
                f"==> batch {i}: {len(pair_batch)} pairs, {elapsed:.3f}s → {metrics_path}",
                file=sys.stderr,
            )

        return 0

    sorted_issues = sort_issues_by_priority(issues)
    batches = split_into_batches(sorted_issues, batch_size=batch_size)

    print(
        f"==> {len(issues)} open Issues → {len(batches)} batches (size={batch_size})",
        file=sys.stderr,
    )

    for i, batch in enumerate(batches):
        start = time.perf_counter()
        # skill 呼び出しは本 CLI の scope 外。ここでは perf 記録のみ実行する。
        # dry_run 時も metrics は記録する（perf 検証のため）。
        if dry_run:
            print(
                f"==> [dry-run] batch {i}: {len(batch)} issues (numbers: {[b.get('number') for b in batch]})",
                file=sys.stderr,
            )
        elapsed = time.perf_counter() - start
        metrics_path = _record_batch_metrics(i, batch, elapsed)
        print(
            f"==> batch {i}: {len(batch)} issues, {elapsed:.3f}s → {metrics_path}",
            file=sys.stderr,
        )

    return 0


def _handle(args: argparse.Namespace) -> int:
    return run(
        batch_size=int(args.batch_size),
        repo=args.repo or None,
        dry_run=bool(args.dry_run),
        use_local_embeddings=bool(args.use_local_embeddings),
        embedding_threshold=float(args.embedding_threshold),
    )


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "detect-duplicates-batch",
        help="/detect-duplicates 用の open Issue を priority + created_at で分割して perf 記録する (Issue #1466)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"1 batch あたりの Issue 数（既定: {DEFAULT_BATCH_SIZE}）",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="対象リポジトリ（既定: git remote から推定）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="skill 呼び出しをせず perf 記録のみ行う",
    )
    parser.add_argument(
        "--use-local-embeddings",
        action="store_true",
        help="CPU ローカル埋め込みモデルで pre-filter を行い、LLM 呼び出し件数を削減する (Issue #1626)",
    )
    parser.add_argument(
        "--embedding-threshold",
        type=float,
        default=_DEFAULT_EMBEDDING_THRESHOLD,
        help=f"ローカル埋め込み pre-filter の cosine 類似度閾値（既定: {_DEFAULT_EMBEDDING_THRESHOLD}）",
    )
    parser.set_defaults(func=_handle)
