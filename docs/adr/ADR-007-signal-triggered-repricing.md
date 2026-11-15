# ADR-007: Signal-triggered repricing (narrow, rail-guarded exception to ADR-003)

## Status
Accepted (business decision 2026-11-15) — **amends ADR-003**: the LLM layer remains non-blocking for serving, but gains one narrow direct-effect channel.

## Context
Decision from step 1: *"certain signal types trigger immediate repricing outside the model."* This buys the fastest possible reaction to market events (competitor stockout → capture demand within seconds) — at the cost of the largest blast radius in the system: **a hallucinated stockout signal now overprices a SKU and destroys demand until someone notices.** The design must make that failure bounded, visible, and killable.

## Decision
Signal-triggered repricing exists, but only through all of these rails simultaneously:

1. **Closed trigger set** — only `competitor_stockout` and `competitor_price_change` may trigger (both already in the closed `signal_type` enum). All other signal types remain decayed-features-only.
2. **Corroboration threshold** — a trigger requires `confidence ≥ 0.90` **and** (`source_trust ≥ 0.8` **or** a second, independent signal of the same type on the same SKU within 15 min). Single low-trust sources can never move a price directly.
3. **Guardrails still apply** — the triggered price passes the same pure-function guardrail chain as model output (`margin_*`, `max_step_pct`, `regulatory_clamp`, band). A trigger can never bypass a clamp.
4. **Rate limits** — max 1 triggered repricing per SKU per 10 min; per-source trigger-rate circuit breaker (5 triggers/min/source → OPEN 15 min → HALF-OPEN) so one noisy crawler can't sweep the catalog.
5. **Automatic kill** — if the `signal_trigger` arm's revenue degrades > 2% vs. the 5% holdout arm over 6 h, triggers are disabled automatically and page on-call. `pricingctl signals triggers --disable` is the manual kill switch.
6. **Full audit** — every triggered price is a `DecisionRecord` with `trigger: "signal_trigger"`, the triggering `signal_id`, and the corroboration evidence; 100% advisory sampling for the first 30 days, then adaptive (rises with error rate).
7. **Graceful absence** — if extraction dies entirely, triggers stop and the model keeps pricing. The LLM is an *additional* trigger source, never a required dependency (ADR-003's availability claim still holds).

## Consequences
- **Easier:** fastest reaction to real market events; the closed enum + corroboration makes injection-driven repricing require forging *two* trusted sources.
- **Easier:** every triggered decision is replayable and attributable to a signal_id.
- **Harder:** a *consistent* bad source (two compromised/erroneous sources agreeing) can still trigger — bounded by guardrails, rate limits, and the automatic kill, but not eliminated. Accepted per business decision.
- **Harder:** one more monitored arm (trigger revenue vs holdout) and a new failure mode to add to evals: "triggered price was wrong" cases in the extraction eval suite.

## Rejected alternatives
- *All signal types trigger:* wider blast radius, no added speed — rejected.
- *Triggers bypass guardrails for "market speed":* unacceptable — clamps are the only hard safety net left.
- *Confidence-only threshold (no corroboration):* LLM confidence is miscalibrated (ADR-005/§7), so corroboration is required instead.
