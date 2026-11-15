# ADR-009: Cold-path ports with lightweight local defaults (Docling/embeddings/LLM swapped later)

## Status
Proposed

## Context
The cold path needs four technologies that the step-4 environment cannot honestly host: Docling (pulls torch, ~GBs, no GPU), a Vector DB (Qdrant/Chroma need Docker — absent), a real LLM endpoint (no credentials), and Airflow (needs the same infra). The failure mode to avoid: either blocking step 4 on infrastructure, or hard-coding substitutes into the pipeline so the real technologies can never land.

## Decision
Build the cold path **behind ports, exactly as the hot path was (ADR-002)**, with lightweight defaults that are *tested* rather than *temporary*:

| Port | Step-4 adapter (tested) | Production adapter (pending) |
|---|---|---|
| `Fetcher` | `FixtureFetcher` (seed HTML) + `HttpFetcher` (stdlib urllib, timeout, host allowlist) | same `HttpFetcher` + proxy/rate-limit config |
| `Parser` | stdlib `HTMLParser`: text + `data-sku` entity chunks | **Docling** adapter (same port, same tests) |
| `DocumentStore` | TF-IDF + cosine (sklearn, already a dep) | **Vector DB** with real embeddings |
| `SignalExtractor` | **lexicon extractor** — deterministic rules over the closed signal vocabulary | **LLM extractor** (ADR-006 pipeline) behind the same port |
| scheduling | `python -m pricing.intel.run` (cron-able) | Airflow DAG wrapping the identical entry point |

Key consequences of choosing the lexicon extractor as the default rather than a placeholder:

- It is simultaneously **tier-3 of the ADR-006 fallback chain** (rule-based degraded extraction) — so the fallback is production code with production tests, not dead code written on the day the LLM breaks.
- Its outputs are structurally injection-safe: a closed pattern vocabulary cannot be extended by text on a page (ADR-006's schema gate remains as the second gate).
- `jsonschema` is permitted inside the gate (pure validation library, no IO); numeric/IO/framework vendors remain forbidden in the non-adapter cold-path code, same rule as the hot path.

## Consequences
- **Easier:** step 4 ships a fully tested end-to-end cold path today; each production swap is one adapter file with the existing test suite as the acceptance criteria.
- **Easier:** extraction quality regressions are measurable now (lexicon precision/recall fixtures) before an LLM introduces nondeterminism.
- **Harder:** retrieval quality (TF-IDF) and extraction recall (patterns) are genuinely worse than embeddings/LLM — category-level and paraphrased signals will be missed. Accepted for the slice; the LLM adapter (ADR-006) is the recall fix.
- **Harder:** two adapter implementations per port eventually (local + production) — the cost of not blocking on infra, paid deliberately.

## Rejected alternatives
- *Installing Docling/torch on an 8 GB dev box:* multi-GB install, CPU-only inference, no test value proportional to cost.
- *Building the pipeline directly against Docling/Chroma/LLM APIs:* untestable here; the pipeline would be written against infrastructure that doesn't exist yet.
- *Skipping the cold path until infra exists:* leaves the ADR-001 join (feature store) and ADR-007 rails untested end-to-end.
