"""position.py のテスト."""

from __future__ import annotations

import pytest

from trade_scripts.position import position_size

pytestmark = pytest.mark.target_position


class TestPositionSize:
    def test_risk_based_sizing(self) -> None:
        """資金 100 万・リスク 1%・エントリー 1000 円・損切り 950 円 → 200 株."""
        assert position_size(capital=1_000_000, risk_pct=1.0, entry=1000, stop=950) == 200

    def test_short_position_uses_absolute_stop_distance(self) -> None:
        """ショート（stop > entry）でも損切り幅の絶対値で計算される."""
        assert position_size(capital=1_000_000, risk_pct=1.0, entry=950, stop=1000) == 200

    def test_fractional_result_rounds_down(self) -> None:
        """端数は切り捨て（過大リスクを取らない側に倒す）."""
        assert position_size(capital=100_000, risk_pct=1.0, entry=1000, stop=970) == 33

    def test_stop_equal_to_entry_raises(self) -> None:
        """損切り幅ゼロはリスク計算不能のため ValueError."""
        with pytest.raises(ValueError, match="stop"):
            position_size(capital=1_000_000, risk_pct=1.0, entry=1000, stop=1000)
