"""Source registry + per-source circuit breaker (ADR-006 design rules).

ToS/robots review is a *refusal*, not a skip: an unreviewed source never
fetches, and the reason is recorded in the run stats (visible, not silent).
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

from pricing.intel.ports import Source

FAILURE_THRESHOLD = 5      # failures within the window -> OPEN
COOLDOWN_SECONDS = 60      # OPEN duration before HALF-OPEN probe


@dataclass
class _BreakerState:
    failures: list[float] = field(default_factory=list)  # timestamps (seconds)
    opened_at: float | None = None
    half_open: bool = False


class CircuitBreaker:
    """CLOSED -> OPEN -> HALF-OPEN, driven by an injected clock (seconds)."""

    def __init__(self, clock_seconds, threshold: int = FAILURE_THRESHOLD,
                 cooldown: float = COOLDOWN_SECONDS) -> None:
        self._clock = clock_seconds
        self._threshold = threshold
        self._cooldown = cooldown
        self._state = _BreakerState()

    @property
    def state(self) -> str:
        s = self._state
        if s.opened_at is None:
            return "closed"
        if self._clock() - s.opened_at >= self._cooldown:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self._state = _BreakerState()

    def record_failure(self) -> None:
        s = self._state
        now = self._clock()
        s.failures = [t for t in s.failures if now - t < self._cooldown] + [now]
        if s.opened_at is None and len(s.failures) >= self._threshold:
            s.opened_at = now
        elif s.opened_at is not None and self.state == "half_open":
            s.opened_at = now  # probe failed: stay open for another cooldown


class SourceRegistry:
    def __init__(self, sources: list[Source]) -> None:
        self._sources = {s.id: s for s in sources}

    @classmethod
    def from_config(cls, path: str | pathlib.Path) -> "SourceRegistry":
        payload = json.loads(pathlib.Path(path).read_text())
        raw = payload.get("sources")
        if not isinstance(raw, list) or not raw:
            raise ValueError(
                f"no sources found in {path}. "
                "Fix: config/sources.json must contain a non-empty 'sources' list."
            )
        sources = [
            Source(
                id=str(s["id"]), url=str(s["url"]), type=str(s.get("type", "")),
                category=str(s.get("category", "general")),
                trust=float(s.get("trust", 0.5)),
                tos_reviewed=bool(s.get("tos_reviewed", False)),
                enabled=bool(s.get("enabled", True)),
                seed_file=s.get("seed_file"),
            )
            for s in raw
        ]
        return cls(sources)

    def get(self, source_id: str) -> Source:
        try:
            return self._sources[source_id]
        except KeyError:
            known = ", ".join(sorted(self._sources))
            raise ValueError(
                f"unknown source '{source_id}'. Known sources: {known}. "
                "Fix: add it to config/sources.json or pick a listed id."
            ) from None

    def all(self) -> list[Source]:
        return list(self._sources.values())

    def reviewable(self) -> list[Source]:
        """Sources the pipeline is allowed to touch: enabled AND ToS-reviewed."""
        return [s for s in self._sources.values() if s.enabled and s.tos_reviewed]
