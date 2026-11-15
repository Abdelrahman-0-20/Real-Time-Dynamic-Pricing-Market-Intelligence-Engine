"""Guardrail chain — the pure-function safety net for every price.

Every price (model output, ADR-007 signal trigger, or rule fallback) passes
through here. No I/O, no clock, no randomness: given the same inputs, the same
clamp decisions come out — which is what makes them unit-testable and
replayable from a DecisionRecord.

Policy packs (ADR-008) supply the numeric bounds as data; this module only
applies them in fixed order.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GuardrailAction:
    rule: str            # closed enum — see decision-record.schema.json
    from_price: float
    to_price: float
    detail: str
    market: str | None = None


def apply_guardrails(
    proposed: float,
    *,
    last_price: float,
    floor: float,
    ceiling: float,
    max_step_pct: float,
    market: str | None = None,
) -> tuple[float, list[GuardrailAction]]:
    """Clamp `proposed` into the safe band. Returns (final_price, actions)."""
    price = proposed
    actions: list[GuardrailAction] = []

    def clamp(rule: str, low: float | None, high: float | None, detail: str) -> None:
        """Apply a one- or two-sided bound. Floor is low-only; ceiling is high-only."""
        nonlocal price
        target = price
        if low is not None:
            target = max(target, low)
        if high is not None:
            target = min(target, high)
        if target != price:
            actions.append(GuardrailAction(rule, price, target, detail, market))
            price = target

    clamp("margin_floor", floor, None, "minimum allowed price (policy pack)")
    clamp("margin_ceiling", None, ceiling, "maximum allowed price (policy pack)")
    if last_price > 0:
        # Step bounds are intersected with the policy band: a relative move may
        # never violate an absolute rule. If the ranges are disjoint (bad pack
        # or extreme market move), the absolute floor wins — fail toward safety.
        lo = max(last_price * (1 - max_step_pct), floor)
        hi = min(last_price * (1 + max_step_pct), ceiling)
        if lo > hi:
            hi = lo
        clamp("max_step_pct", lo, hi, f"max step {max_step_pct:.0%} vs last price (bounded by policy band)")

    return price, actions
