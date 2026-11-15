"""Stream -> feature -> PRICE: the telemetry join, proven end-to-end (ADR-001)."""
from __future__ import annotations

import json
import pathlib
import uuid
from datetime import timedelta

from jsonschema import Draft202012Validator

from pricing.adapters.jsonl_event_log import JsonlEventLog
from pricing.ingest.runner import TelemetryRunner
from tests.helpers import CONTRACTS, make_service, seed_sku, validate_record


def _validator() -> Draft202012Validator:
    return Draft202012Validator(
        json.loads((CONTRACTS / "telemetry-event.schema.json").read_text())
    )


def _event(event_type: str, when, *, sku: str, qty: float) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "event_time": when.isoformat(),
        "sku": sku,
        "source": "pos-eu-1",
        "quantity": qty,
    }


def test_events_flow_into_the_priced_decision(tiny_artifacts: pathlib.Path) -> None:
    ctx = make_service(tiny_artifacts)
    seed_sku(ctx, "SKU-7780", row=11)          # synthetic baseline features
    now = ctx.clock.now()

    log = JsonlEventLog(pathlib.Path(tiny_artifacts) / "events.jsonl")
    for event_type, minutes, qty in (
        ("DemandObserved", 30, 5), ("DemandObserved", 10, 3),
        ("InventoryChanged", 0, 100),   # fresh: inventory TTL is 60s (manifest)
    ):
        log.append(_event(event_type, now - timedelta(minutes=minutes),
                          sku="SKU-7780", qty=qty))

    runner = TelemetryRunner(
        log=log, store=ctx.store, dictionary=ctx.dictionary,
        validator=_validator(), clock=ctx.clock,
    )
    stats = runner.process()

    assert stats["events_read"] == 3 and stats["events_rejected"] == 0
    assert stats["features_written"] == 4  # rate_1h, rate_24h, inventory, sell-through

    record = ctx.service.price("SKU-7780")
    validate_record(record)
    features = record["model_input"]["features"]
    assert features["demand_rate_1h"] == 8.0, "streamed demand never reached the model"
    assert features["inventory_level"] == 100.0
    # Telemetry is10-30min old vs a 5min TTL: served, but flagged — never hidden.
    assert "demand_rate_1h" in record["stale_features"]


def test_corrupt_line_is_rejected_not_poisoning(tiny_artifacts: pathlib.Path) -> None:
    ctx = make_service(tiny_artifacts)
    seed_sku(ctx, "SKU-1", row=1)
    now = ctx.clock.now()
    path = pathlib.Path(tiny_artifacts) / "events-corrupt.jsonl"
    log = JsonlEventLog(path)

    log.append(_event("DemandObserved", now, sku="SKU-1", qty=4))
    with path.open("a") as handle:  # bypass the write gate: simulate legacy junk
        handle.write(json.dumps({"event_type": "DemandObserved", "sku": "SKU-1"}) + "\n")

    runner = TelemetryRunner(
        log=log, store=ctx.store, dictionary=ctx.dictionary,
        validator=_validator(), clock=ctx.clock,
    )
    stats = runner.process()
    assert stats["events_read"] == 2
    assert stats["events_rejected"] == 1
    assert stats["features_written"] >= 1


def test_event_log_refuses_invalid_events_with_a_fix(tmp_path: pathlib.Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    try:
        log.append({"event_type": "DemandObserved", "sku": "SKU-1"})  # missing ids/time/source
    except ValueError as exc:
        assert "Fix:" in str(exc)
    else:
        raise AssertionError("schema gate let an invalid event through")
    assert log.read_all() == []
