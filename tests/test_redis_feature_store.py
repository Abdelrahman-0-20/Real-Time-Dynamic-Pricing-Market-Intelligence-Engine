"""RedisFeatureStore against fakeredis — real client protocol, substituted server (ADR-010)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

from pricing.adapters.memory_features import InMemoryFeatureStore
from pricing.adapters.redis_feature_store import RedisFeatureStore
from pricing.core.features import classify
from tests.helpers import load_dictionary

NOW = datetime(2026, 11, 15, 8, 0, 0, tzinfo=timezone.utc)


def _stores():
    return (
        InMemoryFeatureStore(),
        RedisFeatureStore(fakeredis.FakeRedis()),
    )


def test_roundtrip_preserves_staleness_metadata() -> None:
    memory, redis = _stores()
    age = NOW - timedelta(seconds=200)
    for store in (memory, redis):
        store.put("sku:SKU-1", "competitor_price", 124.5, event_time=age, ttl_ms=1000)
        store.put("sku:SKU-1", "demand_rate_1h", 42.0, event_time=NOW, ttl_ms=300000)

    for store in (memory, redis):
        snap = store.snapshot("sku:SKU-1", NOW)
        assert snap["competitor_price"].value == 124.5
        assert snap["competitor_price"].event_time == age      # staleness travels
        assert snap["competitor_price"].ttl_ms == 1000


def test_redis_store_classifies_identically_to_memory() -> None:
    """Same inputs => same fresh/stale/dropped decisions: adapters can't drift."""
    memory, redis = _stores()
    dictionary = load_dictionary()
    samples = {
        "inventory_level": (50, NOW - timedelta(seconds=10), 60000),        # fresh
        "competitor_price": (124.5, NOW - timedelta(minutes=45), 1800000),  # stale (TTL 30m)
        "base_cost": (30.0, NOW - timedelta(hours=5), 86400000),            # fresh, slow
        "price_last": (99.0, NOW - timedelta(minutes=30), 300000),          # past 3x TTL
    }
    for store in (memory, redis):
        for name, (value, when, ttl) in samples.items():
            store.put("sku:SKU-9", name, value, event_time=when, ttl_ms=ttl)

    expected = classify(memory.snapshot("sku:SKU-9", NOW), dictionary, NOW)
    actual = classify(redis.snapshot("sku:SKU-9", NOW), dictionary, NOW)

    assert actual.model_input == expected.model_input
    assert actual.stale == expected.stale
    assert sorted(actual.dropped) == sorted(expected.dropped)
    assert actual.degraded == expected.degraded
    assert "price_last" in actual.dropped            # >3x TTL: dropped
    assert "competitor_price" in actual.stale


def test_corrupt_hash_field_is_skipped_not_fatal() -> None:
    _, redis = _stores()
    redis.put("sku:SKU-2", "demand_rate_1h", 5.0, event_time=NOW, ttl_ms=1000)
    redis._client.hset("feat:sku:SKU-2", "rogue", "not-json{{{")
    snap = redis.snapshot("sku:SKU-2", NOW)
    assert "demand_rate_1h" in snap
    assert "rogue" not in snap


def test_unknown_entity_returns_empty_snapshot() -> None:
    _, redis = _stores()
    assert redis.snapshot("sku:MISSING", NOW) == {}
