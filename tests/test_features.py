"""Feature dictionary + freshness contract (ARCHITECTURE §4.2) as code."""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from pricing.core.features import FeatureDictionary, StoredFeature, classify
from pricing.training.synthetic import FEATURE_ORDER
from tests.helpers import CONFIG, ROOT, load_dictionary

NOW = datetime(2026, 11, 15, 8, 0, 0, tzinfo=timezone.utc)


def _dict_with(features: dict) -> FeatureDictionary:
    return FeatureDictionary.from_dict({"model_input_version": "fv-test", "features": features})


def test_fresh_feature_is_used_silently() -> None:
    d = _dict_with({"competitor_price": {"ttl_ms": 1000}})
    snap = {"competitor_price": StoredFeature(124.5, NOW - timedelta(milliseconds=500), 1000)}
    result = classify(snap, d, NOW)
    assert result.model_input["competitor_price"] == pytest.approx(124.5)
    assert result.stale == [] and result.dropped == [] and result.degraded is False


def test_stale_between_ttl_and_3x_is_flagged_but_served() -> None:
    d = _dict_with({"competitor_price": {"ttl_ms": 1000}})
    snap = {"competitor_price": StoredFeature(124.5, NOW - timedelta(seconds=2), 1000)}
    result = classify(snap, d, NOW)
    assert "competitor_price" in result.stale
    assert "competitor_price" in result.model_input
    assert result.degraded is False  # flagged, not hidden, not fatal


def test_critical_feature_past_3x_ttl_degrades_the_decision() -> None:
    d = _dict_with({"inventory_level": {"ttl_ms": 1000, "critical": True}})
    snap = {"inventory_level": StoredFeature(10, NOW - timedelta(seconds=4), 1000)}
    result = classify(snap, d, NOW)
    assert "inventory_level" in result.dropped
    assert result.degraded is True


def test_non_critical_feature_past_3x_ttl_is_dropped_without_degrading() -> None:
    d = _dict_with({"competitor_price": {"ttl_ms": 1000}})
    snap = {"competitor_price": StoredFeature(124.5, NOW - timedelta(seconds=4), 1000)}
    result = classify(snap, d, NOW)
    assert "competitor_price" in result.dropped
    assert result.degraded is False


def test_missing_critical_feature_from_store_degrades() -> None:
    d = _dict_with({"base_cost": {"ttl_ms": 1000, "critical": True}})
    result = classify({}, d, NOW)
    assert result.degraded is True
    assert "base_cost" in result.dropped


def test_nan_critical_value_degrades_rather_than_banding_off_nan() -> None:
    d = _dict_with({"base_cost": {"ttl_ms": 1000, "critical": True}})
    snap = {"base_cost": StoredFeature(float("nan"), NOW, 1000)}
    result = classify(snap, d, NOW)
    assert result.degraded is True
    assert "base_cost" in result.dropped


def test_feature_absent_from_manifest_is_contract_drift() -> None:
    d = _dict_with({"base_cost": {"ttl_ms": 1000, "critical": True}})
    snap = {"rogue_feature": StoredFeature(1.0, NOW, 1000)}
    result = classify(snap, d, NOW)
    assert "rogue_feature" in result.dropped
    assert result.degraded is True
    assert "rogue_feature" not in result.model_input


def test_dictionary_validation_errors_name_the_fix() -> None:
    with pytest.raises(ValueError, match="Fix:"):
        FeatureDictionary.from_dict({"features": {"x": {"ttl_ms": 1}}})  # no version
    with pytest.raises(ValueError, match="Fix:"):
        FeatureDictionary.from_dict(
            {"model_input_version": "v", "features": {"Bad Name!": {"ttl_ms": 1}}}
        )
    with pytest.raises(ValueError, match="Fix:"):
        FeatureDictionary.from_dict(
            {"model_input_version": "v", "features": {"x": {"ttl_ms": -1}}}
        )


def test_config_dictionary_matches_training_and_manifest() -> None:
    """Three sources of truth must agree: config, training, prose manifest."""
    dictionary = load_dictionary()
    manifest = (ROOT / "docs" / "feature-manifest.md").read_text()

    assert set(dictionary.specs) == set(FEATURE_ORDER), (
        "config/features/fv-2026.11.json and training FEATURE_ORDER diverged"
    )
    for name in dictionary.specs:
        assert f"`{name}`" in manifest, f"feature '{name}' missing from docs/feature-manifest.md"
    assert dictionary.version in manifest, "model_input_version missing from the manifest"
    assert dictionary.version == "fv-2026.11"
