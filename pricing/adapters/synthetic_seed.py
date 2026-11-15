"""Synthetic feature seeding — the step-3 demo world, shared by CLI and API.

Deterministic per SKU (sha256 -> row index), so `pricingctl price explain`
and `POST /v1/price` price the SAME synthetic snapshot for the same SKU
until real telemetry feeds the store instead.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from pricing.core.features import FeatureDictionary

_SEED_CACHE: dict[int, object] = {}


def _rows(n: int = 500, seed: int = 11):
    if n not in _SEED_CACHE:
        from pricing.training.synthetic import generate

        _SEED_CACHE[n] = generate(n=n, seed=seed)
    return _SEED_CACHE[n]


def seed_sku_features(store, dictionary: FeatureDictionary, sku: str,
                      now: datetime) -> int:
    """Write a full synthetic snapshot for `sku`; returns the row used."""
    X, _, _ = _rows()
    row = int(hashlib.sha256(sku.encode("utf-8")).hexdigest()[:8], 16) % len(X)
    for index, name in enumerate(dictionary.specs):
        store.put(
            f"sku:{sku}", name, float(X[row, index]),
            event_time=now, ttl_ms=dictionary.specs[name].ttl_ms,
        )
    return row
