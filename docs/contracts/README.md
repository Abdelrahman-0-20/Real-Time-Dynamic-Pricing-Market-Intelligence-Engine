# Contracts

Published, versioned boundaries between bounded contexts. **Schemas are the contract — not shared tables, not prose.**

| Schema | Upstream → Downstream | Rule |
|---|---|---|
| [telemetry-event](telemetry-event.schema.json) | Producers (POS/WMS) → Telemetry/Flink | `event_id` idempotent; `event_time` drives point-in-time correctness |
| [market-document](market-document.schema.json) | Market Intelligence → Signal Extraction | immutable, content-hashed, provenance-carrying; **hostile data** |
| [market-signal](market-signal.schema.json) | Signal Extraction → Feature & Serving | **closed** signal-type enum; gate ≥ 0.7 confidence; everything else is quarantined |
| [feature-record](feature-record.schema.json) | all writers → Feature & Serving (sole writer) | staleness (`event_time` + `ttl_ms`) is mandatory and never hidden |
| [decision-record](decision-record.schema.json) | Pricing Decision → audit/training/monitors | full model input snapshot ⇒ exact replay |

## Evolution rules

- Additive optional fields: minor version, no coordination.
- Renaming/removing a field, or renaming a `feature_name` (model input contract): **breaking** → bump major, dual-read during migration, deprecation window before removal.
- CI validates every producer against its schema; a failing producer build is a failed build, not a runtime surprise.
