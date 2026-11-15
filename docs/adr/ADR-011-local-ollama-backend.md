# ADR-011: LLM backend = local Ollama (keyless, cold-path, eval-gated)

## Status
Proposed (backend decision 2026-11-15: user chose local over cloud API)

## Context
Step 6 needs a real LLM for the ADR-006 extraction pipeline. Options were:
cloud API key (quality, cost, egress), existing endpoint (none available), or
local Ollama (no key, no cost, no data egress). The project is **local-first
by intent**; online scaling budget is $10–15/month, which a per-token API
would nibble at forever. Extraction runs on the cold path (ADR-001), so
CPU inference latency is irrelevant — minutes are free.

## Decision
- **Ollama in user space** (no sudo, no system service) serving the
  OpenAI-compatible API at `http://localhost:11434/v1`.
- **Model: `qwen2.5:1.5b` (~1 GB)** — the lightest model with a real chance
  at structured extraction; the Ollama runtime itself is a ~1.1 GB one-time
  bundle (ships GPU libs we don't use on this CPU box).
- **Adapter:** `OpenAICompatExtractor` (pricing/intel/adapters) — keyless,
  `format: json`, temperature 0, closed signal-type enum enforced in code,
  page content passed as delimited DATA with an explicit "not instructions"
  system stance (defense layers 1–4 of ADR-006).
- **Gate decides, not hope:** baselines are per-extractor on the same
  41-case corpus (v2, incl. 14 hard paraphrases). Lexicon bar:
  **f1 0.769, precision 1.000**. The LLM ships as primary only if it beats
  that bar; otherwise lexicon stays primary and the LLM is not wired in.
- **Fallback chain is implemented, not aspirational:** LLM → lexicon
  (tier-3) → breaker + `extract_error` (ADR-006). CLI/eval select via
  `--extractor llm` or `PRICING_EXTRACTOR=llm`.

## Consequences
- **Easier:** zero credentials, zero egress, zero per-token cost; swap
  backend later = URL/model env vars (any OpenAI-compatible server works).
- **Easier:** a failed model degrades to the tested lexicon automatically —
  pricing never waits on or dies with the LLM (ADR-003 intact).
- **Harder:** 1.5b recall on paraphrases may miss the bar — the gate will
  say so with per-case evidence; upgrade path is one `ollama pull qwen2.5:3b`.
- **Harder:** quality varies across model versions; the recorded baseline is
  keyed by model name (`ollama-qwen2.5:1.5b`), so a model swap requires
  re-recording ITS baseline before it can ship.

## Live-model investigation log (2026-11-15) — environment-blocked, infra proven

Proven: Ollama v0.40.16 user-space install; `qwen2.5:1.5b` pulled (**986 MB**);
server healthy (`/api/version` 151 µs); **the model produces the exact signal
schema** via inline curl in ~1 s with the full system prompt; adapter contract
tests (fake OpenAI server) all green; ADR-006 fallback chain exercised.

Blocked: the LIVE eval (`OLLAMA_LIVE=1 pytest`, `evals.run --extractor llm`).
Observed: generation requests hang to exactly the client timeout (30/45/60/90/180 s
variants all reproduced) while interactive inline `-d` curls intermittently succeed
in 0.4–1.4 s; `urllib`, `httpx`, and `curl --data-binary @-` from bash all hang
against the same endpoint that answers curl `-d`. GIN logs show requests arriving
then held to timeout; `llama-server` burned **328–432% CPU for minutes on 30-token
generations** (starved threads). Root context: **swap was 91% full from the desktop
(Firefox, Cinnamon, DSH harness) before Ollama ever ran** — 8.5 GB RAM box at its
limit; every Python process pays page-in storms that inflate all measurements.

Conclusion: transport anomaly is **suspected system starvation, not the payload**
(byte-identical bodies succeed/fail depending on box load) — unresolved on purpose:
the adapter's bounded-timeout + loud-error + lexicon-fallback chain (ADR-006) held
through every failure, and CI never requires a live model.

**Retry checklist (quiet machine):**
1. Free RAM/swap (close Firefox or `sudo swapoff -a` after closing), verify `free -m` shows >3 GB available and load <5.
2. `OLLAMA_KEEP_ALIVE=30m OLLAMA_LOAD_TIMEOUT=15m ollama serve` (file-logged, not piped).
3. Warm once: inline curl `max_tokens` call → expect ~1 s.
4. `OLLAMA_LIVE=1 pytest tests/test_llm_live.py -q` → then `python -m pricing.evals.run --extractor llm` — must beat the lexicon bar (f1 0.769) before `--update-baseline`.

## Rejected alternatives
- *Cloud API:* contradicts local-first intent, recurring cost, egress of
  competitor content — revisit only if the local model fails the gate even
  at 3b.
- *Largest local model first:* disk/RAM headroom exists, but pulling 7B+
  before proving the pipeline on 1.5b optimizes the wrong thing.
