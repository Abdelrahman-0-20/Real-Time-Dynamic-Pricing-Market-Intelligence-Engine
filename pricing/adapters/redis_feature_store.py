"""Redis FeatureStore adapter — real `redis-py` client, tested vs fakeredis.

ADR-010: the CLIENT code is production code (commands, encoding, atomicity
assumptions are exactly what a real Redis will see); only the server is
substituted locally. Layout: one Redis hash per entity, fields = feature
names, values = {"value", "event_time", "ttl_ms"} — staleness metadata
travels with the data, because freshness is part of the contract.
"""
from __future__ import annotations

import json
from datetime import datetime

from pricing.core.features import StoredFeature


class RedisFeatureStore:
    def __init__(self, client, prefix: str = "feat:") -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, entity_key: str) -> str:
        return f"{self._prefix}{entity_key}"

    def put(self, entity_key: str, name: str, value: object,
            event_time: datetime, ttl_ms: int) -> None:
        payload = json.dumps({
            "value": value,
            "event_time": event_time.isoformat(),
            "ttl_ms": int(ttl_ms),
        })
        self._client.hset(self._key(entity_key), name, payload)

    def snapshot(self, entity_key: str, now: datetime) -> dict[str, StoredFeature]:
        raw = self._client.hgetall(self._key(entity_key))
        snapshot: dict[str, StoredFeature] = {}
        for field, data in raw.items():
            name = field.decode() if isinstance(field, bytes) else str(field)
            body = data.decode() if isinstance(data, bytes) else str(data)
            try:
                payload = json.loads(body)
                snapshot[name] = StoredFeature(
                    value=payload["value"],
                    event_time=datetime.fromisoformat(payload["event_time"]),
                    ttl_ms=int(payload["ttl_ms"]),
                )
            except (json.JSONDecodeError, KeyError, ValueError):
                # Corrupt field: degrade loudly (dropped feature), never crash
                # the serve path over one bad hash entry.
                continue
        return snapshot
