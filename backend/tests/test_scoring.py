"""Tests for the deterministic persona scoring path.

This is the logic the brief asks to be "efficient, testable" rather than a
brute-force LLM call per stock — so it gets real assertions.
"""

from __future__ import annotations

import pytest

from app.persona import (
    FACTORS,
    NEUTRAL_WEIGHTS,
    SENTIMENT_TILT,
    Persona,
    check_rules,
    compute_factors,
    score_stock,
)

CONSERVATIVE_DIVIDEND = Persona(
    weights={
        "growth": 0.2,
        "value": 0.6,
        "stability": 0.95,
        "momentum": 0.1,
        "quality": 0.8,
        "income": 0.95,
    },
    rules=[{"type": "rule", "field": "debt_to_equity", "op": "<=", "value": 0.5}],
    facts=["Conservative, dividend-focused, avoids high-debt companies"],
)

GROWTH_SEEKER = Persona(
    weights={
        "growth": 0.95,
        "value": 0.2,
        "stability": 0.2,
        "momentum": 0.9,
        "quality": 0.5,
        "income": 0.05,
    },
)


def _stock(**overrides):
    base = {
        "ticker": "TEST",
        "name": "Test Ltd",
        "pe": 25.0,
        "pb": 3.0,
        "roe": 15.0,
        "roce": 18.0,
        "debt_to_equity": 0.4,
        "dividend_yield": 1.5,
        "market_cap_cr": 50000.0,
        "promoter_holding": 50.0,
        "current_price": 1000.0,
        "sales_growth_3y": 12.0,
        "profit_growth_3y": 14.0,
        "return_3m": 0.05,
        "return_6m": 0.10,
        "return_1y": 0.15,
        "volatility_1y": 0.30,
        "drawdown_1y": -0.15,
        "sentiment": 0.0,
        "sentiment_confidence": 0.0,
    }
    base.update(overrides)
    return base


class TestComputeFactors:
    def test_all_factors_present_and_bounded(self):
        factors = compute_factors(_stock())
        assert set(factors) == set(FACTORS)
        assert all(0.0 <= v <= 1.0 for v in factors.values())

    def test_low_debt_scores_more_stable_than_high_debt(self):
        safe = compute_factors(_stock(debt_to_equity=0.05, volatility_1y=0.18))
        risky = compute_factors(_stock(debt_to_equity=2.4, volatility_1y=0.70))
        assert safe["stability"] > risky["stability"]

    def test_cheap_multiples_score_as_better_value(self):
        cheap = compute_factors(_stock(pe=8.0, pb=1.0))
        expensive = compute_factors(_stock(pe=75.0, pb=14.0))
        assert cheap["value"] > expensive["value"]

    def test_higher_dividend_yield_scores_more_income(self):
        payer = compute_factors(_stock(dividend_yield=5.5))
        hoarder = compute_factors(_stock(dividend_yield=0.0))
        assert payer["income"] > hoarder["income"]

    def test_missing_data_falls_back_to_neutral_not_zero(self):
        """A thinly-covered stock must not be punished for absent data."""
        blank = compute_factors(
            {"ticker": "X", "name": "X", "pe": None, "pb": None, "dividend_yield": None}
        )
        assert blank["value"] == pytest.approx(0.5)
        assert blank["income"] == pytest.approx(0.5)

    def test_extreme_values_are_clamped_not_extrapolated(self):
        absurd = compute_factors(_stock(roe=100000.0, pe=-50.0))
        assert 0.0 <= absurd["quality"] <= 1.0
        assert 0.0 <= absurd["value"] <= 1.0


class TestHardRules:
    def test_rule_excludes_violating_stock(self):
        violation = check_rules(_stock(debt_to_equity=1.8), CONSERVATIVE_DIVIDEND.rules)
        assert violation is not None
        assert "debt_to_equity" in violation

    def test_rule_passes_compliant_stock(self):
        assert check_rules(_stock(debt_to_equity=0.2), CONSERVATIVE_DIVIDEND.rules) is None

    def test_missing_field_does_not_exclude(self):
        """A data gap is a data gap, not grounds to hide a stock."""
        assert check_rules(_stock(debt_to_equity=None), CONSERVATIVE_DIVIDEND.rules) is None

    def test_unknown_field_is_ignored_rather_than_applied(self):
        """Rules originate from LLM output, so unrecognised ones must not bite."""
        rogue = [{"type": "rule", "field": "'; DROP TABLE stocks; --", "op": "<=", "value": 1}]
        assert check_rules(_stock(), rogue) is None

    def test_every_comparison_operator_is_supported(self):
        row = _stock(pe=20.0)
        cases = [
            ("<=", 20.0, True),
            ("<", 20.0, False),
            (">=", 20.0, True),
            (">", 20.0, False),
            ("==", 20.0, True),
            ("!=", 20.0, False),
        ]
        for op, value, should_pass in cases:
            rule = [{"type": "rule", "field": "pe", "op": op, "value": value}]
            assert (check_rules(row, rule) is None) is should_pass, op


class TestScoreStock:
    def test_persona_changes_the_ranking(self):
        """The same two stocks must rank differently for different investors."""
        dividend_payer = _stock(
            ticker="ITC",
            name="ITC",
            dividend_yield=5.0,
            debt_to_equity=0.05,
            pe=22.0,
            volatility_1y=0.18,
            sales_growth_3y=6.0,
            return_1y=0.05,
        )
        growth_name = _stock(
            ticker="GRW",
            name="Growth Co",
            dividend_yield=0.0,
            debt_to_equity=0.4,
            pe=70.0,
            volatility_1y=0.55,
            sales_growth_3y=38.0,
            profit_growth_3y=40.0,
            return_1y=0.60,
        )

        conservative = [
            score_stock(dividend_payer, CONSERVATIVE_DIVIDEND),
            score_stock(growth_name, CONSERVATIVE_DIVIDEND),
        ]
        aggressive = [
            score_stock(dividend_payer, GROWTH_SEEKER),
            score_stock(growth_name, GROWTH_SEEKER),
        ]

        assert conservative[0].total > conservative[1].total
        assert aggressive[1].total > aggressive[0].total

    def test_hard_rule_marks_exclusion_with_a_reason(self):
        result = score_stock(_stock(debt_to_equity=2.0), CONSERVATIVE_DIVIDEND)
        assert result.excluded_by is not None
        assert "0.5" in result.excluded_by

    def test_sentiment_tilts_but_does_not_dominate(self):
        """Good press must not lift a weak business above a strong one."""
        strong = _stock(
            roe=30.0, roce=32.0, debt_to_equity=0.1, sentiment=-0.8, sentiment_confidence=0.9
        )
        weak = _stock(
            roe=3.0, roce=4.0, debt_to_equity=1.2, sentiment=0.9, sentiment_confidence=0.9
        )
        neutral = Persona(weights=dict(NEUTRAL_WEIGHTS))
        assert score_stock(strong, neutral).total > score_stock(weak, neutral).total

    def test_sentiment_adjustment_is_bounded(self):
        """Pin the invariant: news can move a score by at most SENTIMENT_TILT.

        This is what lets the docstring claim sentiment 'breaks near-ties'
        rather than 'sometimes reverses the fundamentals'.
        """
        neutral = Persona(weights=dict(NEUTRAL_WEIGHTS))
        base = score_stock(_stock(sentiment=0.0, sentiment_confidence=0.0), neutral)
        best = score_stock(_stock(sentiment=1.0, sentiment_confidence=1.0), neutral)
        worst = score_stock(_stock(sentiment=-1.0, sentiment_confidence=1.0), neutral)

        assert best.total - base.total == pytest.approx(SENTIMENT_TILT, abs=1e-3)
        assert base.total - worst.total == pytest.approx(SENTIMENT_TILT, abs=1e-3)

    def test_low_confidence_sentiment_barely_moves_the_score(self):
        """Thin news coverage should not masquerade as a strong signal."""
        neutral = Persona(weights=dict(NEUTRAL_WEIGHTS))
        base = score_stock(_stock(sentiment=0.0, sentiment_confidence=0.0), neutral)
        thin = score_stock(_stock(sentiment=1.0, sentiment_confidence=0.05), neutral)
        assert abs(thin.total - base.total) < 0.01

    def test_reason_is_populated(self):
        assert score_stock(_stock(), CONSERVATIVE_DIVIDEND).reason

    def test_score_is_deterministic(self):
        row = _stock()
        assert (
            score_stock(row, CONSERVATIVE_DIVIDEND).total
            == score_stock(row, CONSERVATIVE_DIVIDEND).total
        )
