# ADR-005: XGBoost as the serving model, neural net in shadow; versioned promotion with auto-rollback

## Status
Proposed

## Context
Requirements: <15 ms inference inside a 50 ms budget, tabular features (velocities, windows, competitor deltas, decayed signals), fast iteration, and monitored revenue/latency drift. Both XGBoost and a neural net were named in the brief.

## Decision
- **Primary serving model: XGBoost**, loaded in-process (or ONNX-exported) — ~2–5 ms inference, interpretable feature contributions for `pricingctl price explain`, robust on tabular data with mixed staleness.
- **Neural net runs in shadow:** same feature snapshot, logged-only scoring, compared offline against XGBoost. It promotes only by beating the baseline on backtest + canary with identical guardrails.
- **Promotion is a pipeline, not a button:** backtest → shadow → canary 5% → full, each with automated kill criteria; **promotion requires blocking HITL approval** with the eval report attached.
- **Auto-rollback:** revenue/margin drift > 2% vs. control over 24 h (or p99 latency breach) reverts to the previous registry version and pages on-call.

## Consequences
- **Easier:** latency budget has comfortable headroom; explainability requirement is met natively (SHAP/contribution output feeds the HITL review interface).
- **Easier:** shadow comparison de-risks the neural net without customer exposure; rollback makes promotion reversible.
- **Harder:** two models to maintain and compare; shadow scoring costs compute (accepted — it's offline and cheap relative to serving).
- **Harder:** "revenue drift" needs a **control group / holdout** to be measurable at all — an open question (ARCHITECTURE §10.3) that must be settled before monitoring is meaningful.

## Alternatives rejected
- *Neural net as primary:* higher latency variance, weaker tabular performance out-of-the-box, no native contributions — revisit only if shadow results clearly win.
- *Remote model server (Triton) for XGBoost:* adds a network hop to the hot path; in-process wins on latency, loses on model-hot-reload smoothness (mitigated by cache-friendly versioned swap).
