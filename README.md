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

## Enforced in CI (step 2)

```bash
python -m pytest -q     # schema gate + adversarial fixtures + ADR-002 dependency rule
```

- **[tests/](tests/)** — fixtures for every contract: valid documents must pass, invalid/adversarial ones (injected instructions, low confidence, guardrail bypass, hidden staleness) must fail the gate.
- **[.importlinter](.importlinter)** — ADR-002's dependency rule as config; mirrored by an AST test so it holds before the package is installable.

## Status

- **Step 1 (business decisions): complete** — see [ARCHITECTURE §10](docs/ARCHITECTURE.md) for the four decisions and their design effects.
- **Step 2 (contract enforcement): complete** — schemas are executable gates, not prose.
- **Next: step 3** — the hot-path vertical slice (Kafka → Flink → cache → XGBoost → guardrails → DecisionRecord) with the p99 latency harness.
