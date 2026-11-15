"""HTTP Pricing API — delivery layer + composition root (the app's public face).

Discipline mirrored from the desktop/CLI rules:
  * validation at the PRIVILEGE boundary: pydantic models gate every request,
    published as docs/contracts/price-api.schema.json (responses are the
    DecisionRecord contract itself)
  * auth fail-closed: refuses to start without PRICING_API_KEY unless
    PRICING_API_DEV=1 (never in real deployments)
  * every error is an envelope {error, fix} — same voice as pricingctl
  * routes are sync `def` so FastAPI runs them in its threadpool; the serve
    path is CPU-bound and unchanged from the measured hot path
"""
