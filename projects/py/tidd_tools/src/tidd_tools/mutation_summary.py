"""Mutation testing 集計ヘルパー（Issue #1286）.

mutmut が生成する JUnit XML report をパースして mutation score を計算する。
stdlib のみで実装。

**利用者:**

- ``analyze_performance.py``: 週次〜月次レポートに mutation score 列を追加
- CI: mutmut-weekly job の後処理（オプション）

**mutmut JUnit XML の構造（3.x 系）:**

.. code-block:: xml

    <testsuites>
      <testsuite name="mutmut" tests="100" failures="20" errors="5" ...>
        ...
      </testsuite>
    </testsuites>

- ``tests`` = 全 mutation 数
- ``failures`` = 検知された mutant 数（テストが fail した = テストが動いている）
- ``errors`` = 実行エラー（equivalent mutant 等）
- 生存 mutant = tests - failures - errors
"""

from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET
from pathlib import Path


@dataclasses.dataclass
class MutationSummary:
    """mutmut JUnit XML のサマリ.

    Attributes:
        total: 実行された mutation 総数
        killed: テストが検知した mutation 数（JUnit failures）
        errors: 実行エラー（equivalent mutant 含む）
        survived: 生存 mutation 数 = total - killed - errors
    """

    total: int
    killed: int
    errors: int
    survived: int

    def score_pct(self) -> float:
        """mutation score = killed / (total - errors) * 100.

        errors（equivalent mutants）を分母から除外する（真の生存率を測るため）。
        分母が 0 なら 0 を返す。
        """
        effective = self.total - self.errors
        if effective <= 0:
            return 0.0
        return (self.killed / effective) * 100.0


def parse_mutation_xml(xml_path: Path) -> MutationSummary | None:
    """mutmut JUnit XML をパースして MutationSummary を返す.

    Returns:
        パース成功なら MutationSummary、失敗なら None（フェイルオープン）。
    """
    if not xml_path.is_file():
        return None
    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError:
        return None
    root = tree.getroot()
    # testsuites 直下の testsuite を集計
    total = 0
    killed = 0
    errors = 0
    suites = root.findall(".//testsuite") if root.tag == "testsuites" else [root]
    for suite in suites:
        try:
            total += int(suite.get("tests", "0"))
            killed += int(suite.get("failures", "0"))
            errors += int(suite.get("errors", "0"))
        except (TypeError, ValueError):
            continue
    if total == 0:
        return None
    survived = max(0, total - killed - errors)
    return MutationSummary(total=total, killed=killed, errors=errors, survived=survived)


def format_summary_row(summary: MutationSummary | None) -> str:
    """analyze-performance レポート用の 1 行を返す."""
    if summary is None:
        return "mutation: (no data)"
    return (
        f"mutation: score={summary.score_pct():.1f}% "
        f"killed={summary.killed}/{summary.total - summary.errors} "
        f"(survived={summary.survived}, errors={summary.errors})"
    )
