"""Ingest runner: event log -> windowing -> feature store (the hot-path join).

Application service over ports only. Events are re-validated on READ as well
as on write: a corrupt line must degrade the stats (rejected count), never
poison the feature store — same discipline as the cold path's schema gate.
"""
from __future__ import annotations

from datetime import datetime

from jsonschema import Draft202012Validator

from pricing.core.features import FeatureDictionary
from pricing.ingest.windowing import features_from_events


class TelemetryRunner:
    def __init__(
        self,
        *,
        log,                     # EventLog: read_all() -> list[dict]
        store,                   # FeatureStore port: put(...)
        dictionary: FeatureDictionary,
        validator: Draft202012Validator,
        clock,
    ) -> None:
        self._log = log
        self._store = store
        self._dictionary = dictionary
        self._validator = validator
        self._clock = clock

    def process(self, now: datetime | None = None) -> dict:
        now = now or self._clock.now()
        events = self._log.read_all()

        valid: list[dict] = []
        rejected = 0
        for event in events:
            if not list(self._validator.iter_errors(event)):
                valid.append(event)
            else:
                rejected += 1

        by_sku: dict[str, list[dict]] = {}
        for event in valid:
            by_sku.setdefault(event["sku"], []).append(event)

        features_written = 0
        per_sku: dict[str, int] = {}
        for sku, sku_events in by_sku.items():
            updates = features_from_events(sku_events, now, self._dictionary)
            for update in updates:
                self._store.put(
                    f"sku:{sku}", update.name, update.value,
                    event_time=update.event_time, ttl_ms=update.ttl_ms,
                )
            per_sku[sku] = len(updates)
            features_written += len(updates)

        return {
            "events_read": len(events),
            "events_valid": len(valid),
            "events_rejected": rejected,
            "skus": len(by_sku),
            "features_written": features_written,
            "per_sku": per_sku,
        }
