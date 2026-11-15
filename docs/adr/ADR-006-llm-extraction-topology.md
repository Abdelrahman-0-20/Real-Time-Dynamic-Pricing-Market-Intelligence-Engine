# ADR-006: Extraction topology — parallel fan-out across documents, single pipeline per document, evaluator only on low confidence

## Status
Proposed

## Context
The GenAI layer must turn heterogeneous unstructured documents into validated `MarketSignal` JSON at crawl volume. The default temptation is a multi-agent hierarchy: triage agent → specialist agents (pricing, promo, stockout) → arbitration agent → writer. That design costs latency, tokens, and debuggability for a task that is fundamentally *extract-and-validate*, not *deliberate*.

## Decision
**Parallelism lives at the document level, not the reasoning level.**

```
                         ┌→ sanitize → extract → schema-gate → write ┐
DocumentIngested → fan-out├→ sanitize → extract → schema-gate → write ├→ metrics/quarantine
 (≤7 concurrent/docs)     └→ sanitize → extract → schema-gate → write ┘
```

Per document (a short sequential chain, ≤4 steps):

1. **Sanitize:** external content is placed in delimited data blocks; never concatenated into the system prompt. Strip/flag instruction-like content (imperative verbs + tool names).
2. **Extract:** single-purpose LLM call with a **JSON-Schema-constrained** output (structured output / tool-call mode). Returns `signals[]` + `confidence`. The extractor has **no tools, no DB access, no side effects**.
3. **Gate:** schema validation + confidence ≥ 0.7 + contradiction check against existing signals (source-trust × recency weighting). Pass → writer; fail → **retry once with narrowed prompt** → rule-based fallback extractor (lexicon/regex) → `SignalQuarantined`. **Hard exit after 3 iterations** — never an unbounded evaluator loop.
4. **Write:** the only component with feature-store write access; accepts `MarketSignal` only.

**Evaluator-optimizer is used selectively**, only for documents that failed the gate and are above a value threshold (top-competitor SKUs), max 3 iterations, plateau → escalate to advisory queue.

**No arbitration agent for contradictions:** conflicting signals are resolved deterministically (weights + threshold); unresolved conflicts write nothing and flag advisory. A deliberating agent adds cost and nondeterminism to a decision a formula makes fine.

## Consequences
- **Easier:** every document's failure is isolated (one bad page can't stall the queue); stateless workers scale horizontally; tracing is one span per document stage.
- **Easier:** injection defense is structural — the writer only ever sees schema-valid JSON, and injected instructions can't produce valid JSON + valid tool state because there are no tools.
- **Easier:** evals are simple — 20+ labeled documents per extractor version, scored on schema compliance + signal precision.
- **Harder:** cross-document synthesis (e.g., "competitor raised prices across 50 SKUs") requires a separate aggregation step over signals, not an agent that sees all docs.
- **Harder:** fan-out width capped at ~7 concurrent LLM calls per worker for synthesis/queue reasons — requires a worker pool, not a free-for-all.

## Alternatives rejected
- *Orchestrator + specialist subagents per document:* decomposition overhead per page, orchestrator becomes a bottleneck and an unbounded prompt, contradiction detection between specialists is harder than the direct gate.
- *Mesh/consensus among extractors:* highest complexity, worst debuggability — no production justification for extraction.
