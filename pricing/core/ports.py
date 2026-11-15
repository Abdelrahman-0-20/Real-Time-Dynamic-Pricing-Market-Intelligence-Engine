"""Ports (ADR-002): the interfaces the domain needs from the outside world.

Pure `typing.Protocol` definitions — no implementations, no imports beyond the
stdlib. Adapters implement them; the application core only ever sees these.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pricing.core.features import StoredFeature


class FeatureStore(Protocol):
    """Read-side view of the online feature store, as of a point in time."""

    def snapshot(self, entity_key: str, now: datetime) -> dict[str, StoredFeature]:
        """Return feature name -> StoredFeature for `entity_key`.

        Staleness is NOT resolved here: expired entries are returned too, and
        the core's classification decides fresh / stale-flagged / dropped.
        """
        ...


class ModelServer(Protocol):
    model_version: str

    def score(self, features: dict[str, float]) -> float:
        """Propose a price. Missing features arrive as absent keys — the
        adapter maps them to the model's missing-value path (NaN)."""
        ...


class DecisionLog(Protocol):
    def append(self, record: dict) -> None:
        """Persist a DecisionRecord. Must be idempotent w.r.t. `fingerprint`."""
        ...


class Clock(Protocol):
    def now(self) -> datetime:
        ...
