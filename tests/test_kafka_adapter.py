"""Kafka adapter: contract logic runs without a broker; integration is gated (ADR-010).

Flip-on: export KAFKA_BROKER=host:9092  ->  integration test activates.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

from pricing.adapters.kafka_events import EventDecodeError, KafkaEventSource


def _valid_event() -> dict:
    from datetime import datetime, timezone
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "DemandObserved",
        "event_time": datetime.now(timezone.utc).isoformat(),
        "sku": "SKU-1",
        "source": "pos-eu-1",
        "quantity": 3,
    }


def test_decode_returns_a_validated_event() -> None:
    source = KafkaEventSource(bootstrap="unused:9092")
    event = _valid_event()
    assert source.decode(json.dumps(event)) == event
    assert source._consumer is None, "decoding must not construct a consumer"


def test_decode_rejects_non_json_with_a_fix() -> None:
    source = KafkaEventSource()
    with pytest.raises(EventDecodeError, match="Fix:"):
        source.decode(b"\x00not json")


def test_decode_rejects_schema_violations_naming_the_field() -> None:
    source = KafkaEventSource()
    bad = _valid_event()
    del bad["sku"]
    with pytest.raises(EventDecodeError, match="sku") as exc:
        source.decode(json.dumps(bad))
    assert "schema" in str(exc.value)


def test_construction_never_opens_a_connection() -> None:
    # Unreachable broker on purpose: constructing must be inert (lazy consumer).
    source = KafkaEventSource(bootstrap="127.0.0.1:1", group="g", topics=["t"])
    assert source._consumer is None
    source.close()  # must be safe before any consumer exists


@pytest.mark.skipif(
    not os.environ.get("KAFKA_BROKER"),
    reason="set KAFKA_BROKER=host:9092 to run broker integration (ADR-010 flip-on)",
)
def test_broker_roundtrip_integration() -> None:
    source = KafkaEventSource(bootstrap=os.environ["KAFKA_BROKER"],
                              group="pricing-tests", topics=["telemetry.raw"])
    try:
        result = source.poll(timeout=2.0)
        assert result is None or isinstance(result, dict)  # None = empty topic
    finally:
        source.close()
