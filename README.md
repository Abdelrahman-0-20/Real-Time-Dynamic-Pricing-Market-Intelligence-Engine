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
- **Step 5a (`pricingctl` + eval harness): complete** — operator CLI ([spec](docs/pricingctl.md), 20 ms startup, stable exit codes, `price explain` as the HITL review surface with SHAP contributions) and the extraction eval gate (27 labeled cases, recorded baseline, negative-tested so it can actually fail).
- **Step 5b part 1 (real stream + real transport): complete** — [ADR-010](docs/adr/ADR-010-no-infra-adapters.md) on a no-Docker box: **real HTTP crawl** against a live local server (allowlist, redirect refusal, e2e signals), **windowing as pure functions** (the Flink job's logic, unit-tested before any cluster), **schema-gated JSONL event log → runner → feature store → priced DecisionRecord** (telemetry join proven), **real `redis-py` adapter tested against fakeredis**, **real `confluent-kafka` adapter with broker-gated integration test** (`KAFKA_BROKER` flips it on).
- **Step 5b part 2 (flip-on): complete** — Docker installed (28 GB free), `docker-compose.yml` stack up (Redis 7 + Redpanda): **165 passed, 0 skipped** against real servers; ADR-010 rows deleted per contract (config event, no code rewrite). The flip itself caught a real defect: consumer now bounds `UNKNOWN_TOPIC` tolerance then fails loud.

```bash
./pricingctl status                       # model · features · latency · intel · sources
./pricingctl price explain --sku SKU-4471 # the WHY: features, clamps, contributions, trace id
./pricingctl crawl run --dry-run          # preview sources, fetch nothing
./pricingctl eval run                     # extraction quality gate vs evals/baseline.json
./pricingctl bench --n 5000               # latency gate (p99 < 50 ms)
python -m pricing.intel.run               # cold path directly (cron/Airflow entry)
```

- **Next: step 5b (blocked on infra decision)** — real adapters behind the existing ports: Kafka/Flink → feature store, Redis cache, Docling + LLM (ADR-006), Airflow DAG. Needs Docker or a dev cluster. Then `pricingctl completion` and shadow/canary + drift monitors (needs real traffic).
