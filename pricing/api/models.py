"""Request models — the executable side of docs/contracts/price-api.schema.json."""
from __future__ import annotations

from pydantic import BaseModel, Field


class PriceRequest(BaseModel):
    sku: str = Field(
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
        description="e.g. SKU-4471",
        examples=["SKU-4471"],
    )
    market: str = Field(
        default="EG",
        pattern=r"^[A-Z]{2}$",
        description="Guardrail policy pack (ADR-008); unknown packs degrade stricter, never permissive.",
    )


class BatchPriceRequest(BaseModel):
    items: list[PriceRequest] = Field(min_length=1, max_length=100)


class ReviewRequest(BaseModel):
    choice: str = Field(pattern=r"^(approve|reject|escalate)$")
    note: str | None = Field(default=None, max_length=500)
