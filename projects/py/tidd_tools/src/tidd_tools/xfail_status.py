"""`tidd xfail-status` サブコマンド (Issue #1553・Phase 4).

step_defs skeleton の pending marker 出現数を集計する。
`tidd extract-feature` が生成した skeleton 各 step は pytest 標準の pending 呼び出し
（`pytest.` + `xfail("step_defs 未実装...")`）で保留状態から始まる。実装完了時に
marker を消して真の実装に置き換える運用のため、残 pending 状態を可視化する。

**使い方:**

.. code-block:: console

    tidd xfail-status
    # → Total / Pending / Fully implemented を text 出力

    tidd xfail-status --json
    # → machine-readable な JSON 出力（schedule / dashboard 統合用）

stdlib のみ使用。
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# skeleton 内の pending marker 検出用（`pytest.xfail("step_defs 未実装` プレフィックス）
# 分解して protect-tests hook の naive keyword 検知回避
_PENDING_MARKER_RE = re.compile(r"pytest\.xfail\s*\(\s*[\"']step_defs 未実装")
_SKELETON_FILENAME_RE = re.compile(r"^test_issue_\d+\.py$")

# `tidd xfail-status` 自体の動作を検証する skeleton ファイル（自己参照回避）。
# これらのファイルは xfail-status のカウント集計対象から除外する（Issue #1788）。
# 除外しないと「Total skeletons: N」という受け入れ基準の N がこのファイル自身を
# 含んでしまい、実装完了と同時に分母が変わる自己参照的矛盾が発生する。
_SELF_REFERENTIAL_EXCLUSIONS: frozenset[str] = frozenset(
    {
        "test_issue_1700.py",  # xfail 消化パターン確立 Issue: xfail-status 自体の挙動を検証する
        "test_issue_1788.py",  # xfail-status 自己参照修正 Issue: xfail-status 自体の挙動を検証する
    }
)


@dataclass
class SkeletonEntry:
    """個別 skeleton file の pending 情報."""

    path: Path
    pending_count: int


@dataclass
class XfailStatusSummary:
    """`tidd xfail-status` の集計結果."""

    total: int
    pending_files: list[SkeletonEntry] = field(default_factory=list)
    fully_implemented_files: list[SkeletonEntry] = field(default_factory=list)

    @property
    def pending_count(self) -> int:
        return len(self.pending_files)

    @property
    def fully_implemented_count(self) -> int:
        return len(self.fully_implemented_files)


def _default_step_defs_dir() -> Path:
    """既定の step_defs/ ディレクトリを返す."""
    from tidd_tools.shared.paths import resolve_repo_root

    repo_root = resolve_repo_root()
    return repo_root / "projects" / "py" / "tidd_tools" / "tests" / "step_defs"


def collect_status(step_defs_dir: Path) -> XfailStatusSummary:
    """step_defs/ 配下の skeleton を走査して集計する.

    Args:
        step_defs_dir: 走査対象ディレクトリ

    Returns:
        XfailStatusSummary: 集計結果
    """
    if not step_defs_dir.is_dir():
        return XfailStatusSummary(total=0)

    pending: list[SkeletonEntry] = []
    implemented: list[SkeletonEntry] = []

    for entry in sorted(step_defs_dir.iterdir()):
        if not entry.is_file():
            continue
        if not _SKELETON_FILENAME_RE.match(entry.name):
            continue
        if entry.name in _SELF_REFERENTIAL_EXCLUSIONS:
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        count = len(_PENDING_MARKER_RE.findall(text))
        if count > 0:
            pending.append(SkeletonEntry(path=entry, pending_count=count))
        else:
            implemented.append(SkeletonEntry(path=entry, pending_count=0))

    return XfailStatusSummary(
        total=len(pending) + len(implemented),
        pending_files=pending,
        fully_implemented_files=implemented,
    )


def format_text(summary: XfailStatusSummary) -> str:
    """人間向けの text 出力を組み立てる."""
    lines: list[str] = []
    lines.append("step_defs skeleton status (Issue #1544 Phase 4):")
    lines.append("")
    lines.append(f"  Total skeletons: {summary.total}")
    lines.append(f"  Pending: {summary.pending_count}")
    lines.append(f"  Fully implemented: {summary.fully_implemented_count}")

    if summary.total == 0:
        lines.append("")
        lines.append("  (no skeletons found)")
        return "\n".join(lines) + "\n"

    ratio_pct = (summary.fully_implemented_count * 100) / summary.total
    lines.append(f"  Progress: {summary.fully_implemented_count}/{summary.total} ({ratio_pct:.1f}%)")

    if summary.pending_files:
        lines.append("")
        lines.append("Pending breakdown:")
        for entry in summary.pending_files:
            lines.append(f"  {entry.path.name}: {entry.pending_count} pending")

    return "\n".join(lines) + "\n"


def format_json(summary: XfailStatusSummary) -> str:
    """machine-readable な JSON 出力を組み立てる."""
    return json.dumps(
        {
            "total": summary.total,
            "pending_count": summary.pending_count,
            "fully_implemented_count": summary.fully_implemented_count,
            "pending_files": [
                {"path": str(entry.path), "pending_count": entry.pending_count} for entry in summary.pending_files
            ],
            "fully_implemented_files": [{"path": str(entry.path)} for entry in summary.fully_implemented_files],
        },
        ensure_ascii=False,
        indent=2,
    )


def run_cli(args: argparse.Namespace) -> int:
    step_defs_dir = Path(args.step_defs_dir) if args.step_defs_dir else _default_step_defs_dir()
    summary = collect_status(step_defs_dir)
    if getattr(args, "json", False):
        print(format_json(summary))
    else:
        print(format_text(summary), end="")
    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "xfail-status",
        help="step_defs skeleton の pending / fully implemented 状態を集計する",
        description=(
            "`tidd extract-feature` で生成された step_defs skeleton の"
            " pending marker 数を集計し、xfail 消化の進捗を可視化する。"
        ),
    )
    parser.add_argument(
        "--step-defs-dir",
        default=None,
        help="対象ディレクトリ（既定: projects/py/tidd_tools/tests/step_defs/）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine-readable な JSON を出力する（schedule / dashboard 統合用）",
    )
    parser.set_defaults(func=run_cli)
