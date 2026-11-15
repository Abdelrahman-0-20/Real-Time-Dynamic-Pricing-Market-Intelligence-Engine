# Pricing HTTP API

The product's public face: guardrailed, auditable prices over HTTP — the same
serve path as `pricingctl`, measured with the network included.

## Run

```bash
export PRICING_API_KEY=$(openssl rand -hex 32)
uvicorn pricing.api.main:create_app --factory --ws wsproto --port 8000
# local unauthenticated experiment ONLY:
PRICING_API_DEV=1 uvicorn pricing.api.main:create_app --factory --ws wsproto
```

> `--ws wsproto` is required on this box: the system-packaged `websockets` is
> too old for uvicorn's default (ImportError: ServerProtocol).

**Auth is fail-closed:** no `PRICING_API_KEY` and no `PRICING_API_DEV=1` → the
server refuses to start and prints the fix. `dev-open` is reported by `/health`
so an unauthenticated deployment is never silent.

## Endpoints

| Method & path | Auth | Body / returns |
|---|---|---|
| `GET /health` | none | `{status, model_version, dictionary_version, pack, auth}` (contract: `price-api.schema.json`) |
| `POST /v1/price` | `X-API-Key` | `{"sku": "SKU-4471", "market": "EG"}` → **the DecisionRecord** (contract: `decision-record.schema.json`) + `X-Serve-Ms` header |
| `POST /v1/price/explain` | `X-API-Key` | same body → `{decision, contributions[]}` — SHAP contributions, the HITL "why" (contract: `ExplainResponse`) |
| `POST /v1/prices` | `X-API-Key` | `{"items": [...≤100...]}` → array of DecisionRecords |
| `GET /v1/decisions?limit=N` | `X-API-Key` | most recent N (1–100) DecisionRecords |
| `GET /v1/decisions/{trace_id}` | `X-API-Key` | that DecisionRecord (404 + fix envelope if unknown) |
| `POST /v1/decisions/{trace_id}/review` | `X-API-Key` | `{"choice": "approve\|reject\|escalate", "note"?}` → review entry persisted to `var/reviews.jsonl` (contract: `ReviewRequest/Response`) |
| `GET /v1/signals` | `X-API-Key` | last crawl's signals + quarantines + stats (404 + crawl fix if never run) |

Interactive docs: `http://localhost:8000/docs` (FastAPI/OpenAPI).

**Consumers:** `pricingctl` (CLI), CI gates, and the **Tauri operator console** ([console/](../console/)) — a thin shell whose only capability is HTTP to `127.0.0.1:8000`.

### Examples

```bash
curl -s localhost:8000/health | jq
curl -s -X POST localhost:8000/v1/price \
  -H "X-API-Key: $PRICING_API_KEY" -H 'Content-Type: application/json' \
  -d '{"sku": "SKU-4471"}' | jq '{price: .served_price, degraded, trace: .trace_id, clamps: .guardrail_actions}'
```

## Error contract

Every error is an envelope, same voice as `pricingctl`:

```json
{ "error": "invalid field 'sku': String should match pattern '...'",
   "fix": "see docs/contracts/price-api.schema.json for the accepted shape" }
```

| Status | Meaning |
|---|---|
| 200 | price served — check `degraded` inside the record (a degraded price is still a price) |
| 401 | missing/wrong `X-API-Key` (fix names the header + generation command) |
| 404 | unknown `trace_id` |
| 422 | body failed validation (field named) |
| 500 | unexpected — envelope still `{error, fix}` |

## Measured latency (2026-11-15, this dev desktop, n=5000, conc=8, 2 uvicorn workers)

| Metric | Value | Note |
|---|---|---|
| e2e p50 | 11–13 ms | client wall time incl. HTTP + JSON + auth |
| e2e p99 | **34 / 42 / 47 ms (3 runs, gate PASS ≤ 50)** | before fixes: 49 / 57 / 322 — the gate caught it |
| serve p99 (`X-Serve-Ms`) | 12–16 ms | handler only: seed → model → guardrails → log |

**Honest scope:** a *shared desktop* (Firefox + harness + swap-limited) — variance
is real; the tiered protocol lives in [docs/latency-harness.md](latency-harness.md)
(local e2e ≤ 50 hard when quiet, CI ≤ 150). The 50 ms customer SLA gets its final
validation on the phase-2 VPS, not this laptop.

**Tuning levers used (measured wins):** persistent JSONL append handle
(open/close per request showed up in tails), `--workers 2` (GIL spread),
`seed_once` (warm stores don't rewrite themselves), uvloop + httptools,
`--ws wsproto` (see above).

## Contract

- Requests / health / error envelope: [docs/contracts/price-api.schema.json](contracts/price-api.schema.json)
- Served decision bodies: [docs/contracts/decision-record.schema.json](contracts/decision-record.schema.json)
- Gate: [tests/test_api.py](../tests/test_api.py) (auth, envelopes, contracts) + `benchmarks/http_latency.py` (p99 gate)
