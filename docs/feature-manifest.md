# Feature Manifest — `model_input_version: fv-2026.11`

The frozen feature contract for the pricing model. **Adding, renaming, or changing the TTL semantics of a feature here is a breaking change** (contracts/README evolution rules): training and serving must move together, or training/serving skew begins silently.

Every feature is a `FeatureRecord` (see [feature-record.schema.json](contracts/feature-record.schema.json)) and appears in `DecisionRecord.model_input` at serve time.

## Streaming features (source: `telemetry_flink`)

| Feature | Type | TTL | Criticality | Notes |
|---|---|---|---|---|
| `demand_rate_1h` | number | 5 min | normal | units/hour, Flink tumble window |
| `demand_rate_24h` | number | 30 min | normal | baseline velocity |
| `inventory_level` | number | 1 min | **critical** | stale ≥ 3×TTL ⇒ degrade to rule-based fallback (never impute) |
| `sell_through_24h` | number | 30 min | normal | |
| `supply_eta_hours` | number | 60 min | normal | null when no inbound shipment |

## Static / slow features (source: `crawler_direct` or batch)

| Feature | Type | TTL | Criticality | Notes |
|---|---|---|---|---|
| `base_cost` | number | 24 h | **critical** | margin floor depends on it |
| `price_last` | number | 5 min | normal | last served price; also the step-clamp reference |
| `category_median_price` | number | 60 min | normal | ceiling reference |

## Market intelligence features (source: `crawler_direct`)

| Feature | Type | TTL | Criticality | Notes |
|---|---|---|---|---|
| `competitor_price` | number | 30 min | normal | flagged stale between TTL and 3×TTL |
| `competitor_price_delta_pct` | number | 30 min | normal | vs. our `price_last` |
| `competitor_in_stock` | boolean | 30 min | normal | raw page signal, pre-LLM |

## LLM signal features (source: `llm_signal`, confidence-decayed)

Decay: `served_value = confidence × exp(-Δt/τ)`; feature dropped at `validity_hours` (ADR-003/007).

| Feature | Type | τ | validity | Can trigger repricing? |
|---|---|---|---|---|
| `signal_competitor_stockout` | 0..1 | 120 min | 24 h | **yes** (ADR-007, with corroboration) |
| `signal_competitor_price_change` | 0..1 | 60 min | 24 h | **yes** (ADR-007, with corroboration) |
| `signal_seasonal_promotion` | 0..1 | 360 min | 72 h | no — decayed feature only |
| `signal_demand_surge` | 0..1 | 120 min | 24 h | no — decayed feature only |

## Policy fields (not model inputs)

| Field | Rule |
|---|---|
| `policy_arm` | `holdout` if `hash(sku) % 100 < 5`, else `treatment` — deterministic, stable across runs, recorded on every DecisionRecord |
| guardrail pack | EG pack v0 (status: `placeholder`, ADR-008) — see `config/guardrails/EG.json` |

## Skew prevention

- Training sets join history **as-of `event_time`** with this exact manifest version.
- CI validates that every feature name in this manifest matches `[a-z][a-z0-9_]*` and exists in the serving feature dictionary (added in step 3).
- Renaming without bumping `model_input_version` = CI failure.
