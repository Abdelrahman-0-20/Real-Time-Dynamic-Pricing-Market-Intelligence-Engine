"""In-memory FeatureStore adapter — step-3 stand-in for the online store.

Implements the `FeatureStore` port exactly: returns raw stored entries with
their `event_time`/`ttl_ms`; staleness semantics live in the core, not here.
Swapping for Redis/Feast later replaces this file only (ADR-004).
"""
from __future__ import annotations

from datetime import datetime

from pricing.core.features import StoredFeature


class InMemoryFeatureStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], StoredFeature] = {}

    def put(
        self,
        entity_key: str,
        name: str,
        value: object,
        event_time: datetime,
        ttl_ms: int,
    ) -> None:
        self._data[(entity_key, name)] = StoredFeature(
            value=value, event_time=event_time, ttl_ms=ttl_ms
        )

    def snapshot(self, entity_key: str, now: datetime) -> dict[str, StoredFeature]:
        # `now` intentionally unused: expired entries are returned so the
        # core's freshness contract decides their fate (never hidden here).
        return {
            name: stored
            for (entity, name), stored in self._data.items()
            if entity == entity_key
        }
