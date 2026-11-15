"""Windowing semantics — the Flink job's logic, proven before any cluster exists (ADR-010)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from pricing.ingest.windowing import features_from_events
from tests.helpers import load_dictionary

NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)


def event(event_type: str, minutes_ago: float, *, sku: str = "SKU-1",
          qty: float = 1, attrs: dict | None = None) -> dict:
    payload = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "event_time": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "sku": sku,
        "source": "test-producer",
    }
    if qty is not None:
        payload["quantity"] = qty
    if attrs:
        payload["attrs"] = attrs
    return payload


def values(events, dictionary=None) -> dict[str, float]:
    dictionary = dictionary or load_dictionary()
    return {u.name: u.value for u in features_from_events(events, NOW, dictionary)}


def test_demand_rates_use_their_windows() -> None:
    got = values([
        event("DemandObserved", 30, qty=5),
        event("DemandObserved", 10, qty=3),
        event("DemandObserved", 60 * 5, qty=50),   # 5h old: day window only
    ])
    assert got["demand_rate_1h"] == 8.0            #5+3 in the last hour
    assert got["demand_rate_24h"] == (8 + 50) / 24.0


def test_inventory_is_latest_wins_not_a_sum() -> None:
    got = values([
        event("InventoryChanged", 60, qty=50),
        event("InventoryChanged", 20, qty=100),
    ])
    assert got["inventory_level"] == 100.0


def test_sell_through_needs_both_sides() -> None:
    got = values([
        event("DemandObserved", 10, qty=8),
        event("InventoryChanged", 20, qty=100),
    ])
    assert got["sell_through_24h"] == 8 / 108

    # No inventory event -> sell-through must NOT be fabricated.
    assert "sell_through_24h" not in values([event("DemandObserved", 10, qty=8)])


def test_supply_eta_from_latest_event_with_eta() -> None:
    got = values([
        event("SupplyObserved", 40, qty=None, attrs={"carrier": "x"}),
        event("SupplyObserved", 10, qty=None, attrs={"eta_hours": 6}),
    ])
    assert got["supply_eta_hours"] == 6.0


def test_future_events_are_excluded_never_averaged_in() -> None:
    future = event("DemandObserved", -60, qty=999)   # one hour in the future
    got = values([event("DemandObserved", 10, qty=4), future])
    assert got["demand_rate_1h"] == 4.0
    assert got["demand_rate_24h"] == 4.0 / 24.0


def test_demand_older_than_a_day_is_not_counted() -> None:
    got = values([event("DemandObserved", 60 * 25, qty=99)])
    assert got["demand_rate_24h"] == 0.0
    assert "demand_rate_1h" not in got


def test_no_emittable_data_yields_nothing() -> None:
    assert features_from_events([], NOW, load_dictionary()) == []
    # Supply event without eta, and nothing else: nothing is emittable —
    # absence of data must never become fabricated feature values.
    assert values([event("SupplyObserved", 5, qty=None, attrs={"carrier": "x"})]) == {}


def test_every_update_carries_manifest_ttl() -> None:
    dictionary = load_dictionary()
    updates = features_from_events(
        [event("DemandObserved", 10, qty=4),
         event("InventoryChanged", 10, qty=50),
         event("SupplyObserved", 10, qty=None, attrs={"eta_hours": 3})],
        NOW, dictionary,
    )
    assert updates
    for update in updates:
        assert update.ttl_ms == dictionary.specs[update.name].ttl_ms
        assert update.event_time <= NOW


def test_feature_absent_from_manifest_is_dropped_silently() -> None:
    # dictionary without supply_eta_hours -> windowing must not emit it
    from pricing.core.features import FeatureDictionary

    minimal = FeatureDictionary.from_dict({
        "model_input_version": "fv-test",
        "features": {"demand_rate_1h": {"ttl_ms": 60000}},
    })
    got = values([event("DemandObserved", 5, qty=9),
                  event("InventoryChanged", 5, qty=10)], minimal)
    assert set(got) == {"demand_rate_1h"}
