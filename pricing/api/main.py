"""FastAPI application factory.

Run it:
    export PRICING_API_KEY=$(openssl rand -hex 32)
    uvicorn pricing.api.main:create_app --factory --port 8000       # real auth
    PRICING_API_DEV=1 uvicorn pricing.api.main:create_app --factory  # local experiments

Auth is FAIL-CLOSED: no key and no dev flag → startup raises with the fix.
Contract: docs/contracts/price-api.schema.json (+ decision-record.schema.json
for served decision bodies). Errors: {"error": ..., "fix": ...}.
"""
from __future__ import annotations

import os
import time

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from pricing.api.models import BatchPriceRequest, PriceRequest
from pricing.api.wiring import PricingStack

VERSION = "0.1.0"


class ApiError(Exception):
    def __init__(self, status: int, error: str, fix: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.fix = fix


def create_app(
    *,
    market: str | None = None,
    currency: str = "EGP",
    api_key: str | None = None,
    dev: bool | None = None,
    config_dir=None,
    artifacts_dir=None,
    var_dir=None,
) -> FastAPI:
    dev_mode = (os.environ.get("PRICING_API_DEV") == "1") if dev is None else dev
    key = api_key if api_key is not None else os.environ.get("PRICING_API_KEY")

    if not dev_mode and not key:
        raise RuntimeError(
            "refusing to start: PRICING_API_KEY is not set. "
            "Fix: export PRICING_API_KEY=$(openssl rand -hex 32)  — or set "
            "PRICING_API_DEV=1 for a local, unauthenticated experiment "
            "(never for real traffic)."
        )

    stack = PricingStack(
        market=market or os.environ.get("PRICING_API_MARKET", "EG"),
        currency=currency,
        config_dir=config_dir, artifacts_dir=artifacts_dir, var_dir=var_dir,
    )

    app = FastAPI(
        title="Pricing API",
        version=VERSION,
        description="Serve guardrailed, auditable prices. Errors: {error, fix}.",
    )
    app.state.stack = stack
    app.state.dev_mode = dev_mode
    app.state.api_key = key

    # ---- error envelopes -------------------------------------------------

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            {"error": exc.error, "fix": exc.fix}, status_code=exc.status
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"] if part != "body") or "body"
        return JSONResponse(
            {
                "error": f"invalid field '{location}': {first['msg']}",
                "fix": "see docs/contracts/price-api.schema.json for the accepted shape",
            },
            status_code=422,
        )

    # ---- auth (fail-closed by construction) ------------------------------

    async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        if app.state.dev_mode:
            return
        if x_api_key != app.state.api_key:
            raise ApiError(
                401,
                "missing or invalid X-API-Key header",
                "send X-API-Key: $PRICING_API_KEY (generate one with "
                "`openssl rand -hex 32`); unauthenticated local use needs PRICING_API_DEV=1",
            )

    auth = [Depends(require_api_key)]

    # ---- routes ----------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        payload = dict(stack.health)
        payload["auth"] = "dev-open" if app.state.dev_mode else "api_key"
        return payload

    @app.post("/v1/price", dependencies=auth)
    def price(request: PriceRequest, response: Response) -> dict:
        started = time.perf_counter_ns()
        stack.seed_once(request.sku)
        service, _ = stack.service_for(request.market)
        record = service.price(request.sku)
        serve_ms = (time.perf_counter_ns() - started) / 1e6
        response.headers["X-Serve-Ms"] = f"{serve_ms:.3f}"
        return record

    @app.post("/v1/prices", dependencies=auth)
    def prices(request: BatchPriceRequest, response: Response) -> list[dict]:
        started = time.perf_counter_ns()
        decisions: list[dict] = []
        for item in request.items:
            stack.seed_once(item.sku)
            service, _ = stack.service_for(item.market)
            decisions.append(service.price(item.sku))
        serve_ms = (time.perf_counter_ns() - started) / 1e6
        response.headers["X-Serve-Ms"] = f"{serve_ms:.3f}"
        return decisions

    @app.get("/v1/decisions/{trace_id}", dependencies=auth)
    def decision(trace_id: str) -> dict:
        record = stack.log.find_trace(trace_id)
        if record is None:
            raise ApiError(
                404,
                f"no decision with trace_id '{trace_id}'",
                "check the trace_id — every POST /v1/price response includes "
                "one; var/decisions.jsonl may also have been cleared",
            )
        return record

    return app
