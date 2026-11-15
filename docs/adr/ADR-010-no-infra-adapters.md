# ADR-010: Real-where-it-matters adapters (no-Docker environment, flip-on conditions)

## Status
Accepted (environment decision 2026-11-15)

## Context
Step 5b needs real infrastructure. The machine says otherwise:
`sudo` requires a password (no Docker install), disk is **97% full (2.2 GB free)**
(Docker images would fill it), and no JVM exists (no Flink runtime). Network and
pip work, so client libraries are available. Question: what does "real" mean here?

## Decision
Split each component into **the part that is logic** (must be real and tested today)
and **the part that is runtime** (substituted locally, flipped on when infra exists):

| Component | Local (shipped now) | Production (flip-on) | Flip-on condition |
|---|---|---|---|
| Telemetry stream | **~~JSONL event log~~ FLIPPED ON 2026-11-15:** real Kafka broker (Redpanda container, `KAFKA_BROKER=localhost:19092`) — produce→consume→decode proven by `tests/test_flipon_integration.py`; JSONL remains the replay/offline tool | Kafka topic (same) | ✅ **done** — CI without a broker: test skips with its activation reason |
| Window/aggregation (**the Flink job**) | **Pure functions** (`pricing/ingest/windowing.py`) + in-process runner — the exact transforms Flink will call | Flink job wrapping the same functions | JVM/cluster |
| Online feature store | **~~`fakeredis`~~ FLIPPED ON 2026-11-15:** real Redis 7 container (`REDIS_URL=redis://localhost:6379/0`), roundtrip + classification parity proven; `fakeredis` stays the unit-test default | Redis/Feast server | ✅ **done** — CI without a server: test skips with its activation reason |
| Crawl transport | **Real HTTP** through `HttpFetcher` — tested against a live local server (allowlist, redirect refusal, timeout) | same code + proxy/rate limits | ToS expansion per source |
| Scheduling | `pricingctl crawl run` / `python -m pricing.intel.run` (cron-able) | Airflow DAG wrapping the identical entry point | Airflow deployment |
| Extraction | lexicon (tier-3, baseline f1 1.0) | LLM adapter (ADR-006) | API key — **must beat `evals/baseline.json`** |

Rules that make the substitutions honest:
1. **No fake runtime code.** Nothing named `flink_job.py` exists until it runs on Flink; the *logic* is real, tested Python.
2. **Client code is production code.** `RedisFeatureStore`/`KafkaEventSource` use the real client libraries — only the *server* is substituted (fakeredis) or absent (broker-gated skip).
3. **Every substituted row has a flip-on condition** in this table; hitting the condition means deleting a line from this table, not rewriting code.

## Flip-on log

| Date | What | Evidence |
|---|---|---|
| 2026-11-15 | Docker installed (28 GB free after cleanup); `redis:7-alpine` + `redpanda:v24.2.4` containers up; **Redis and Kafka rows flipped** | full suite **165 passed, 0 skipped** with `REDIS_URL` + `KAFKA_BROKER` set; one real bug found by the flip (consumer treated `UNKNOWN_TOPIC` as fatal — now a bounded 50-poll startup window, then a loud error naming the fix) |

Start/stop: `sg docker -c 'docker run …'` (see [docker-compose.yml](../../docker-compose.yml); compose plugin absent, plain `docker run` used) · stop with `sg docker -c 'docker stop pricing-redis pricing-kafka'`.

## Consequences
- **Easier:** 5b delivers a genuinely real stream→feature→price path today; infra arrival is a config event (URL + env var), not a project.
- **Easier:** windowing semantics (tumble windows, rates, latest-wins inventory) are unit-tested *before* any cluster exists — Flink debugging later is integration debugging, not logic debugging.
- **Harder:** local runs cannot prove broker-level behavior (partitions, lag, exactly-once) — those remain until flip-on; the integration tests that will prove them already exist behind `KAFKA_BROKER`.
- **Harder:** disk (2.2 GB free) warns against any image/binary downloads on this box — flag to ops before adding anything.

## Rejected alternatives
- *Installing Docker anyway:* needs a sudo password we don't have, and 2.2 GB free makes it the wrong move even with one.
- *Testcontainers-style integration tests:* require Docker; blocked for the same two reasons.
- *Embedded Kafka (JVM):* no JVM, ~1 GB disk — worse on both axes than a schema-gated JSONL log.
