# Real-Time Dynamic Pricing & Market Intelligence Engine

Design package: merges real-time streaming telemetry with scraped web/news intelligence to serve optimal prices in **< 50 ms p99**, with an LLM extraction layer that informs the pricing model — and, under strict rails, can trigger repricing directly.

## Read in this order

1. **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — problem & constraints, bounded contexts + context map, C4 container view, the 50 ms latency budget, failure modes & fallback chains, HITL gates, MLOps, observability, the decision record, resolved business decisions.
2. **[docs/adr/](docs/adr/)** — the eight decisions that carry the design, each with trade-offs and rejected alternatives:
   - ADR-001 hot/cold path split (feature store as the only join point)
   - ADR-002 hexagonal pricing core (guardrails as pure, testable rules)
   - ADR-003 LLM output is a feature, never a *blocking* dependency *(amended by ADR-007)*
   - ADR-004 local feature cache (freshness over consistency)
   - ADR-005 XGBoost primary, neural in shadow, auto-rollback promotion
   - ADR-006 extraction topology: fan-out across documents, not across agents
   - ADR-007 signal-triggered repricing — narrow, rail-guarded direct-effect channel
   - ADR-008 regulatory guardrails as per-market policy packs (data, not code)
3. **[docs/contracts/](docs/contracts/)** — five JSON Schemas defining every boundary (telemetry, documents, signals, features, decisions), enforced by CI.
4. **[docs/pricingctl.md](docs/pricingctl.md)** — the operator CLI: verb-noun commands, dual human/JSON output, stable exit codes, <100 ms startup, and the `price explain` HITL review view.

## Enforced in CI

```bash
python -m pytest -q                    # 69 tests: schema gate, hexagonal boundary, guardrails, serve path, training, harness
lint-imports                           # 4 contracts: adapters→core, app→core, core/app have no vendor imports
python -m pricing.training.train       # baseline XGBoost + anti-leakage control model
python benchmarks/latency.py           # p99 gate: PASS at 0.8 ms vs 50 ms budget (dev hardware)
```

- **[tests/](tests/)** — fixtures for every contract (valid pass, adversarial fail for the right reason) plus end-to-end serve-path tests: stale flagged, critical-dead → rule-based fallback, model failure → still a price, unknown market → stricter pack, schema-valid DecisionRecord every time.
- **[.importlinter](.importlinter)** — ADR-002's dependency rule as config, mirrored by AST tests.
- **[benchmarks/latency.py](benchmarks/latency.py)** — protocol from [docs/latency-harness.md](docs/latency-harness.md): stage-level p99, blame on failure, `--json` for pipes, exit codes as gates.

## Status

- **Step 1 (business decisions): complete** — see [ARCHITECTURE §10](docs/ARCHITECTURE.md).
- **Step 2 (contract enforcement): complete** — schemas are executable gates.
- **Step 3 (hot-path vertical slice): complete** — synthetic market → trained XGBoost → in-memory ports → guardrails → schema-valid DecisionRecord, latency gate green (p99 0.8 ms local, code path only).
- **Step 4 (cold path): complete** — [ADR-009](docs/adr/ADR-009-cold-path-local-adapters.md): crawl (ToS gate + circuit breaker) → parse → injection gate → lexicon extraction (the ADR-006 tier-3 fallback, production-tested) → schema/confidence gate → contradiction resolution → `MarketSignal` → decayed feature written to the **same store the hot path reads** (proven by `test_hot_path_prices_with_the_extracted_signal_feature`).

```bash
python -m pricing.intel.run            # crawl reviewed seed sources; exit 3 = quarantines happened
python -m pricing.intel.run --json     # pipe-safe stats
```

- **Next: step 5** — real infrastructure adapters behind the existing ports (Kafka/Flink → feature store, Redis cache, Docling, LLM extractor per ADR-006, Airflow DAG wrapping `pricing.intel.run`), plus `pricingctl` per [docs/pricingctl.md](docs/pricingctl.md).
