# ADR-008: Regulatory guardrails as per-market policy packs (data, not code)

## Status
Accepted (business decision 2026-11-15)

## Context
Decision from step 1: *"full regulatory per market."* Guardrails now encode legal constraints (price-parity rules, statutory pricing restrictions, market-specific caps) — the rules that must never be wrong, across N markets, with legal input that arrives slower than engineering.

## Decision
- **Policy packs are versioned data**, loaded at startup, not hardcoded: one pack per market code (`EG`, `SA`, `DE`, …), each declaring the ordered guardrail chain (floor/ceiling/step/parity/regulatory clamps) with effective dates.
- **Fail-safe direction:** an unknown or failed-to-load market pack resolves to the **most restrictive** pack (never permissive), and the affected decisions are flagged `degraded` with an alert. Missing configuration must never widen the allowed price band.
- **`regulatory_clamp` / `market_specific_rule`** are closed members of the `guardrail_actions.rule` enum in the DecisionRecord, carrying `market` + statute reference in `detail` for audit.
- **Every pack change is a blocking HITL gate** (ARCHITECTURE §6) with `--dry-run` preview: `pricingctl config set guardrails.EG --file pack.json --dry-run` shows which SKUs' bands change before applying.
- **Legal review is a deployment gate** for pack changes, recorded as the approval on the gate — engineering cannot ship a pack unilaterally.

## Consequences
- **Easier:** new market = new pack file, no redeploy of the pricing core; regression tests run against every pack (fixture: each market × boundary SKUs).
- **Easier:** audit answers "which law was in effect when we priced this?" via pack version in the decision record.
- **Harder:** pack version skew between training (historical decisions) and serving — recorded per decision, so replay uses the pack that was actually applied.
- **Harder:** "most restrictive fallback" can clamp legitimate prices in a new market if the pack is missing — visible via `degraded` flag rather than silent, accepted as the safe direction.

## Rejected alternatives
- *Guardrails in code per market:* legal change = code change = release train; unreviewable by non-engineers.
- *Permissive default on missing config:* the exact failure mode that turns a config bug into a regulatory incident.
