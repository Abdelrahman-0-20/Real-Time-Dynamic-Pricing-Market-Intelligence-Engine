# ADR-001: Split hot path (streaming) and cold path (scraping/LLM), join only at the Feature Store

## Status
Proposed

## Context
The system merges two data sources whose freshness SLAs differ by four orders of magnitude: telemetry (~10–100 ms) and scraped market intel (~15 min–6 h, plus LLM extraction time). The serve SLA is p99 < 50 ms. A naive "everything goes through one pipeline" design either blocks prices on scrapes/LLM calls (blows the SLA) or forces the streaming path to wait on batch semantics (loses freshness).

## Decision
Run **two asynchronous pipelines** that never call each other at request time:

- **Hot path:** Kafka → Flink → feature materializer → online feature store → local cache → inference → guardrails. Bounded, no I/O to cold-path systems.
- **Cold path:** Airflow → crawler → Docling → Vector DB → LLM extraction → schema gate → `MarketSignal` topic → feature writer → same feature store.
- The **online feature store is the only join point**. Cold-path output arrives as feature updates with `event_time` + TTL; the hot path reads them as ordinary features.

## Consequences
- **Easier:** the pricing API's dependency graph never includes Airflow, a Vector DB, or an LLM; cold-path outages degrade features (via decay), not availability.
- **Easier:** each pipeline scales, deploys, and fails independently; LLM quota issues can't page the serving on-call.
- **Harder:** we give up strong consistency — a price may reflect a market fact that is minutes old. Mitigated by explicit staleness flags in every decision record.
- **Harder:** we give up *real-time* reaction to breaking news. News can only move prices at the next extract cycle.
- **Harder:** debugging spans two pipelines — requires the shared `trace_id`/`event_time` discipline (ARCHITECTURE §8) to correlate.

## Alternatives rejected
- *LLM call in the request path:* violates the 50 ms SLA outright; adds a non-deterministic dependency to a financial decision.
- *Single Flink job for both:* forces crawl/LLM lateness semantics onto the streaming engine; couples release cadences.
