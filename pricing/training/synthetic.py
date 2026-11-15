"""Synthetic market with KNOWN ground truth (step-1 decision #3).

Why synthetic: with no historical export we can verify the model learns real
signal instead of leakage — because we know the true price elasticity and the
true margin-optimal price for every generated SKU. A real dataset can't give
us that check.

Ground truth model (constant-elasticity demand):
    D(p) = demand_24h * (p / price_last) ** (-elasticity)
    margin(p) = (p - base_cost) * D(p)
    p* = base_cost * e / (e - 1)          (unconstrained optimum)

Elasticity is derived FROM observable features, so the mapping
features -> optimal price is learnable (no irreducible hidden variable),
plus a small noise term so it isn't trivially exact.
"""
from __future__ import annotations

import numpy as np

# Must match config/features/fv-2026.11.json exactly (test_training enforces).
FEATURE_ORDER: list[str] = [
    "demand_rate_1h",
    "demand_rate_24h",
    "inventory_level",
    "sell_through_24h",
    "supply_eta_hours",
    "base_cost",
    "price_last",
    "category_median_price",
    "competitor_price",
    "competitor_price_delta_pct",
    "competitor_in_stock",
    "signal_competitor_stockout",
    "signal_competitor_price_change",
    "signal_seasonal_promotion",
    "signal_demand_surge",
]


def generate(n: int = 20_000, seed: int = 7) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray]]:
    """Return (X in FEATURE_ORDER, y = optimal price, ground-truth columns)."""
    rng = np.random.default_rng(seed)

    base_cost = rng.uniform(15.0, 80.0, n)
    category_median = base_cost * np.exp(rng.normal(np.log(2.0), 0.30, n))
    competitor_price = category_median * np.exp(rng.normal(0.0, 0.12, n))
    price_last = category_median * np.exp(rng.normal(0.0, 0.10, n))
    demand_24h = np.exp(rng.normal(3.5, 0.7, n))
    demand_1h = demand_24h / 24.0 * np.exp(rng.normal(0.0, 0.35, n))
    inventory = rng.poisson(demand_24h * 3.0) + 1
    sell_through = rng.uniform(0.05, 0.90, n)
    supply_eta = np.where(rng.random(n) < 0.10, np.nan, rng.uniform(2.0, 72.0, n))
    delta_pct = (competitor_price - price_last) / price_last * 100.0 + rng.normal(0.0, 1.5, n)
    in_stock = (rng.random(n) > 0.12).astype(float)
    sig_stockout = (rng.random(n) < 0.10).astype(float) * rng.uniform(0.70, 0.98, n)
    sig_pricedrop = (rng.random(n) < 0.15).astype(float) * rng.uniform(0.70, 0.98, n)
    sig_promo = rng.beta(1.2, 3.0, n)
    sig_demand = (rng.random(n) < 0.12).astype(float) * rng.uniform(0.70, 0.95, n)

    # True elasticity, derived from observables + small noise.
    elasticity = np.clip(
        1.7
        + 0.5 * np.tanh(np.log(demand_24h) - 3.5)
        + 0.3 * (competitor_price / category_median - 1.0)
        + 0.2 * sig_promo
        + rng.normal(0.0, 0.08, n),
        1.25,
        4.0,
    )
    optimal_price = base_cost * elasticity / (elasticity - 1.0)

    columns = {
        "demand_rate_1h": demand_1h,
        "demand_rate_24h": demand_24h,
        "inventory_level": inventory.astype(float),
        "sell_through_24h": sell_through,
        "supply_eta_hours": supply_eta,
        "base_cost": base_cost,
        "price_last": price_last,
        "category_median_price": category_median,
        "competitor_price": competitor_price,
        "competitor_price_delta_pct": delta_pct,
        "competitor_in_stock": in_stock,
        "signal_competitor_stockout": sig_stockout,
        "signal_competitor_price_change": sig_pricedrop,
        "signal_seasonal_promotion": sig_promo,
        "signal_demand_surge": sig_demand,
    }
    X = np.column_stack([columns[name] for name in FEATURE_ORDER])
    truth = {
        "base_cost": base_cost,
        "elasticity": elasticity,
        "price_last": price_last,
        "demand_24h": demand_24h,
        "category_median_price": category_median,
    }
    return X, optimal_price, truth


def margin_at(truth: dict[str, np.ndarray], idx: np.ndarray | slice, price: np.ndarray) -> np.ndarray:
    """True margin of charging `price` (per row in idx)."""
    cost = truth["base_cost"][idx]
    demand_at_last = truth["demand_24h"][idx]
    price_last = truth["price_last"][idx]
    elasticity = truth["elasticity"][idx]
    demand = demand_at_last * np.power(np.clip(price, 1e-6, None) / price_last, -elasticity)
    return (price - cost) * demand
