"""ポジションサイズ計算."""

from __future__ import annotations


def position_size(*, capital: float, risk_pct: float, entry: float, stop: float) -> int:
    """1 トレードの許容リスク額から発注数量を計算する（端数切り捨て）.

    Args:
        capital: 口座資金。
        risk_pct: 1 トレードで許容するリスク（% 表記。1.0 = 資金の 1%）。
        entry: エントリー価格。
        stop: 損切り価格。

    Returns:
        発注数量（`資金 × リスク% ÷ 損切り幅` の切り捨て整数）。

    Raises:
        ValueError: entry と stop が同値で損切り幅がゼロの場合。
    """
    stop_distance = abs(entry - stop)
    if stop_distance == 0:
        raise ValueError("stop は entry と異なる価格を指定してください（損切り幅がゼロ）")
    risk_amount = capital * risk_pct / 100
    return int(risk_amount / stop_distance)
