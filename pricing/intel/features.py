"""Signal -> feature conversion: how an LLM/lexicon signal becomes model input.

Implements ADR-003's decay mechanism: the stored value is the confidence
decayed by age (value = confidence * exp(-age/tau)), materialized at write
time. The hot path consumes the result as an ordinary FeatureRecord — the
feature store is the only join (ADR-001).

TAU and validity must match docs/feature-manifest.md (enforced by tests).
Signal types with no model feature (supply_disruption,
competitor_description_change) are signal-only: recorded, never fed to the
model until the manifest gains a slot for them.
"""
from __future__ import annotations

import math
from datetime import datetime

from pricing.intel.ports import Source

# signal_type -> (feature_name, tau_minutes, validity_hours)
SIGNAL_FEATURE_MAP: dict[str, tuple[str, int, int]] = {
    "competitor_stockout": ("signal_competitor_stockout", 120, 24),
    "competitor_price_change": ("signal_competitor_price_change", 60, 24),
    "seasonal_promotion": ("signal_seasonal_promotion", 360, 72),
    "demand_surge": ("signal_demand_surge", 120, 24),
}
SIGNAL_ONLY_TYPES = ("supply_disruption", "competitor_description_change")

POLARITY_BY_TYPE = {
    "competitor_stockout": "positive",       # their stockout helps us
    "competitor_price_change": "negative",   # their cut pressures us
    "seasonal_promotion": "negative",
    "demand_surge": "positive",
    "supply_disruption": "positive",
    "competitor_description_change": "neutral",
}


def decayed_value(confidence: float, observed_at: datetime, now: datetime,
                  tau_minutes: int) -> float:
    age_minutes = max(0.0, (now - observed_at).total_seconds() / 60.0)
    return float(confidence) * math.exp(-age_minutes / tau_minutes)


def signal_to_feature(signal: dict, now: datetime) -> tuple[str, str, float, int] | None:
    """Return (entity_key, feature_name, value, ttl_ms) or None (signal-only).

    Category-level signals map to a `category:` entity — the hot path reads
    `sku:` entities, so they don't affect pricing in this slice (reserved for
    category-level model features, see feature manifest).
    """
    mapping = SIGNAL_FEATURE_MAP.get(signal["signal_type"])
    if mapping is None:
        return None
    feature_name, tau_minutes, validity_hours = mapping
    subject = signal["subject"]
    if "sku" in subject:
        entity_key = f"sku:{subject['sku']}"
    else:
        entity_key = f"category:{subject.get('category', 'general')}"
    observed = datetime.fromisoformat(signal["observed_at"])
    value = decayed_value(float(signal["confidence"]), observed, now, tau_minutes)
    ttl_ms = validity_hours * 3_600_000
    return entity_key, feature_name, value, ttl_ms


def build_candidate(
    *,
    signal_type: str,
    source: Source,
    doc_id: str,
    extracted_by: str,
    model: str,
    trace_id: str,
    observed_at: datetime,
    confidence: float,
    subject: dict,
    magnitude: float | None = None,
) -> dict:
    """Assemble a raw dict shaped exactly like the MarketSignal contract.

    Provenance and source_trust are set HERE, from the pipeline's own
    context — extractor output can never claim its own trust level.
    """
    _, _, validity_hours = SIGNAL_FEATURE_MAP.get(signal_type, (None, 0, 24))
    return {
        "signal_id": _uuid(),
        "signal_type": signal_type,
        "subject": subject,
        "polarity": POLARITY_BY_TYPE.get(signal_type, "neutral"),
        "magnitude": magnitude,
        "observed_at": observed_at.isoformat(),
        "confidence": float(confidence),
        "source_trust": float(source.trust),
        "validity_hours": validity_hours or 24,
        "provenance": {
            "doc_id": doc_id,
            "extractor": extracted_by,
            "model": model,
            "trace_id": trace_id,
            "prompt_version": extracted_by,
        },
        "evidence_span": "",
    }


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())
