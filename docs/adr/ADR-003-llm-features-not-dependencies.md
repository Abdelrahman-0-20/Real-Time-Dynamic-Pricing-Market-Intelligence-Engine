# ADR-003: LLM output is a *feature*, never a *dependency*

## Status
Proposed — **amended by ADR-007**: business decision added a narrow signal-triggered repricing channel. The LLM still never *blocks* serving (availability claim intact), but two closed signal types can now move prices directly under ADR-007's rails (corroboration, guardrails, rate limits, auto-kill).

## Context
The Generative AI layer converts news/competitor descriptions into structured signals ("competitor stockout", "seasonal promotion"). The temptation is to treat these as authoritative events that trigger repricing. But LLMs hallucinate, time out, get rate-limited, and can be prompt-injected by scraped pages — and every one of those failures would then reach customer-visible prices.

## Decision
- LLM-extracted signals are **written only as decayable features** in the feature store, with `confidence`, `event_time`, and a decay function (`confidence × exp(-Δt/τ)`). They influence prices the way any stale feature does — softly, and with visibility.
- The extraction pipeline has **no write access to prices, no tools, and no path around the schema gate.** Its only effect on the system is `MarketSignal` records that pass JSON-Schema validation + confidence threshold.
- If extraction stops entirely, pricing continues on decaying features until they age out — an expected mode, not an incident.

## Consequences
- **Easier:** hallucination blast radius is bounded to one feature with a TTL; injection attempts (imperative text, tool-name smuggling) fail schema validation and land in quarantine.
- **Easier:** extraction can be paused, re-prompted, or re-run retroactively without touching serving.
- **Harder:** market signals react in minutes, not milliseconds — acceptable, since competitor price changes are not per-request events anyway.
- **Harder:** we must build decay + confidence plumbing into the feature store (one mechanism, reused for all stale data).

## Alternatives rejected
- *LLM as an agent that can call repricing:* violates least privilege and the 50 ms SLA simultaneously; unauditable.
- *Trust extraction above a threshold automatically:* confidence scores from LLMs are miscalibrated (see eval requirement §7); threshold + decay + sampling review instead.
