# Real-Time Dynamic Pricing & Market Intelligence Engine

**Turns live sales telemetry + scraped market news into audited price decisions in < 50 ms** — with hard guardrails, an LLM extraction layer, and test gates that make every claim on this page verifiable.

```
![tests](https://img.shields.io/badge/tests-189_passed,_5_skipped-blue)   (measured: latency gate PASS · eval f1 1.00 · 10 ADRs)
```

---

## The 60-second story

One SKU, one hour:

```text
POS says "5 earbuds sold this hour"            ─┐  streaming
Warehouse says "100 units left"                ─┘  telemetry
Crawler reads CompAlpha: "SKU-7780 OUT OF STOCK"─┐  web scraping
News: "retailers raised prices by 8%"          ─┘
LLM turns both into structured signals: "competitor_stockout" (conf 0.92)
                ▼
   ┌────────── FEATURE STORE — where everything joins ──────────┐
   │ demand=8/hr · inventory=100 · competitor_stockout=0.92     │
   └────────────────────────┬───────────────────────────────────┘
                            ▼
   XGBoost proposes 129.90 → guardrails clamp → 119.99 EGP
                            ▼
   served in <50 ms · DecisionRecord logged (why, with what, by which model)
   their stockout confirmed by 2 trusted sources → may reprice instantly (rule-guarded)
```

**In one sentence:** *scrape the market, sense demand, compute the best price, never break a rule, and always be able to explain why.*

## Quickstart

```bash
git clone <repo> && cd <repo>
python3 -m pip install jsonschema pytest xgboost scikit-learn numpy redis fakeredis confluent-kafka import-linter fastapi uvicorn wsproto

python3 -m pricing.training.train      # baseline model (synthetic market, known ground truth)
python3 -m pytest -q                   # contract gates + end-to-end suite
python3 benchmarks/latency.py          # p99 latency gate (50 ms budget)
./pricingctl status                   # operator CLI (20 ms startup)
./pricingctl price explain --sku SKU-4471   # the WHY behind a price
python3 -m pricing.intel.run           # crawl seeds -> signals -> quarantine report
```

> This machine only provides `python3` (there is no bare interpreter alias) — every command above is written to run as-is.

Optional real infra (Docker): `docker compose up -d` + `export REDIS_URL=redis://localhost:6379/0 KAFKA_BROKER=localhost:19092` → 3 gated integration tests activate against real Redis + Kafka (without them the suite runs with those 3 skipped: baseline `python3 -m pytest -q` → 189 passed, 5 skipped).

### HTTP Pricing API

Auth is **fail-closed**: the API refuses to start (a `RuntimeError`) unless you give it `PRICING_API_KEY`, or explicitly opt into `PRICING_API_DEV=1`. Both options, exactly as in [docs/API.md](docs/API.md):

```bash
export PRICING_API_KEY=$(openssl rand -hex 32)   # real use: random key, sent as X-API-Key
uvicorn pricing.api.main:create_app --factory --ws wsproto --port 8000
# local unauthenticated experiment ONLY:
PRICING_API_DEV=1 uvicorn pricing.api.main:create_app --factory --ws wsproto
```

The fastapi app is served by `uvicorn` over a WebSocket handled by `wsproto` (the system-packaged `websockets` is too old for uvicorn's default).

## Troubleshooting

- **`docker compose up -d` fails** (`docker: 'compose' is not a docker command`, and there is no `docker-compose` binary either): this machine has neither the compose plugin nor the legacy standalone installed. Install the plugin: `sudo apt install docker-compose-plugin`, then retry `docker compose up -d`.
- **Integration tests stay skipped**: the flip-on integration tests require real infra and their env vars — start the stack (`docker compose up -d`), then `export REDIS_URL=redis://localhost:6379/0 KAFKA_BROKER=localhost:19092` before `python3 -m pytest -q`. Without both, those 3 tests skip (baseline 189 passed, 5 skipped).

## Measured results (and their honest scope)

| Metric | Value | Scope — what it does *not* claim |
|---|---|---|
| Serve latency | **p99 0.8 ms** (budget 50 ms) | in-process code path on dev hardware; production network hops not yet measured |
| Extraction eval | **f1 1.00** (27 labeled cases, recorded baseline) | lexicon extractor on its own corpus; LLM must beat the *re-baselined* harder corpus to ship |
| Model quality | **0.34% mean regret** vs true oracle | trained on synthetic demand with known elasticity — that's *why* regret is measurable |
| Test suite | **189 passed, 5 skipped** (194 total; the 5 are env-gated without brokers — a 0-skipped run needs brokers/real infra, so that figure is broker-dependent and not claimed here) | local stack, synthetic data |
| Operator CLI | **20 ms** cold start | file-read-only commands |

## Architecture

```text
  HOT PATH (ms)                          COLD PATH (minutes–hours)
  POS/WMS → Kafka → windowing            crawler → parse → INJECTION gate
      → feature store ◀────── join ──────→ lexicon/LLM → SCHEMA gate → signals
      → XGBoost → GUARDRAILS → DecisionRecord → feature writer
                ▲
   MLOps: eval gate · import-linter · latency gate · decision replay · holdout arm
```

Rules the design enforces (each with a test):

- **Model proposes, guardrails dispose** — every price (model output, signal trigger, or fallback) passes the same clamp chain: margin floor/ceiling, max step, per-market legal packs.
- **The LLM is a feature, never a dependency** — extraction dying changes no price; signal values decay (`confidence × e^(-t/τ)`).
- **Staleness is never hidden** — fresh / flagged-stale / dropped are explicit in every `DecisionRecord`; critical features failing degrade to rule-based pricing, not silent imputation.
- **Web content is hostile** — instruction-like text quarantines the whole document *before* extraction; closed signal enums mean page text can't invent signal types.
- **ToS-reviewed sources only** — unreviewed sources are *refused before any fetch*, with the reason recorded.
- **Gates, not vibes** — 5 JSON Schemas as CI gates, hexagonal import rules, extraction eval vs recorded baseline, latency budget with stage-level blame.

## Design decisions

Every non-obvious choice is an ADR with trade-offs and rejected alternatives — [docs/adr/](docs/adr/):

| # | Decision |
|---|---|
| 001 | Hot/cold path split — the feature store is the only join |
| 002 | Hexagonal pricing core — guardrails are pure, framework-free rules |
| 003 | LLM output is a *feature*, never a blocking dependency *(amended by 007)* |
| 004 | Local feature cache — freshness over consistency |
| 005 | XGBoost primary, neural net in shadow, auto-rollback promotion |
| 006 | Extraction topology: fan-out across documents, not across agents |
| 007 | Signal-triggered repricing — narrow, rail-guarded (corroboration, rate limits, auto-kill) |
| 008 | Regulatory guardrails as per-market policy packs (data, not code) |
| 009 | Cold-path ports with tested lightweight defaults (Docling/VectorDB/LLM swaps) |
| 010 | Real-where-it-matters adapters + explicit flip-on conditions |

## Repo map

| Path | What |
|---|---|
| `pricing/core` | domain: guardrails, freshness, policy packs, decision records — **zero vendor imports (CI-enforced)** |
| `pricing/app` | the hot path use case (snapshot → score → clamp → record) |
| `pricing/intel` | cold path: crawl pipeline + injection/schema/contradiction gates |
| `pricing/ingest` | windowing = the portable Flink job logic |
| `pricing/adapters` | Redis, Kafka, XGBoost, JSONL, HTTP — the only vendor code |
| `pricing/ctl` | `pricingctl` operator CLI (exit codes are an API) |
| `pricing/evals` | extraction eval harness vs `evals/baseline.json` |
| `docs/contracts` | 5 JSON Schemas — the published boundaries between contexts |
| `docs/` | architecture, ADRs, feature manifest, latency protocol, CLI spec |
| `tests/` | 194 tests (189 passed, 5 skipped) incl. adversarial (prompt injection, guardrail bypass, mislabeled evals) |

## Status & roadmap — honest

**Real today:** full loop on a local stack — telemetry + crawl → features → model → guardrailed, audited price; real Redis + Kafka containers; operator CLI; eval gate in CI.

**Shipped since:** **HTTP Pricing API** ([docs/API.md](docs/API.md) — fail-closed auth, `{error, fix}` envelopes, p99 gate 34/42/47 ms over 3 runs) · **LLM extractor infrastructure** (ADR-006 fallback chain + bounded-generation adapter; live eval awaiting a quiet box, retry checklist in [ADR-011](docs/adr/ADR-011-local-ollama-backend.md) — the lexicon ships meanwhile, per the gate) · **Tauri operator console** ([console/](console/) — thin shell: capability-scoped HTTP to localhost only, no shell/fs/opener surface, status · price+SHAP explain · approve/reject/escalate · signals · decisions).

**Not yet (in order):**
1. **LLM live eval** — run when the box is quiet; must beat the lexicon bar (f1 0.769) to become primary
2. **Operator console** (Tauri) — IPC contract first, calls the HTTP API
3. **Production traffic** — shadow/canary → 5% holdout → drift monitors need real requests
4. Guardrail packs carry `status: placeholder` — **legal review is a blocking gate** before `approved`

## License

Choose one before publishing (MIT or Apache-2.0 recommended for this kind of project).
