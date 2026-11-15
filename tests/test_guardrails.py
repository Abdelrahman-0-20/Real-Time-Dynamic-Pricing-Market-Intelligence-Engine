"""Guardrail chain behavior — the safety net every price passes through."""
from __future__ import annotations

import pytest

from pricing.core.guardrails import apply_guardrails


def test_price_within_band_passes_untouched() -> None:
    price, actions = apply_guardrails(
        110.0, last_price=100.0, floor=80.0, ceiling=150.0, max_step_pct=0.20
    )
    assert price == pytest.approx(110.0)
    assert actions == []


def test_margin_floor_clamps_up() -> None:
    price, actions = apply_guardrails(
        50.0, last_price=100.0, floor=90.0, ceiling=150.0, max_step_pct=0.20
    )
    assert price == pytest.approx(90.0)
    assert [a.rule for a in actions] == ["margin_floor"]
    assert actions[0].from_price == 50.0 and actions[0].to_price == 90.0


def test_max_step_limits_upward_jump() -> None:
    price, actions = apply_guardrails(
        200.0, last_price=100.0, floor=50.0, ceiling=250.0, max_step_pct=0.10
    )
    assert price == pytest.approx(110.0)
    assert [a.rule for a in actions] == ["max_step_pct"]


def test_ceiling_clamps_down() -> None:
    price, actions = apply_guardrails(
        300.0, last_price=290.0, floor=50.0, ceiling=295.0, max_step_pct=0.20
    )
    assert price == pytest.approx(295.0)
    assert [a.rule for a in actions] == ["margin_ceiling"]


def test_market_code_is_carried_for_audit() -> None:
    _, actions = apply_guardrails(
        10.0, last_price=100.0, floor=60.0, ceiling=150.0, max_step_pct=0.20, market="EG"
    )
    assert actions and actions[0].market == "EG"


def test_deterministic_replay() -> None:
    """Same inputs → same clamps: the property that makes DecisionRecords replayable."""
    args = dict(last_price=100.0, floor=85.0, ceiling=140.0, max_step_pct=0.15, market="SA")
    first = apply_guardrails(250.0, **args)
    second = apply_guardrails(250.0, **args)
    assert first == second


def test_absolute_floor_wins_over_step_when_ranges_disjoint() -> None:
    """floor 120 vs step band [90, 110]: the price must never land below the floor."""
    price, _ = apply_guardrails(
        50.0, last_price=100.0, floor=120.0, ceiling=300.0, max_step_pct=0.10
    )
    assert price >= 120.0
