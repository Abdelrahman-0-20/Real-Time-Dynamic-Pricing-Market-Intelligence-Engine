"""Flip-on integration tests (ADR-010): activate ONLY against real servers.

    docker compose up -d
    export REDIS_URL=redis://localhost:6379/0
    export KAFKA_BROKER=localhost:19092
    python -m pytest tests/test_flipon_integration.py -q

Without the env vars these SKIP (not fail) — the local substitutes
(fakeredis, JSONL) remain the tested default. With them, this file proves
the REAL server path: real Redis roundtrip, real Kafka produce->consume.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

REDIS_URL = os.environ.get("REDIS_URL")
KAFKA_BROKER = os.environ.get("KAFKA_BROKER")

NOW = datetime(2026, 11, 15, 8, 0, 0, tzinfo=timezone.utc)


@pytest.mark.skipif(not REDIS_URL, reason="set REDIS_URL to run against real Redis")
def test_real_redis_roundtrip_and_classify_parity() -> None:
    import redis

    from pricing.adapters.memory_features import InMemoryFeatureStore
    from pricing.adapters.redis_feature_store import RedisFeatureStore
    from pricing.core.features import classify
    from tests.helpers import load_dictionary

    client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=3)
    client.ping()  # fail loudly if the server isn't there (env promises it)
    client.flushdb()  # integration test owns this DB (index from URL)

    real = RedisFeatureStore(client, prefix="flipon:test:")
    memory = InMemoryFeatureStore()
    samples = {
        "inventory_level": (50, NOW - timedelta(seconds=10)),
        "competitor_price": (124.5, NOW - timedelta(minutes=45)),
        "price_last": (99.0, NOW - timedelta(minutes=30)),
    }
    for store in (real, memory):
        for name, (value, when) in samples.items():
            store.put("sku:FLIPON", name, value, event_time=when, ttl_ms=60000)

    dictionary = load_dictionary()
    from_memory = classify(memory.snapshot("sku:FLIPON", NOW), dictionary, NOW)
    from_redis = classify(real.snapshot("sku:FLIPON", NOW), dictionary, NOW)

    assert from_redis.model_input == from_memory.model_input
    assert from_redis.stale == from_memory.stale
    assert sorted(from_redis.dropped) == sorted(from_memory.dropped)
    client.delete("feat:flipon:test:sku:FLIPON")


@pytest.mark.skipif(not KAFKA_BROKER, reason="set KAFKA_BROKER to run against a real broker")
def test_real_kafka_produce_consume_decode() -> None:
    from confluent_kafka import Producer

    from pricing.adapters.kafka_events import KafkaEventSource

    topic = "telemetry.flipon"
    producer = Producer({"bootstrap.servers": KAFKA_BROKER})
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "DemandObserved",
        "event_time": datetime.now(timezone.utc).isoformat(),
        "sku": "SKU-FLIPON",
        "source": "flipon-test",
        "quantity": 7,
    }
    producer.produce(topic, json.dumps(event).encode())
    producer.flush(10)

    source = KafkaEventSource(
        bootstrap=KAFKA_BROKER, group="pricing-flipon", topics=[topic],
        config={"enable.auto.commit": False},
    )
    try:
        received = None
        for _ in range(30):  # up to ~3s for delivery
            received = source.poll(timeout=0.1)
            if received is not None:
                break
        assert received is not None, "produced event never arrived at the consumer"
        assert received["sku"] == "SKU-FLIPON"
        assert received["quantity"] == 7
    finally:
        source.close()
