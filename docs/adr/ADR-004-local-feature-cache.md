# ADR-004: Local feature cache in the serving process (freshness over consistency)

## Status
Proposed

## Context
The <50 ms p99 budget allows ≤ 12 ms for feature acquisition. A synchronous lookup against a remote online feature store costs 3–8 ms typical, far worse at p99 under load, plus serialization — and any store hiccup becomes a pricing outage.

## Decision
Each `pricing-api` instance runs an **in-process feature cache** (LRU + TTL, single-flight fills) that holds the hot feature set for its active SKUs:

- Cache is populated at startup (warmup of top SKUs) and refreshed asynchronously; the request path reads local memory only.
- Remote store is contacted only on miss/expiry, off the request budget.
- Every cached entry retains `event_time`/`ttl_ms` so the freshness contract (ARCHITECTURE §4.2) still applies to *cached* data — the cache may serve stale values, never *hidden* staleness.
- On remote-store failure: serve from cache with declared staleness → drop features past `3×ttl` → rule-based fallback.

## Consequences
- **Easier:** feature acquisition drops to ~3–8 ms with a bounded worst case; the SLA becomes achievable; store outages become degradation, not downtime.
- **Easier:** cache-miss behavior is explicit (single-flight) — no stampede.
- **Harder:** memory footprint scales with active SKU × feature count; requires warmup sizing and eviction discipline.
- **Harder:** brief (seconds) windows where instances disagree on a freshly updated feature — acceptable, since decisions are individually auditable and the divergence window is bounded by refresh interval.
- **Harder:** cache invalidation on guardrail/policy change must be push-based (pub/sub invalidation), not poll-based.

## Alternatives rejected
- *Remote store on every request:* simplest, but p99 and availability both fail the constraint.
- *Embedding the whole feature store:* impossible at catalog scale; the cache holds only the active window.
