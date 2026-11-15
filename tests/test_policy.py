"""Policy pack behavior (ADR-008): band math, fail-safe resolution, validation."""
from __future__ import annotations

import pytest

from pricing.core.policy import SAFE_DEFAULTS, PolicyPack, resolve_pack


def _pack(market: str, margin: float = 0.15, multiple: float = 1.3, step: float = 0.10) -> PolicyPack:
    return PolicyPack.from_dict(
        dict(
            market=market, version="v1", status="approved",
            min_margin_pct=margin, max_price_multiple=multiple,
            max_step_pct=step, parity_enabled=False,
        )
    )


def test_band_math() -> None:
    floor, ceiling = _pack("EG").band(base_cost=100.0, category_median_price=200.0)
    assert floor == pytest.approx(115.0)
    assert ceiling == pytest.approx(260.0)


def test_band_conflict_resolves_to_floor() -> None:
    """floor 115 vs ceiling 104: the legal minimum wins (ADR-008)."""
    floor, ceiling = _pack("EG").band(base_cost=100.0, category_median_price=80.0)
    assert floor == pytest.approx(115.0)
    assert ceiling == floor


def test_known_market_is_not_a_fallback() -> None:
    pack, fallback = resolve_pack({"EG": _pack("EG")}, "EG")
    assert fallback is False
    assert pack.market == "EG"


def test_unknown_market_resolves_stricter_than_every_pack() -> None:
    packs = {"EG": _pack("EG", margin=0.15, multiple=1.3, step=0.10),
             "SA": _pack("SA", margin=0.20, multiple=1.5, step=0.05)}
    pack, fallback = resolve_pack(packs, "ZZ")
    assert fallback is True
    assert pack.min_margin_pct >= max(p.min_margin_pct for p in packs.values())
    assert pack.max_price_multiple <= min(p.max_price_multiple for p in packs.values())
    assert pack.max_step_pct <= min(p.max_step_pct for p in packs.values())
    assert pack.status == "derived"


def test_no_packs_at_all_falls_back_to_safe_defaults() -> None:
    pack, fallback = resolve_pack({}, "EG")
    assert fallback is True
    assert pack.market == SAFE_DEFAULTS["market"]
    assert pack.max_price_multiple == 1.0  # never above median


def test_invalid_values_name_the_fix() -> None:
    with pytest.raises(ValueError, match="Fix:"):
        PolicyPack.from_dict(
            dict(market="EG", version="v0", status="placeholder",
                 min_margin_pct=5.0, max_price_multiple=1.3, max_step_pct=0.1)
        )
    with pytest.raises(ValueError, match="Fix:"):
        PolicyPack.from_dict(
            dict(market="EG", version="v0", status="placeholder",
                 min_margin_pct=0.15, max_price_multiple=1.3, max_step_pct=0.0)
        )
    with pytest.raises(ValueError, match="Fix:"):
        PolicyPack.from_dict({"market": "EG"})  # missing required keys
