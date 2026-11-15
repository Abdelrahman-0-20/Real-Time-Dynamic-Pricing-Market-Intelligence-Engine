# Real-Time Dynamic Pricing & Market Intelligence Engine — Architecture

**Status:** Proposed (2026-11-15)
**Roles covered:** Software Architect (domain/patterns/ADRs), Multi-Agent Systems Architect (LLM pipeline, failure modes, HITL), Developer Tooling Engineer (`pricingctl` DX).

---

## 1. Problem & Constraints (before any tool choice)

We need to serve an **optimal price per SKU in <50 ms (p99)** while continuously absorbing two data sources with freshness SLAs that differ by **four orders of magnitude**:

| Source | Freshness | Volume | Failure cost if it stops |
|---|---|---|---|
| Supply/demand telemetry (Kafka → Flink) | ~10–100 ms | High, continuous | Pricing goes blind on inventory/velocity |
| Competitor pages + news (Airflow + Docling → Vector DB) | ~15 min – 6 h | Low, bursty | Pricing loses market context |
| LLM-extracted signals (JSON) | ~ minutes after crawl | Very low | Features decay gracefully; pricing continues |

**Non-negotiable constraints**

1. **p99 serve latency < 50 ms** — a network hop to a remote feature store or an LLM call inside the request path breaks this immediately.
2. **The hot path must never wait on the cold path.** Scrapes, LLM calls, and vector searches are *inputs to features*, never *dependencies of a request*.
3. **Every price decision is auditable and replayable** — features + model version + guardrails must be reconstructable after the fact.
4. **The system always produces a price.** Degraded-but-defined beats silent failure.
5. **External web content is hostile** (prompt injection). It is data, never instructions.
6. Assume a **small team** → prefer a modular control plane and few data-plane services over a mesh of microservices.

**What we are explicitly giving up** (name the trade, not just the gain):
- Giving up *strong consistency* between the feature store and raw sources — we accept bounded staleness with declared TTLs.
- Giving up *real-time reaction to news* — news can only move prices on the next crawl/extract cycle (minutes), not per-request.
- Giving up *microservice-per-bounded-context* — contexts are separate **modules with enforced dependency direction**, deployed as ~4 containers, not 9.

---

## 2. Bounded Contexts & Context Map

Six contexts. Each has one language, one owner of its invariants.

| # | Bounded context | Owns (aggregate / invariants) | Emits | Consumes |
|---|---|---|---|---|
| 1 | **Telemetry** | `ProductState`: inventory, demand rate, sell-through — *never negative, windowed* | `DemandObserved`, `InventoryChanged`, `SupplyObserved` | Kafka producers (POS, WMS) |
| 2 | **Market Intelligence** | `MarketDocument`: raw page/article, provenance, fetch time — *immutable once fetched* | `DocumentIngested` | Airflow crawler, Docling |
| 3 | **Signal Extraction** | `MarketSignal`: typed, time-boxed market facts — *schema-valid or rejected* | `MarketSignalExtracted`, `SignalQuarantined` | Market documents (via Vector DB) |
| 4 | **Feature & Serving** | `FeatureRecord`: entity-keyed values with `event_time`, `ttl_ms`, `source` — *staleness is explicit* | materialized features, `FeatureStale` | Telemetry, Signal Extraction |
| 5 | **Pricing Decision** | `PriceDecision`: price, guardrail actions, rationale — *never outside guardrail band* | `PriceDecision` (decision log) | Feature & Serving, model server |
| 6 | **MLOps & Observability** | model registry, drift monitors, decision log, eval harness | `ModelPromoted`, `DriftAlert` | everything (read-only) |

**Context map (upstream → downstream)**

```
Telemetry ────────────(conformist)──────────┐
                                            ▼
Market Intel ─(published doc contract)→ Signal Extraction ─(MarketSignal schema)→ Feature & Serving ─→ Pricing Decision
                                                                 ▲                                            │
                                              MLOps & Observability ◄─────── decision log, metrics ───────────┘
```

- **Conformist** for telemetry: Flink jobs adopt the feature-store naming directly — no translation layer, because the producer and consumer are both us.
- **Published contract** (`MarketSignal` schema) between Signal Extraction and Feature & Serving — a versioned JSON Schema, not a shared database.
- **Anti-corruption layer** at the crawler boundary: Docling/HTML output never reaches downstream models raw; the extraction pipeline translates it into `MarketSignal`.
- Feature & Serving is the **only** context allowed to write the online feature store. Pricing Decision reads it through a port (`FeaturePort`), never directly against Redis/Feast APIs.

---

## 3. C4 — Container view

```
                       ┌──────────────────────────── COLD PATH (minutes–hours) ────────────────────────────┐
                       │                                                                                   │
   competitor pages ──▶│  Airflow scheduler ─▶ Crawler workers ─▶ Docling parse ─▶ Vector DB (raw+embed)     │
   market news    ────▶│        │                                                              │            │
                       │        └── DLQ / blocked-source alert                              ▼            │
                       │                                        Extraction workers (LLM, schema-gated)     │
                       │                                              │  └─ quarantine + rule-based       │
                       │                                              ▼        fallback extractors         │
                       │                                    MarketSignal topic ──▶ Signal writer            │
                       └──────────────────────────────────────────────┼───────────────────────────────────┘
                                                                      ▼
   POS / WMS ──▶ Kafka ──▶ Flink (window, aggregate) ──────────▶ ONLINE FEATURE STORE ◀── feature materializer
                                     │                                │        ▲            (TTLs, staleness)
                                     │                                │        └── decay/merge logic
                                     ▼                                │
                              historical snapshot ──▶ training ──▶ ML Registry ──▶ MODEL SERVER (XGBoost)
                                     │                                                    │  (NN shadow)
                                     │                                                    ▼
   client app ──▶ PRICING API (stateless) ── local feature cache ──▶ inference ──▶ guardrails ──▶ response
                        │                                                         │
                        └─────────────── async append ──▶ DECISION LOG ◀─────────┘
                                                                    │
                        Observability: OTel traces (trace_id end-to-end), Prometheus metrics,
                        drift monitors (revenue, PSI, staleness, p99 latency, signal precision)
```

**4 deployable units** (small-team reality, per ADR-001/ADR-004):

| Container | Tech (suggested, reversible) | Scales on |
|---|---|---|
| `stream-worker` | Kafka + Flink | event rate |
| `intel-worker` | Airflow + crawler + Docling + extraction workers | crawl volume / LLM quota |
| `pricing-api` | stateless service + embedded model + local feature cache | request rate |
| `mlops` | registry, decision log, monitors, eval runner | (batch) |

Control plane (Airflow, MLflow, Grafana) is deliberately **out** of the serving path.

---

## 4. The <50 ms hot path — budget and design

### 4.1 Latency budget (p99, per request)

| Stage | Budget | How |
|---|---|---|
| Auth, routing, request decode | 2 ms | in-process |
| **Feature acquisition** | **≤ 12 ms** | local in-process feature cache (LRU, TTL), async refresh; remote store only on miss |
| Model inference | ≤ 15 ms | XGBoost in-process (~2–5 ms); NN served via ONNX Runtime in shadow |
| Guardrails & business rules | 1 ms | pure functions, no I/O |
| Response encode | 2 ms | — |
| **Contingency headroom** | 18 ms | jitter, GC, cache miss penalty |
| **Total** | **50 ms** | — |

**ADR-004 is load-bearing here:** a synchronous round trip to a remote feature store (typically 3–8 ms, p99 much worse) plus serialization is exactly what pushes p99 over 50 ms. The local cache is not an optimization; it is the reason the SLA is achievable.

### 4.2 Freshness contract (the hot path's honesty rule)

Every `FeatureRecord` carries `event_time`, `ttl_ms`, `source`. At serve time:

- `age < ttl` → normal use.
- `ttl ≤ age < 3×ttl` → serve the model, but flag `stale_features: [names]` in the decision record.
- `age ≥ 3×ttl` → **do not** silently impute: the feature is dropped, the model runs with the documented missing-value path, and the decision is flagged `degraded`. For a critical feature (e.g. inventory), degrade all the way to the rule-based fallback.

Stale-but-flagged beats fresh-and-wrong; a decision record that hides staleness is a bug.

---

## 5. Failure modes, fallback chains, circuit breakers

**Rule applied everywhere: primary → narrowed fallback → rule-based degraded → human. The system always returns a price.**

### 5.1 Serving-path failures

| Failure | Detection | Recovery path |
|---|---|---|
| Feature store unreachable | client timeout > 5 ms | serve from local cache (declared staleness) → drop stale features → rule-based pricing (margin floor/ceiling + last-good price) → alert |
| Model server timeout/5xx | > 15 ms or error | cached last price per SKU (≤ 60 s) → rule-based band price → alert; circuit breaker: 3 fails/5 attempts → OPEN 30 s → HALF-OPEN |
| Request spike / degraded p99 | latency histogram | shed: serve last-good price for tail requests, keep model for head; alert at p99 > 40 ms |
| Cache stampede on cold SKU | miss fan-out | single-flight per SKU key; others wait on the same in-flight fill or get the degraded answer |

### 5.2 Cold-path failures (must not leak into the hot path)

| Failure | Detection | Recovery path |
|---|---|---|
| **LLM extraction down / quota exhausted** | circuit breaker: 5 failures in 5 min → OPEN 60 s → HALF-OPEN | signals stop updating → features **decay** (`confidence × exp(-Δt/τ)`) → pricing unaffected. Never an error to the pricing API. |
| **LLM returns garbage / hallucinated signal** | JSON-Schema validation fails, or confidence < threshold | retry once with narrowed prompt (max 2) → rule-based extractor (lexicon/regex: "sold out", "% off") → drop with `SignalQuarantined` + alert. Max 3 iterations, hard exit. |
| **Prompt injection in scraped content** | content that yields instruction-like output (imperative verbs + tool names) or schema violation | quarantine, never write to feature store; the extractor has **no tools and no DB write access** — the writer only accepts schema-valid signals |
| **Contradictory signals** (doc A says promo, doc B says stockout) | contradiction check at write time | resolve by source-trust × recency weighting; if resolved confidence < 0.7 → **write nothing** + advisory review flag. No arbitration agent: the cost isn't warranted (see ADR-006) |
| Crawler blocked / source changed layout | parse yield < threshold, HTTP 403/429 | per-source circuit breaker, robots/ToS-aware backoff, DLQ, alert; downstream prices keep running on decaying competitor features |
| Kafka lag / Flink backpressure | consumer lag metric | staleness flags propagate automatically via `event_time`; no manual action until features age past `3×ttl` |
| Bad model promoted | drift monitors (§7) | automatic rollback to previous registry version; blocking HITL gate on promotion (§6) |
| **Hallucinated/injected signal triggers a bad price** (ADR-007) | trigger-arm revenue vs. holdout; 100% advisory sampling of triggered decisions | guardrails clamp *every* triggered price → per-SKU rate limit → per-source circuit breaker → **auto-disable triggers** + page; kill switch: `pricingctl signals triggers --disable` |

### 5.3 Cascade defense

- **Idempotency:** every write is keyed (`signal_id`, `feature_key = entity + name + event_time`, `decision_id = hash(request, model_version, feature_snapshot)`), so replays are safe.
- **Decision log is the checkpoint:** any bad model or bad feature writer can be evaluated by replaying the log — no need to "roll back" customer-visible history, only future decisions.
- **No shared mutable state** between extraction workers; each document is processed independently.

---

## 6. HITL gates (calibrated: not theater, not blind)

| Action | Gate | Why |
|---|---|---|
| Model promotion (staging → canary → full) | **Blocking approval** | Regulatory/financial blast radius; requires eval score ≥ baseline |
| Guardrail changes (margin floors, max step %, parity rules) | **Blocking approval** | Irreversible-in-effect policy change; clamps are the safety net for everything else |
| Quarantined signals (injection/hallucination suspects) | **Advisory review queue** (async) | Reversible — nothing was written; blocking would just stall the queue |
| Prices outside the "normal band" (>±20% step, or top-100 SKU moves) | **Advisory flag + adaptive sampling** (start at 5%, rise with error rate) | Blocking would destroy the product's purpose; sampling catches systematic error |
| **Signal-triggered repricing** (ADR-007) | **Advisory: 100% sampling for 30 days → adaptive** | The speed benefit dies under a blocking gate — hard rails are guardrails + rate limits + auto-kill; sampling calibrates them |
| Crawl source additions / removals | `--dry-run` + confirm in CLI | Slow, reversible, but easy to get wrong |

**HITL interface = `pricingctl price explain --sku`** (see [pricingctl.md](pricingctl.md)): shows features used + staleness, model contribution, guardrail clamps, alternatives considered, confidence, and one-click `approve|reject|escalate`. A reviewer who can't see *why* rubber-stamps.

---

## 7. MLOps: training, promotion, monitoring

- **Training set:** point-in-time-correct as-of joins (`event_time`, not ingest time) from the decision log + feature snapshots — prevents the classic leakage where a price model "predicts" using features that hadn't happened yet.
- **Promotion pipeline:** backtest → shadow (log-only, no customer traffic) → canary 5% → full, each stage with automated kill criteria. Every comparison runs against the **5% holdout arm** (`policy_arm`), which keeps the baseline policy live as the control.
- **Monitors & alert thresholds:**

| Monitor | Signal | Action |
|---|---|---|
| Revenue/margin drift vs. holdout arm | > 2% degradation over 24 h | auto-rollback to previous version + page |
| **Signal-triggered arm vs. holdout** (ADR-007) | > 2% worse over 6 h | auto-disable triggers + page; `pricingctl signals triggers --disable` is the manual kill |
| Triggered-decision sampling error rate | > 5% of reviewed triggers wrong | raise sampling to 100%, pause triggers pending eval |
| Feature staleness PSI | > 0.2 on top-20 features | alert data owner (cold path), degrade decisions |
| Serve p99 latency | > 40 ms | alert before SLA breach |
| Signal precision (sampled human review) | < 0.85 | hold extraction changes, re-run evals |
| LLM cost / token spend | > budget ceiling/day | circuit breaker pauses non-critical extraction |

- **Evals (deployment gate):** each extraction role gets ≥20 cases (functional, schema-compliance, adversarial-injection, edge cases); the pipeline gets end-to-end + failure-recovery + latency evals. No model or prompt ships without a recorded baseline and a meets-or-exceeds score.

---

## 8. Observability (one trace, one story)

Every price request carries a `trace_id` through: API → cache fill → inference → guardrails → **decision log record** → (async) metrics. Every extraction call logs the shared schema:

```json
{ "trace_id": "…", "span_id": "…", "agent_id": "extractor_pricing_page_v3",
  "started_at": "…", "latency_ms": 840, "input_tokens": 1900, "output_tokens": 210,
  "model": "…", "confidence": 0.91, "input_hash": "sha256:…",
  "status": "success|failure|partial|quarantined", "errors": [] }
```

**Root-cause protocol:** start from the wrong price → open its decision record → which feature or model contribution is wrong → trace that feature's `source` and `event_time` back → classify (prompt / context / model / schema / missing-info) → fix + add the failing case to the eval set.

---

## 9. Decision record (the audit primitive)

Every served price writes (async, non-blocking) one `DecisionRecord`:

- `decision_id`, `trace_id`, `sku`, `requested_at`, `served_price`, `model_version`, `model_input` (feature snapshot ref + values), `stale_features[]`, `guardrail_actions[]` (e.g. `clamped_from 129.90 → 119.99 by margin_floor`), `degraded: bool`, `fingerprint`.

This is simultaneously: the audit log, the replay source for "why did we price that?", the training-set builder, and the input to the drift monitors. One artifact, four consumers — that's the payoff of making it a first-class aggregate.

---

## 10. Resolved decisions (step 1 — 2026-11-15)

| Question | Decision | Effect on the design |
|---|---|---|
| Regulatory/parity guardrails | **Full regulatory per market** → [ADR-008](adr/ADR-008-regulatory-guardrails.md) | Guardrails become versioned per-market **policy packs (data, not code)**; unknown pack ⇒ most restrictive fallback; legal review is a blocking gate |
| Control group | **Reserve a 5% holdout arm** | `policy_arm: treatment\|holdout` in every DecisionRecord; all drift monitors compare treatment vs holdout — drift is now measurable |
| LLM authority | **Signal-triggered repricing** (narrow) → [ADR-007](adr/ADR-007-signal-triggered-repricing.md) | Closed trigger set (`competitor_stockout`, `competitor_price_change`), confidence ≥ 0.90 + corroboration, guardrails still apply, per-SKU/per-source rate limits, automatic kill vs holdout |
| Crawl scope | **Broad competitive crawl** | Every source still gets an allowlist entry with ToS/robots review, per-source circuit breaker, and DLQ — "broad" changes volume, not the extraction quality gate |
