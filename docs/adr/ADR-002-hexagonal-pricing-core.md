# ADR-002: Hexagonal core for Pricing Decision (ports & adapters)

## Status
Proposed

## Context
Pricing holds the rules that must never be wrong: margin floors, max step size, parity/regulatory clamps, fallback pricing. These rules currently risk being scattered across a serving framework, a feature-store client, and model-server glue — so testing "what happens when inventory is stale?" requires spinning up infrastructure.

## Decision
The **Pricing Decision context is a pure domain core** with dependency inversion:

- **Ports:** `FeaturePort` (get feature snapshot), `ModelPort` (score input vector), `ClockPort`, `DecisionLogPort`, `GuardrailPolicyPort`.
- **Adapters:** Redis/Feast cache client, XGBoost/ONNX scorer, Kafka/OTel emitters — all live outside the core.
- **Domain layer imports no** web framework, ORM, messaging client, or vendor SDK. Guardrails are pure functions over `(features, policy) → (price, guardrail_actions)`.
- Application services coordinate: acquire snapshot → score → apply guardrails → emit decision.

## Consequences
- **Easier:** guardrail unit tests run in microseconds with no infrastructure; "what does the system do when feature X is missing?" is a test case, not an incident drill.
- **Easier:** swapping the model server (XGBoost → NN → remote) or feature store touches one adapter, not the rules.
- **Harder:** indirection tax — a small team must actually respect the dependency rule. Enforced with an import-linter/CI check (`core` may not import `adapters`), otherwise it decays into a layered monolith in name only.
- **Harder:** some cross-cutting concerns (tracing) need explicit wiring at the adapter boundary.

## Alternatives rejected
- *Layered service without ports:* cheaper initially, but guardrails become entangled with I/O and untestable in isolation — unacceptable for financial rules.
- *Full DDD with aggregates everywhere:* the rest of the system is mostly data plumbing (CRUD + streams); rich modeling only where rules deserve it — here, and at `MarketSignal` validation.
