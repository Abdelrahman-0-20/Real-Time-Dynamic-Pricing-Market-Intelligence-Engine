"""Tumbling-window aggregation — the Flink job's logic, as pure functions.

Semantics (unit-tested in tests/test_ingest_windowing.py):

  demand_rate_1h    = units of DemandObserved in [now-1h,  now] / 1h
  demand_rate_24h   = units of DemandObserved in [now-24h, now] / 24h
  inventory_level   = quantity of the LATEST InventoryChanged (absolute level;
                      schema guarantees quantity >= 0, so no delta math here)
  sell_through_24h  = units_24 / (units_24 + inventory_level)
  supply_eta_hours  = eta_hours from the latest SupplyObserved

Freshness is per-feature: each update carries the event_time of the newest
event that CONTRIBUTED to it — staleness in the hot path then means exactly
"we haven't seen telemetry for this fact", not "we computed something late".
Events with event_time in the future (clock skew) are excluded, never averaged
in. Features with no contributing data are NOT emitted (no fabricated zeros).

Every emitted TTL comes from the FeatureDictionary (single source of truth,
docs/feature-manifest.md) — the windowing cannot drift from the freshness
contract without a test failure.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pricing.core.features import FeatureDictionary

HOUR = timedelta(hours=1)
DAY = timedelta(hours=24)


@dataclass(frozen=True)
class FeatureUpdate:
    name: str
    value: float
    event_time: datetime   # newest contributing event
    ttl_ms: int


def _time(event: dict) -> datetime:
    return datetime.fromisoformat(event["event_time"])


def features_from_events(
    events: list[dict],
    now: datetime,
    dictionary: FeatureDictionary,
) -> list[FeatureUpdate]:
    """Aggregate telemetry events into feature updates (empty when no data)."""
    usable = [e for e in events if _time(e) <= now]
    updates: dict[str, FeatureUpdate] = {}

    def emit(name: str, value: float, event_time: datetime) -> None:
        spec = dictionary.specs.get(name)
        if spec is None:
            # Not in the manifest: contract drift, never feed it downstream.
            return
        updates[name] = FeatureUpdate(
            name=name, value=float(value), event_time=event_time, ttl_ms=spec.ttl_ms
        )

    # --- demand rates -------------------------------------------------------
    demands = [e for e in usable if e["event_type"] == "DemandObserved"]
    if demands:
        latest_demand = max(_time(e) for e in demands)
        in_last_hour = [e for e in demands if _time(e) >= now - HOUR]
        in_last_day = [e for e in demands if _time(e) >= now - DAY]
        if in_last_hour:
            emit("demand_rate_1h",
                 sum(float(e["quantity"]) for e in in_last_hour), latest_demand)
        emit("demand_rate_24h",
             sum(float(e["quantity"]) for e in in_last_day) / 24.0, latest_demand)
        total_24 = sum(float(e["quantity"]) for e in in_last_day)
    else:
        total_24 = 0.0

    # --- inventory (latest wins) --------------------------------------------
    inventory_events = [e for e in usable if e["event_type"] == "InventoryChanged"]
    inventory_level: float | None = None
    if inventory_events:
        latest_inventory = max(inventory_events, key=_time)
        inventory_level = float(latest_inventory["quantity"])
        emit("inventory_level", inventory_level, _time(latest_inventory))

    # --- sell-through (needs both sides; never fabricated) -------------------
    if inventory_level is not None and demands:
        denominator = total_24 + inventory_level
        if denominator > 0:
            emit("sell_through_24h", total_24 / denominator,
                 max(_time(e) for e in demands))

    # --- supply ETA ----------------------------------------------------------
    supply = [e for e in usable if e["event_type"] == "SupplyObserved"]
    for event in sorted(supply, key=_time, reverse=True):
        eta = (event.get("attrs") or {}).get("eta_hours")
        if eta is not None:
            emit("supply_eta_hours", float(eta), _time(event))
            break

    return sorted(updates.values(), key=lambda u: u.name)
