"""Signal -> feature conversion: decay math and manifest consistency."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pricing.intel.features import (
    SIGNAL_FEATURE_MAP,
    decayed_value,
    signal_to_feature,
)
from tests.helpers import load_dictionary

NOW = datetime(2026, 11, 15, 8, 0, 0, tzinfo=timezone.utc)


def _signal(signal_type="competitor_stockout", subject=None, hours_ago=0.0,
            confidence=0.92) -> dict:
    return {
        "signal_type": signal_type,
        "subject": subject or {"sku": "SKU-4471"},
        "confidence": confidence,
        "observed_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
    }


def test_decay_halves_at_one_tau_for_confidence_one() -> None:
    value = decayed_value(1.0, NOW - timedelta(minutes=120), NOW, tau_minutes=120)
    assert value == pytest.approx(0.3679, rel=1e-3)  # exp(-1)


def test_fresh_signal_keeps_full_confidence() -> None:
    value = decayed_value(0.9, NOW, NOW, tau_minutes=120)
    assert value == pytest.approx(0.9)


def test_signal_maps_to_manifest_feature_with_manifest_ttl() -> None:
    entity, name, value, ttl_ms = signal_to_feature(_signal(hours_ago=2), NOW)
    dictionary = load_dictionary()

    assert entity == "sku:SKU-4471"
    assert name == "signal_competitor_stockout"
    assert name in dictionary.specs, "feature not present in fv-2026.11 dictionary"
    assert ttl_ms == 24 * 3_600_000 == dictionary.specs[name].ttl_ms
    assert value < 0.92  # two hours in, decay has bitten


def test_signal_only_types_are_not_model_features() -> None:
    assert signal_to_feature(_signal(signal_type="supply_disruption"), NOW) is None
    assert signal_to_feature(
        _signal(signal_type="competitor_description_change"), NOW
    ) is None


def test_category_subject_maps_to_category_entity() -> None:
    entity, *_ = signal_to_feature(
        _signal(subject={"category": "electronics"}), NOW
    )
    assert entity == "category:electronics"


def test_every_mapped_feature_exists_in_the_dictionary() -> None:
    dictionary = load_dictionary()
    for signal_type, (feature_name, _, _) in SIGNAL_FEATURE_MAP.items():
        assert feature_name in dictionary.specs, (
            f"{signal_type} maps to '{feature_name}', absent from fv-2026.11"
        )
