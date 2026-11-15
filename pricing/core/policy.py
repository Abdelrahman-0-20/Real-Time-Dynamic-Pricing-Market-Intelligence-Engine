"""Policy packs (ADR-008): regulatory/commercial guardrails as DATA.

One JSON pack per market, loaded at the composition root, validated here as
pure functions. Failure direction is always toward safety:

  * unknown market -> derived pack STRICTER than every loaded pack
  * no packs at all -> conservative absolute defaults
  * conflicting floor/ceiling -> floor (the legal minimum) wins
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# Absolute safety net when no pack can be loaded at all (ADR-008).
SAFE_DEFAULTS = dict(
    market="__safe_defaults__",
    version="safe-default",
    status="derived",
    min_margin_pct=0.30,
    max_price_multiple=1.0,
    max_step_pct=0.05,
    parity_enabled=False,
)


@dataclass(frozen=True)
class PolicyPack:
    market: str
    version: str
    status: str            # "placeholder" | "approved" | "derived" | ...
    min_margin_pct: float  # floor  = base_cost * (1 + min_margin_pct)
    max_price_multiple: float  # ceiling = category_median_price * max_price_multiple
    max_step_pct: float    # max move vs last served price
    parity_enabled: bool

    @classmethod
    def from_dict(cls, data: dict) -> "PolicyPack":
        try:
            pack = cls(
                market=str(data["market"]),
                version=str(data["version"]),
                status=str(data.get("status", "unknown")),
                min_margin_pct=float(data["min_margin_pct"]),
                max_price_multiple=float(data["max_price_multiple"]),
                max_step_pct=float(data["max_step_pct"]),
                parity_enabled=bool(data.get("parity_enabled", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid guardrail pack ({exc!r}). "
                "Fix: every pack needs market, version, status, min_margin_pct, "
                "max_price_multiple, max_step_pct — see config/guardrails/EG.json."
            ) from None
        if not 0 <= pack.min_margin_pct <= 1:
            raise ValueError(
                f"{pack.market}: min_margin_pct must be in [0, 1], got {pack.min_margin_pct}. "
                "Fix: express margin as a fraction (0.15 = 15%)."
            )
        if pack.max_price_multiple < 1:
            raise ValueError(
                f"{pack.market}: max_price_multiple must be >= 1, got {pack.max_price_multiple}. "
                "Fix: 1.0 means 'never above the category median'."
            )
        if not 0 < pack.max_step_pct <= 1:
            raise ValueError(
                f"{pack.market}: max_step_pct must be in (0, 1], got {pack.max_step_pct}. "
                "Fix: express the step cap as a fraction (0.10 = 10%)."
            )
        return pack

    def band(self, base_cost: float, category_median_price: float) -> tuple[float, float]:
        """Absolute price band for these inputs. Floor wins a conflict."""
        floor = base_cost * (1 + self.min_margin_pct)
        ceiling = category_median_price * self.max_price_multiple
        if floor > ceiling:
            ceiling = floor  # misconfigured pack: satisfy the legal minimum first
        return floor, ceiling


def resolve_pack(
    packs: Mapping[str, PolicyPack], market: str
) -> tuple[PolicyPack, bool]:
    """ADR-008: known market -> its pack; unknown -> most restrictive fallback.

    Returns (pack, fallback_used). `fallback_used=True` must flag the
    DecisionRecord as degraded.
    """
    if market in packs:
        return packs[market], False
    if not packs:
        return PolicyPack.from_dict(SAFE_DEFAULTS), True
    # Strictly at least as restrictive as ANY loaded pack, in every dimension.
    derived = PolicyPack(
        market=f"__derived_from_{market}__",
        version="derived-restrictive",
        status="derived",
        min_margin_pct=max(p.min_margin_pct for p in packs.values()),
        max_price_multiple=min(p.max_price_multiple for p in packs.values()),
        max_step_pct=min(p.max_step_pct for p in packs.values()),
        parity_enabled=all(p.parity_enabled for p in packs.values()),
    )
    return derived, True
