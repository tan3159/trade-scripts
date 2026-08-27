"""Coverage 集計ヘルパー（Issue #1287）.

`pytest --cov --cov-report=xml` が生成する ``coverage.xml`` を stdlib のみでパースし、
line coverage / branch coverage を計算する。

**利用者:**

- ``analyze_performance.py``: 週次〜月次レポートに coverage 列を追加
- CI ジョブ: fail_under 判定は pytest-cov 側で行うが、レポート集計は本モジュールが担当

**設計判断:**

- coverage.py 4.0+ の XML フォーマット（Cobertura 互換）に準拠
- stdlib xml.etree で十分（PyYAML 等の依存追加不要）
- 見つからない場合は None を返して呼び出し側でフォールバック
"""

from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET
from pathlib import Path


@dataclasses.dataclass
class CoverageSummary:
    """coverage.xml のサマリ.

    Attributes:
        line_rate: 0.0〜1.0 の line coverage rate（Cobertura の line-rate 属性）
        branch_rate: 0.0〜1.0 の branch coverage rate
        lines_covered: covered lines 数
        lines_valid: 有効な line 数
    """

    line_rate: float
    branch_rate: float
    lines_covered: int
    lines_valid: int

    def line_pct(self) -> float:
        return self.line_rate * 100.0

    def branch_pct(self) -> float:
        return self.branch_rate * 100.0


def parse_coverage_xml(xml_path: Path) -> CoverageSummary | None:
    """coverage.xml をパースして CoverageSummary を返す.

    Returns:
        パース成功なら CoverageSummary、失敗（file なし・XML 不正）なら None。
    """
    if not xml_path.is_file():
        return None
    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError:
        return None
    root = tree.getroot()
    # Cobertura XML root は <coverage line-rate="..." branch-rate="..." lines-covered="..." lines-valid="..." />
    try:
        line_rate = float(root.get("line-rate", "0"))
        branch_rate = float(root.get("branch-rate", "0"))
        lines_covered = int(root.get("lines-covered", "0"))
        lines_valid = int(root.get("lines-valid", "0"))
    except (TypeError, ValueError):
        return None
    return CoverageSummary(
        line_rate=line_rate,
        branch_rate=branch_rate,
        lines_covered=lines_covered,
        lines_valid=lines_valid,
    )


def format_summary_row(summary: CoverageSummary | None) -> str:
    """analyze-performance の週次〜月次レポート用の 1 行を返す."""
    if summary is None:
        return "coverage: (no data)"
    return (
        f"coverage: line={summary.line_pct():.1f}% "
        f"branch={summary.branch_pct():.1f}% "
        f"({summary.lines_covered}/{summary.lines_valid} lines)"
    )
