"""TDD RED-first 順序判定ロジックの単一の真実源（Issue #2895）.

`.claude/hooks/require-red-first.py`（PreToolUse hook）と `tidd tdd-check` CLI
（`projects/py/tidd_tools/src/tidd_tools/tdd_check.py`）の両方から動的 import で
参照される。判定ロジック（ファイルパス分類・ブランチ prefix skip 対象・commit 順序判定）は
必ずここに定義し、呼び出し側で再定義してはならない（判定ロジックが乖離すると
prose 手動近似と同じ問題が再発する。Issue #2874 の PR #2886 誤 park が実例）。

`evaluate_tdd_order()` は commit 一覧と lookup 関数を引数で受け取る純粋関数として実装し、
git subprocess 呼び出し（`get_commits_with_files` 等）とロジック本体を分離している。
これにより呼び出し元（hook）は自身のラッパー関数（`_get_commits_with_files` 等）を
そのままテストで mock 可能な状態に保てる。

stdlib のみ使用。
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

# ブランチ名 prefix による skip 対象（require-red-first.py 自体が RED-first を免除する）
SKIP_BRANCH_PREFIXES = {"docs", "research", "refactor", "ci", "build", "chore"}

# 実装ファイル判定
IMPL_PATTERNS = [
    re.compile(r"^projects/py/[^/]+/src/"),
    re.compile(r"^projects/gas/[^/]+/(?!tests/)"),
    re.compile(r"^\.claude/hooks/(?!_lib/)"),
    re.compile(r"^\.claude/agents/"),
]
# テストファイル判定
TEST_PATTERNS = [
    re.compile(r"^projects/py/[^/]+/tests/"),
    re.compile(r"^projects/gas/[^/]+/tests/"),
]


def classify_file(path: str) -> str | None:
    """ファイルパスを 'impl' or 'test' に分類する（該当しなければ None）."""
    for pattern in TEST_PATTERNS:
        if pattern.match(path):
            return "test"
    for pattern in IMPL_PATTERNS:
        if pattern.match(path):
            return "impl"
    return None


def get_commits_with_files(
    base_ref: str = "origin/main",
    head_ref: str = "HEAD",
    *,
    cwd: str | None = None,
) -> list[tuple[str, set[str]]]:
    """`<base_ref>..<head_ref>` の各 commit と含まれるファイルパスセットを返す（古い順）."""
    try:
        result = subprocess.run(
            ["git", "log", "--reverse", "--format=%H", f"{base_ref}..{head_ref}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            cwd=cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    commits: list[tuple[str, set[str]]] = []
    for line in result.stdout.strip().splitlines():
        sha = line.strip()
        if not sha:
            continue
        try:
            files_proc = subprocess.run(
                ["git", "show", "--name-only", "--format=", sha],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            continue
        files = {p for p in files_proc.stdout.strip().splitlines() if p}
        commits.append((sha, files))
    return commits


def get_file_first_add_commit(
    file_path: str,
    base_ref: str = "origin/main",
    head_ref: str = "HEAD",
    *,
    cwd: str | None = None,
) -> str | None:
    """指定 file が最初に追加された commit hash を返す（`<base_ref>..<head_ref>` 範囲）.

    ``git log --reverse --diff-filter=A --format=%H <base_ref>..<head_ref> -- <file>`` の
    先頭 1 行を返す。cherry-pick / rebase 後も「その file が初めて追加された commit」を
    正確に特定できる。file が base_ref で既に存在する（新規追加でない）場合は None。
    """
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--reverse",
                "--diff-filter=A",
                "--format=%H",
                f"{base_ref}..{head_ref}",
                "--",
                file_path,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            cwd=cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.strip().splitlines():
        sha = line.strip()
        if sha:
            return sha
    return None


def get_commit_index_map(
    base_ref: str = "origin/main",
    head_ref: str = "HEAD",
    *,
    cwd: str | None = None,
) -> dict[str, int]:
    """`<base_ref>..<head_ref>` の commit hash → 順序 index (古い順) の map を返す."""
    try:
        result = subprocess.run(
            ["git", "log", "--reverse", "--format=%H", f"{base_ref}..{head_ref}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            cwd=cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    idx_map: dict[str, int] = {}
    for i, line in enumerate(result.stdout.strip().splitlines()):
        sha = line.strip()
        if sha:
            idx_map[sha] = i
    return idx_map


def evaluate_tdd_order(
    commits: list[tuple[str, set[str]]],
    *,
    get_commit_index_map: Callable[[], dict[str, int]],
    get_file_first_add_commit: Callable[[str], str | None],
) -> str | None:
    """commits データと注入された lookup 関数から RED-GREEN 順序を判定する（純粋ロジック）.

    Args:
        commits: `(sha, files)` のタプルリスト（古い順）。
        get_commit_index_map: 引数なしで呼び出すと commit hash → 順序 index の map を返す関数。
        get_file_first_add_commit: file path を渡すと最初に追加された commit hash を返す関数。

    Returns:
        None: 違反なし。str: 違反理由（日本語メッセージ）。
    """
    if not commits:
        return None  # commit なし → skip

    # 各 commit の分類を計算
    impl_commit_idx: int | None = None
    test_commit_idx: int | None = None
    mixed_commits: list[int] = []
    for idx, (_sha, files) in enumerate(commits):
        classifications = {classify_file(f) for f in files}
        classifications.discard(None)
        has_impl = "impl" in classifications
        has_test = "test" in classifications
        if has_impl and has_test:
            mixed_commits.append(idx)
        if has_impl and impl_commit_idx is None:
            impl_commit_idx = idx
        if has_test and test_commit_idx is None:
            test_commit_idx = idx

    # 実装ファイルもテストファイルも触っていない → skip（docs 等）
    if impl_commit_idx is None and test_commit_idx is None:
        return None

    # 実装のみで tests 変更がない → 独立実装 → skip（既存テストで検証済みの想定）
    if impl_commit_idx is not None and test_commit_idx is None:
        return None

    # テストのみで実装なし → skip（テスト追加のみの PR）
    if impl_commit_idx is None:
        return None

    # 単一 commit で impl + test 混在 → 違反（TDD 順序が判定不能）
    if mixed_commits and (impl_commit_idx == test_commit_idx == mixed_commits[0]):
        return (
            "テストファイルと実装ファイルを 1 つのコミットに同時に追加しています。\n"
            "先にテストのみをコミットしてから、次のコミットで実装を追加してください。\n"
            "（テストを先にコミットすると、実装前にテストが失敗することを記録できます）"
        )

    # test より前に impl が来ている → 違反
    if (
        impl_commit_idx is not None
        and test_commit_idx is not None
        and impl_commit_idx < test_commit_idx
    ):
        return (
            f"テストファイルより先に実装ファイルをコミットしています"
            f"（実装 {impl_commit_idx + 1} 番目コミット、テスト {test_commit_idx + 1} 番目コミット）。\n"
            "先にテストのみをコミットしてから実装を追加してください。\n"
            "（テストを先にコミットすると、実装前にテストが失敗することを記録できます）"
        )

    # Issue #1458: file-per-file 初出 commit 追跡（cherry-pick / rebase 回避検知）
    # 全 impl file と test file を集めて、`--diff-filter=A` で「初めて追加された commit」を特定。
    # 追加された commit の順序 index が impl < test なら違反。
    idx_map = get_commit_index_map()
    impl_files: set[str] = set()
    test_files: set[str] = set()
    for _sha, files in commits:
        for f in files:
            cls = classify_file(f)
            if cls == "impl":
                impl_files.add(f)
            elif cls == "test":
                test_files.add(f)

    earliest_impl_idx: int | None = None
    earliest_impl_file: str | None = None
    for f in impl_files:
        sha = get_file_first_add_commit(f)
        if sha is None:
            continue  # base_ref で既存
        i = idx_map.get(sha)
        if i is None:
            continue
        if earliest_impl_idx is None or i < earliest_impl_idx:
            earliest_impl_idx = i
            earliest_impl_file = f

    earliest_test_idx: int | None = None
    earliest_test_file: str | None = None
    for f in test_files:
        sha = get_file_first_add_commit(f)
        if sha is None:
            continue
        i = idx_map.get(sha)
        if i is None:
            continue
        if earliest_test_idx is None or i < earliest_test_idx:
            earliest_test_idx = i
            earliest_test_file = f

    if (
        earliest_impl_idx is not None
        and earliest_test_idx is not None
        and earliest_impl_idx < earliest_test_idx
    ):
        return (
            f"テストファイルより先に実装ファイルをコミットしています"
            f"（実装ファイル: {earliest_impl_file!r}、テストファイル: {earliest_test_file!r}）。\n"
            "コミットの順序を入れ替えても検知します。\n"
            "先にテストのみをコミットしてから実装ファイルを追加してください。\n"
            "（テストを先にコミットすると、実装前にテストが失敗することを記録できます）\n"
            "どうしても分割不能な場合は PR 本文（PR ボディ）に `<!-- allow-single-commit: <理由> -->` "
            "または `<!-- commit-order: -->` を追加してください。"
        )

    return None


def check_tdd_order(
    base_ref: str = "origin/main",
    head_ref: str = "HEAD",
    *,
    cwd: str | None = None,
) -> str | None:
    """CLI 向け完結版: commits を収集してから `evaluate_tdd_order` に委譲する.

    `tidd tdd-check` CLI から利用する想定（hook 側は自前の mock 可能なラッパー関数
    経由で `evaluate_tdd_order` を直接呼び出すため、本関数は使わない）。
    """
    commits = get_commits_with_files(base_ref, head_ref, cwd=cwd)
    return evaluate_tdd_order(
        commits,
        get_commit_index_map=lambda: get_commit_index_map(base_ref, head_ref, cwd=cwd),
        get_file_first_add_commit=lambda f: get_file_first_add_commit(
            f, base_ref, head_ref, cwd=cwd
        ),
    )
