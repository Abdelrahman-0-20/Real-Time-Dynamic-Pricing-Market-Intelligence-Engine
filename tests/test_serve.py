"""End-to-end serve path: every decision is schema-valid, clamped, auditable.

The failure questions from the multi-agent review, answered as tests:
  * what happens when a feature is stale?            -> flagged, not hidden
  * when a critical feature dies?                    -> degraded rule-based price
  * when the model server fails?                     -> fallback, still a price
  * when the market pack is unknown?                 -> stricter pack + degraded
  * when the snapshot has no anchors at all?         -> honest error, not a fake price
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pricing.core.decision import policy_arm
from tests.helpers import (
    StubModel,
    load_dictionary,
    make_service,
    seed_sku,
    validate_record,
)


def test_happy_path_produces_schema_valid_decision(tiny_artifacts: Path) -> None:
    ctx = make_service(tiny_artifacts)
    seed_sku(ctx, "SKU-00001", row=3)
    record = ctx.service.price("SKU-00001")

    validate_record(record)
    assert record["trigger"] == "model"
    assert record["degraded"] is False
    assert record["unclamped_price"] is not None
    assert record["fingerprint"].startswith("sha256:")
    assert record["policy_arm"] in {"treatment", "holdout"}
    assert record["policy_pack"] == {"market": "EG", "version": "v0", "status": "placeholder"}
    assert len(ctx.log.records) == 1

    # Served price must live inside the policy band.
    features = record["model_input"]["features"]
    floor = features["base_cost"] * 1.15
    ceiling = features["category_median_price"] * 1.3
    assert floor <= record["served_price"] <= ceiling


def test_extreme_model_output_is_clamped(tiny_artifacts: Path) -> None:
    ctx = make_service(tiny_artifacts, model=StubModel(propose=1000.0))
    seed_sku(ctx, "SKU-00002", row=7)
    record = ctx.service.price("SKU-00002")

    validate_record(record)
    assert record["unclamped_price"] == 1000.0
    assert record["served_price"] < 1000.0
    assert record["guardrail_actions"], "clamp must be recorded, not silent"
    features = record["model_input"]["features"]
    floor = features["base_cost"] * 1.15
    ceiling = features["category_median_price"] * 1.3
    assert floor <= record["served_price"] <= ceiling


def test_stale_non_critical_feature_is_flagged_not_hidden(tiny_artifacts: Path) -> None:
    ctx = make_service(tiny_artifacts)
    ttl = ctx.dictionary.specs["competitor_price"].ttl_ms
    seed_sku(ctx, "SKU-00003", row=1, overrides={"competitor_price": (124.5, 2 * ttl)})
    record = ctx.service.price("SKU-00003")

    validate_record(record)
    assert "competitor_price" in record["stale_features"]
    assert record["trigger"] == "model"
    assert record["degraded"] is False


def test_dead_critical_feature_falls_back_to_rule_based_price(tiny_artifacts: Path) -> None:
    ctx = make_service(tiny_artifacts)
    ttl = ctx.dictionary.specs["inventory_level"].ttl_ms
    seed_sku(ctx, "SKU-00004", row=2, overrides={"inventory_level": (10, 4 * ttl)})
    record = ctx.service.price("SKU-00004")

    validate_record(record)
    assert "inventory_level" in record["dropped_features"]
    assert record["trigger"] == "rule_fallback"
    assert record["unclamped_price"] is None
    assert record["degraded"] is True
    # Fallback anchors on price_last, still clamped into the band.
    features = record["model_input"]["features"]
    assert record["served_price"] >= features["base_cost"] * 1.15


def test_model_server_failure_still_produces_a_price(tiny_artifacts: Path) -> None:
    ctx = make_service(tiny_artifacts, model=StubModel(fail=True))
    seed_sku(ctx, "SKU-00005", row=4)
    record = ctx.service.price("SKU-00005")

    validate_record(record)
    assert record["trigger"] == "rule_fallback"
    assert record["degraded"] is True
    assert record["served_price"] > 0


def test_unknown_market_pack_flags_degraded(tiny_artifacts: Path) -> None:
    ctx = make_service(tiny_artifacts, pack_fallback=True)
    seed_sku(ctx, "SKU-00006", row=5)
    record = ctx.service.price("SKU-00006")

    validate_record(record)
    assert record["degraded"] is True
    assert record["policy_pack"]["status"] == "placeholder"  # pack still applied


def test_snapshot_without_any_anchor_raises_with_a_fix(tiny_artifacts: Path) -> None:
    ctx = make_service(tiny_artifacts)
    with pytest.raises(ValueError, match="Fix:"):
        ctx.service.price("SKU-404")


def test_fingerprint_is_replayable_but_decision_ids_are_unique(tiny_artifacts: Path) -> None:
    ctx = make_service(tiny_artifacts)
    seed_sku(ctx, "SKU-00007", row=9)
    first = ctx.service.price("SKU-00007")
    second = ctx.service.price("SKU-00007")

    assert first["fingerprint"] == second["fingerprint"]  # same inputs => same decision
    assert first["decision_id"] != second["decision_id"]  # but distinct audit entries


def test_holdout_assignment_is_deterministic_and_about_five_percent() -> None:
    arms = [policy_arm(f"SKU-{i:05d}") for i in range(2000)]
    assert all(policy_arm(f"SKU-{i:05d}") == arms[i] for i in range(2000))
    holdouts = sum(1 for a in arms if a == "holdout")
    assert 40 < holdouts < 170, f"holdout share off: {holdouts}/2000"


def test_record_carries_full_staleness_story(tiny_artifacts: Path) -> None:
    """stale + dropped + model_input must be internally consistent."""
    ctx = make_service(tiny_artifacts)
    ttl = ctx.dictionary.specs["competitor_price"].ttl_ms
    seed_sku(
        ctx, "SKU-00008", row=6,
        overrides={"competitor_price": (111.0, 2 * ttl), "supply_eta_hours": None},
    )
    record = ctx.service.price("SKU-00008")
    validate_record(record)

    features = record["model_input"]["features"]
    assert features.get("competitor_price") == pytest.approx(111.0)  # stale but served
    assert "competitor_price" in record["stale_features"]
    assert "supply_eta_hours" in record["dropped_features"]
    assert "supply_eta_hours" not in features
    assert record["degraded"] is False  # supply_eta is non-critical
