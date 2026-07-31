"""Tests for price-derived momentum, volatility and drawdown.

Split from the network fetch so the maths is verifiable without hitting an
external API — these feed the momentum and stability factors in scoring.
"""

from __future__ import annotations

import pytest

from app.sources import _metrics_from_closes


def _series(*, start: float, days: int, daily_growth: float) -> list[float]:
    price = start
    out = [price]
    for _ in range(days - 1):
        price *= 1 + daily_growth
        out.append(price)
    return out


class TestMetricsFromCloses:
    def test_steady_rise_gives_positive_returns(self):
        m = _metrics_from_closes(_series(start=100, days=260, daily_growth=0.001))
        assert m.return_1m > 0
        assert m.return_1y > m.return_1m  # longer window captures more of the trend

    def test_steady_fall_gives_negative_returns(self):
        m = _metrics_from_closes(_series(start=100, days=260, daily_growth=-0.001))
        assert m.return_1m < 0
        assert m.return_1y < 0

    def test_flat_series_has_no_volatility_and_no_drawdown(self):
        m = _metrics_from_closes([100.0] * 260)
        assert m.volatility_1y == pytest.approx(0.0, abs=1e-9)
        assert m.drawdown_1y == pytest.approx(0.0, abs=1e-9)
        assert m.return_1y == pytest.approx(0.0, abs=1e-9)

    def test_drawdown_measures_peak_to_trough(self):
        # Rises to 200, falls to 150: worst drawdown is -25% from the peak.
        m = _metrics_from_closes([100.0, 200.0, 150.0] + [150.0] * 20)
        assert m.drawdown_1y == pytest.approx(-0.25, abs=1e-6)

    def test_drawdown_is_never_positive(self):
        m = _metrics_from_closes(_series(start=100, days=100, daily_growth=0.002))
        assert m.drawdown_1y <= 0

    def test_a_choppier_series_is_more_volatile(self):
        calm = _metrics_from_closes([100 + (i % 2) * 0.5 for i in range(260)])
        wild = _metrics_from_closes([100 + (i % 2) * 20.0 for i in range(260)])
        assert wild.volatility_1y > calm.volatility_1y

    def test_short_windows_return_none_rather_than_guessing(self):
        m = _metrics_from_closes([100.0] * 30)
        assert m.return_1m is not None  # 30 sessions covers a month
        assert m.return_6m is None  # but not six
        assert m.return_1y is None

    def test_last_close_is_the_most_recent_price(self):
        assert _metrics_from_closes([10.0, 20.0, 30.0] * 10).last_close == 30.0
