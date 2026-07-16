#!/usr/bin/env python3
"""PreToolUse hook: `gh pr create` 前に TDD の RED-GREEN 順序を機械強制する（Issue #1285）.

**背景（監査 §1-2）:** `.claude/rules/workflow.md:221` の RED 手順は「テストが失敗することを
確認する」の 1 行だけで、誰が・どこに記録するか未定義。PR 中央値マージ 12 分の速度は
TDG（テスト先行）を skip している疑いがあり、順序を機械強制しないと TDD の
RED-GREEN-REFACTOR サイクルが崩れる。

**動作:**

- `gh pr create` の Bash 呼び出しを検知
- `git log origin/main..HEAD` で PR 内の全 commit を解析
- 実装ファイル（`projects/py/*/src/**` / `projects/gas/*/Code.js`）の最古 commit が
  テストファイル（`projects/py/*/tests/**` / `projects/gas/*/tests/**`）の最古 commit
  より前なら **exit 2 でブロック**
- 単一 commit で両方が混在している場合も違反扱いにする
- `type: docs` / `research` / `refactor` / `ci` / `build` はブランチ名前綴で skip
- PR ボディに ``<!-- allow-single-commit: <理由> -->`` があれば bypass する

stdlib のみ使用。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.bypass_audit import record_bypass as _record_bypass  # noqa: E402
from _lib.hook_io import (  # noqa: E402
    get_command,
    get_tool_name,
    is_hook_enabled,
    read_hook_input,
)
from _lib.override_markers import (  # noqa: E402
    extract_reason,
    find_invalid_syntax,
    has_override_marker,
)

_GH_PR_CREATE_RE = re.compile(r"(^|&&|;|\|)\s*gh pr create(\s|$)")
_SKIP_BRANCH_PREFIXES = {"docs", "research", "refactor", "ci", "build", "chore"}

# 実装ファイル判定
_IMPL_PATTERNS = [
    re.compile(r"^projects/py/[^/]+/src/"),
    re.compile(r"^projects/gas/[^/]+/(?!tests/)"),
    re.compile(r"^\.claude/hooks/(?!_lib/)"),
    re.compile(r"^\.claude/agents/"),
]
# テストファイル判定
_TEST_PATTERNS = [
    re.compile(r"^projects/py/[^/]+/tests/"),
    re.compile(r"^projects/gas/[^/]+/tests/"),
]


def _current_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _extract_body_from_command(command: str) -> str:
    """`gh pr create` の PR body を可能な限り展開して返す（best-effort）.

    以下の形式を扱う:
    - `--body '...'` / `--body "..."` / `--body word`
    - `--body-file <path>` → ファイルから読む
    - `--body "$(cat <<'EOF' ... EOF)"` / heredoc → コマンド内 heredoc を掘り出す
    """
    # --body-file <path>
    file_match = re.search(r"--body-file\s+(\S+)", command)
    if file_match:
        body_path = Path(file_match.group(1))
        try:
            return body_path.read_text(encoding="utf-8")
        except OSError:
            return ""
    # heredoc: <<'MARKER' ... MARKER
    heredoc_match = re.search(r"<<'?(\w+)'?\s*(.*?)\n\s*\1(?:\s|$)", command, re.DOTALL)
    if heredoc_match:
        return heredoc_match.group(2)
    # --body '...' / "..." / word
    m = re.search(r"--body\s+(?:'([^']*)'|\"([^\"]*)\"|(\S+))", command)
    if m:
        return m.group(1) or m.group(2) or m.group(3) or ""
    return ""


def _classify_file(path: str) -> str | None:
    """ファイルパスを 'impl' or 'test' に分類する（該当しなければ None）."""
    for pattern in _TEST_PATTERNS:
        if pattern.match(path):
            return "test"
    for pattern in _IMPL_PATTERNS:
        if pattern.match(path):
            return "impl"
    return None


def _get_commits_with_files() -> list[tuple[str, set[str]]]:
    """origin/main..HEAD の各 commit と含まれるファイルパスセットを返す（古い順）."""
    try:
        result = subprocess.run(
            ["git", "log", "--reverse", "--format=%H", "origin/main..HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
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
            )
        except subprocess.TimeoutExpired:
            continue
        files = {p for p in files_proc.stdout.strip().splitlines() if p}
        commits.append((sha, files))
    return commits


def _get_file_first_add_commit(file_path: str) -> str | None:
    """Issue #1458: 指定 file が最初に追加された commit hash を返す（origin/main..HEAD 範囲）.

    ``git log --reverse --diff-filter=A --format=%H origin/main..HEAD -- <file>`` の
    先頭 1 行を返す。cherry-pick / rebase 後も「その file が初めて追加された commit」を
    正確に特定できる。file が origin/main で既に存在する（新規追加でない）場合は None。
    """
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--reverse",
                "--diff-filter=A",
                "--format=%H",
                "origin/main..HEAD",
                "--",
                file_path,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
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


def _get_commit_index_map() -> dict[str, int]:
    """origin/main..HEAD の commit hash → 順序 index (古い順) の map を返す."""
    try:
        result = subprocess.run(
            ["git", "log", "--reverse", "--format=%H", "origin/main..HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
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


def _check_tdd_order() -> str | None:
    """RED-GREEN 順序をチェックし、違反があればエラーメッセージを返す（正常時は None）."""
    commits = _get_commits_with_files()
    if not commits:
        return None  # PR に commit なし → skip

    # 各 commit の分類を計算
    impl_commit_idx: int | None = None
    test_commit_idx: int | None = None
    mixed_commits: list[int] = []
    for idx, (_sha, files) in enumerate(commits):
        classifications = {_classify_file(f) for f in files}
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
    if impl_commit_idx < test_commit_idx:
        return (
            f"テストファイルより先に実装ファイルをコミットしています"
            f"（実装 {impl_commit_idx + 1} 番目コミット、テスト {test_commit_idx + 1} 番目コミット）。\n"
            "先にテストのみをコミットしてから実装を追加してください。\n"
            "（テストを先にコミットすると、実装前にテストが失敗することを記録できます）"
        )

    # Issue #1458: file-per-file 初出 commit 追跡（cherry-pick / rebase 回避検知）
    # 全 impl file と test file を集めて、`--diff-filter=A` で「初めて追加された commit」を特定。
    # 追加された commit の順序 index が impl < test なら違反。
    idx_map = _get_commit_index_map()
    impl_files: set[str] = set()
    test_files: set[str] = set()
    for _sha, files in commits:
        for f in files:
            cls = _classify_file(f)
            if cls == "impl":
                impl_files.add(f)
            elif cls == "test":
                test_files.add(f)

    earliest_impl_idx: int | None = None
    earliest_impl_file: str | None = None
    for f in impl_files:
        sha = _get_file_first_add_commit(f)
        if sha is None:
            continue  # origin/main で既存
        i = idx_map.get(sha)
        if i is None:
            continue
        if earliest_impl_idx is None or i < earliest_impl_idx:
            earliest_impl_idx = i
            earliest_impl_file = f

    earliest_test_idx: int | None = None
    earliest_test_file: str | None = None
    for f in test_files:
        sha = _get_file_first_add_commit(f)
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


def _main() -> int:
    payload = read_hook_input(hook_name="PreToolUse")  # Issue #1364
    if get_tool_name(payload) != "Bash":
        return 0

    command = get_command(payload)
    if not _GH_PR_CREATE_RE.search(command):
        return 0

    # branch prefix で skip 対象を判定
    branch = _current_branch()
    prefix = branch.split("/", 1)[0] if branch else ""
    if prefix in _SKIP_BRANCH_PREFIXES:
        return 0

    # Issue #1460: 共通 helper で PR body の allow-single-commit マーカー bypass 判定
    body = _extract_body_from_command(command)
    invalid_markers = find_invalid_syntax(body, ["allow-single-commit"])
    if invalid_markers:
        sys.stderr.write(
            f"Blocked: override marker の書式が不正です: "
            f"{', '.join(invalid_markers)}\n"
            "正しい書式: <!-- allow-single-commit: <理由> -->\n"
        )
        return 2
    if has_override_marker(body, "allow-single-commit"):
        # Issue #1625: バイパス使用を audit log に記録
        reason = extract_reason(body, "allow-single-commit")
        _record_bypass(event="allow-single-commit", reason=reason)
        return 0
    # Issue #1458: commit-order marker で cherry-pick / rebase 経由の順序改竄 bypass
    if has_override_marker(body, "commit-order"):
        return 0

    error = _check_tdd_order()
    if error is None:
        return 0

    sys.stderr.write(
        f"require-red-first.py: {error}\n"
        "分割不能な正当な理由がある場合は、PR 本文（PR ボディ）に以下を追加してください:\n"
        "  <!-- allow-single-commit: <理由> -->\n"
        "詳細: docs/reference/hooks.md#require-red-firstpy\n"
    )
    return 2


def main() -> int:
    # Issue #1633: hook 機能別 on/off
    if not is_hook_enabled("require-red-first"):
        return 0
    return _main()


if __name__ == "__main__":
    sys.exit(main())
