"""配布物（templates/workflow/）内の docs/ パス参照が配布済みか検証する（Issue #4039）.

`templates/workflow/` 配下の hook・rule・skill・agent 定義・README・config テンプレート
（`.mise.toml.example.jinja` 等・#4043 で走査対象に追加）から参照される `docs/` パスの
うち、`templates/workflow/docs/` に実在しないもの（consumer に配布されていない）は
consumer 側の AI エージェントが手順を追えなくなる原因になる（#4017 の棚卸しで判明）。

既存 150 箇所を文言修正で一度に解消するのは非現実的なため、baseline データファイル
（`projects/py/tidd_tools/src/tidd_tools/data/docs-reference-baseline.yaml`）に既知の
未解消参照を登録し、baseline 記載分は許容しつつ baseline 外の新規参照のみを検出する。
解消 Issue は baseline から対象行を削って進める。

`templates/workflow/` が存在しない consumer 文脈では no-op（#3982 の判断を踏襲）。

**逆方向の誤分類検出（Issue #4070）:** 「ai-dev-handbook 本体の docs/... 配下・`<file>`
（consumer 未配布）」形式の注記は本来「`templates/workflow/docs/` に配布されていない
資料」を指すためのものだが、実際には配布済みの docs（`.md.jinja` として配布され
consumer 側で `.md` にレンダリングされるものを含む）へこの注記を誤って使うと、consumer
側で手元に存在するファイルへの参照先を見失う（#4043・#4042 の書き換えで実例あり）。
`find_misclassified_distributed_references` はこの逆方向の誤分類を検出する。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import yaml

#: 走査対象ディレクトリ（repo_root からの相対パス）。
#: 「hook・rule・skill・agent 定義」に対応する4カテゴリのみを対象とする。
#: `.codex/` は `tidd sync-template` で `.claude/` から同期される複製のため対象外
#: （二重カウント回避）。
_SCAN_DIRS: tuple[str, ...] = (
    "templates/workflow/.claude/hooks",
    "templates/workflow/.claude/rules",
    "templates/workflow/.claude/skills",
    "templates/workflow/.claude/agents",
)

#: 走査対象ファイル（repo_root からの相対パス）。README・config テンプレートは
#: ディレクトリではなく個別ファイル単位で配布されるため `_SCAN_DIRS` とは別管理にする
#: （Issue #4043）。
_SCAN_FILES: tuple[str, ...] = (
    "templates/workflow/README.md",
    "templates/workflow/.mise.toml.example.jinja",
    "templates/workflow/.pre-commit-config.yaml.jinja",
    "templates/workflow/.coderabbit.yaml.jinja",
    "templates/workflow/mise-worktree-bridge.bash",
)

_TEMPLATE_ROOT_RELPATH = "templates/workflow"
_BASELINE_RELPATH = "projects/py/tidd_tools/src/tidd_tools/data/docs-reference-baseline.yaml"

#: `docs/foo/bar.md` 形式の相対パス参照を抽出する（先頭 `../` 等の相対プレフィックスは
#: 実際の参照に出現しないため対象外・調査済み）。
_DOC_PATH_PATTERN = re.compile(r"docs/[A-Za-z0-9_./-]+\.md")

#: 「本体の docs/xxx/ 配下・`<file>`」形式（consumer 未配布の注記に使う定型句）の参照を
#: 抽出する（Issue #4070）。`<file>` に `#anchor` が付く場合は呼び出し側で除去する。
_UPSTREAM_ONLY_PATTERN = re.compile(r"本体の (docs/[A-Za-z0-9_/-]+/) 配下・`([^`]+)`")


def load_baseline(repo_root: Path) -> set[str]:
    """baseline データファイルに登録済みの docs/ 相対パス集合を返す.

    ファイル未存在・空・不正形式の場合は空集合を返す（fail-open。baseline ファイルが
    読めなくてもチェック自体は動作させる方針。ただし fail-open な分、baseline が壊れて
    いると全未配布参照がブロック対象になる）。
    """
    baseline_path = repo_root / _BASELINE_RELPATH
    if not baseline_path.is_file():
        return set()
    try:
        data = yaml.safe_load(baseline_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return set()
    if not isinstance(data, dict):
        return set()
    paths = data.get("baseline", [])
    if not isinstance(paths, list):
        return set()
    return {str(p) for p in paths}


def _iter_scan_texts(repo_root: Path) -> Iterator[str]:
    """`_SCAN_DIRS` + `_SCAN_FILES`（hook・rule・skill・agent 定義・README・config テンプレート）
    配下のファイル内容を走査順に yield する（Issue #4039・#4043 の走査対象を単一化）."""
    for scan_dir in _SCAN_DIRS:
        target = repo_root / scan_dir
        if not target.is_dir():
            continue
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            try:
                yield path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
    for scan_file in _SCAN_FILES:
        target_file = repo_root / scan_file
        if not target_file.is_file():
            continue
        try:
            yield target_file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def scan_referenced_docs_paths(repo_root: Path) -> set[str]:
    """`templates/workflow/` の hook・rule・skill・agent 定義・README・config テンプレートから
    参照される docs/ パス集合を返す（Issue #4039・README・config は #4043 で追加）."""
    refs: set[str] = set()
    for text in _iter_scan_texts(repo_root):
        refs.update(_DOC_PATH_PATTERN.findall(text))
    return refs


def scan_upstream_only_references(repo_root: Path) -> set[str]:
    """「本体の docs/xxx/ 配下・`<file>`」形式で参照される docs/ パス集合を返す（Issue #4070）.

    この定型句は「`templates/workflow/docs/` に配布されていない資料」を指すために使う
    ものだが、実際に配布済みの docs へ誤って使われていないかは
    `find_misclassified_distributed_references` で検証する。
    """
    refs: set[str] = set()
    for text in _iter_scan_texts(repo_root):
        for dir_part, file_part in _UPSTREAM_ONLY_PATTERN.findall(text):
            refs.add(dir_part + file_part.split("#", 1)[0])
    return refs


def _is_distributed(template_root: Path, ref: str) -> bool:
    """`ref`（`docs/xxx/foo.md` 形式）が `template_root` 配下に配布済みかを判定する.

    `_templates_suffix: .jinja`（copier.yml）により `foo.md.jinja` は consumer 側で
    `foo.md` としてレンダリングされるため、`.jinja` 付きの実体も配布済みとして扱う
    （Issue #4070）。
    """
    if (template_root / ref).is_file():
        return True
    return (template_root / f"{ref}.jinja").is_file()


def find_new_undistributed_references(repo_root: Path) -> list[str]:
    """baseline 未登録かつ未配布の docs/ パス参照一覧を返す（ソート済み）.

    `templates/workflow/` が存在しない consumer 文脈では空リストを返す（no-op）。
    """
    template_root = repo_root / _TEMPLATE_ROOT_RELPATH
    if not template_root.is_dir():
        return []

    baseline = load_baseline(repo_root)
    referenced = scan_referenced_docs_paths(repo_root)

    undistributed: list[str] = []
    for ref in sorted(referenced):
        if _is_distributed(template_root, ref):
            continue
        if ref in baseline:
            continue
        undistributed.append(ref)
    return undistributed


def find_misclassified_distributed_references(repo_root: Path) -> list[str]:
    """配布済みにもかかわらず「本体の...配下（consumer 未配布）」と誤注記された docs/ パス
    一覧を返す（ソート済み・Issue #4070）.

    `templates/workflow/` が存在しない consumer 文脈では空リストを返す（no-op）。
    """
    template_root = repo_root / _TEMPLATE_ROOT_RELPATH
    if not template_root.is_dir():
        return []

    upstream_only = scan_upstream_only_references(repo_root)
    return sorted(ref for ref in upstream_only if _is_distributed(template_root, ref))
