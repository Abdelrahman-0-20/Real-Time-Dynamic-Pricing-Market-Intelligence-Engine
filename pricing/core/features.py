"""Feature dictionary + staleness classification (the freshness contract).

ARCHITECTURE §4.2, made executable:

    age < 3*ttl        -> normal
    ttl <= age < 3*ttl -> served, flagged in DecisionRecord.stale_features
    age >= 3*ttl       -> dropped; if the feature is critical, the whole
                          decision degrades (never silently imputed)

The dictionary is the machine form of docs/feature-manifest.md and carries
`model_input_version` — renaming a feature without bumping the version is a
contract violation, caught by tests.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    ttl_ms: int
    critical: bool  # critical features degrade the decision when missing/stale-out


@dataclass(frozen=True)
class FeatureDictionary:
    version: str
    specs: Mapping[str, FeatureSpec]

    @classmethod
    def from_dict(cls, data: dict) -> "FeatureDictionary":
        version = data.get("model_input_version")
        if not version:
            raise ValueError(
                "feature dictionary is missing 'model_input_version'. "
                "Fix: copy config/features/fv-2026.11.json and set the version field."
            )
        raw = data.get("features")
        if not isinstance(raw, dict) or not raw:
            raise ValueError(
                "feature dictionary needs a non-empty 'features' mapping. "
                "Fix: see docs/feature-manifest.md for the expected shape."
            )
        specs: dict[str, FeatureSpec] = {}
        for name, spec in raw.items():
            if not _NAME_RE.match(name):
                raise ValueError(
                    f"invalid feature name '{name}' — must match [a-z][a-z0-9_]*. "
                    "Fix: rename in the manifest AND bump model_input_version."
                )
            ttl = spec.get("ttl_ms")
            if not isinstance(ttl, int) or ttl <= 0:
                raise ValueError(
                    f"feature '{name}' needs a positive integer ttl_ms. "
                    "Fix: set the TTL from the manifest table."
                )
            specs[name] = FeatureSpec(name=name, ttl_ms=ttl, critical=bool(spec.get("critical", False)))
        return cls(version=version, specs=specs)


@dataclass(frozen=True)
class StoredFeature:
    value: object          # number | bool | str (embeddings ref)
    event_time: datetime   # when the underlying fact was TRUE
    ttl_ms: int


@dataclass(frozen=True)
class SnapshotResult:
    model_input: dict[str, float] = field(default_factory=dict)
    stale: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    degraded: bool = False


def classify(
    snapshot: Mapping[str, StoredFeature],
    dictionary: FeatureDictionary,
    now: datetime,
) -> SnapshotResult:
    """Turn a raw store snapshot into a model input + honest staleness lists."""
    model_input: dict[str, float] = {}
    stale: list[str] = []
    dropped: list[str] = []
    degraded = False

    for name, stored in snapshot.items():
        spec = dictionary.specs.get(name)
        if spec is None:
            # Feature not in the manifest: contract drift — never feed it to
            # the model, and make the decision loudly degraded.
            dropped.append(name)
            degraded = True
            continue
        age = now - stored.event_time
        # The dictionary (manifest) is THE freshness contract — both thresholds
        # derive from it, so a writer stamping a different TTL can never make
        # "stale" and "dropped" disagree with each other.
        if age >= timedelta(milliseconds=3 * spec.ttl_ms):
            dropped.append(name)
            if spec.critical:
                degraded = True
            continue
        if spec.critical and isinstance(stored.value, float) and stored.value != stored.value:
            # A critical feature that exists but is NaN is broken data, not a
            # value — degrade loudly rather than band a price off NaN.
            dropped.append(name)
            degraded = True
            continue
        model_input[name] = float(stored.value) if isinstance(stored.value, (int, float, bool)) else float("nan")
        if age >= timedelta(milliseconds=spec.ttl_ms):
            stale.append(name)

    # Dictionary features absent from the store: absent critical => degrade;
    # absent non-critical => model's missing-value path (key simply absent).
    for name, spec in dictionary.specs.items():
        if name not in snapshot:
            dropped.append(name)
            if spec.critical:
                degraded = True

    return SnapshotResult(model_input=model_input, stale=stale, dropped=dropped, degraded=degraded)
