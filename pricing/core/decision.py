"""DecisionRecord construction — the audit primitive (ARCHITECTURE §9).

One record per served price, valid against decision-record.schema.json by
construction: the schema's required fields are exactly this function's
output. Deterministic fingerprints make replay and idempotency possible;
the deterministic holdout arm makes revenue drift measurable.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from pricing.core.guardrails import GuardrailAction

_TRIGGERS = {"model", "signal_trigger", "rule_fallback", "manual"}
_ARMS = {"treatment", "holdout"}
_HOLDOUT_BUCKETS = 100
_HOLDOUT_SIZE = 5  # 5% holdout arm (step-1 decision)


def policy_arm(sku: str) -> str:
    """Deterministic 5% holdout assignment: stable across runs and hosts."""
    digest = hashlib.sha256(sku.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % _HOLDOUT_BUCKETS
    return "holdout" if bucket < _HOLDOUT_SIZE else "treatment"


def fingerprint(sku: str, model_version: str, model_input: dict[str, float]) -> str:
    """hash(request identity, model version, feature snapshot) — replay key."""
    canonical = json.dumps(
        {"sku": sku, "model_version": model_version, "model_input": model_input},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_record(
    *,
    sku: str,
    requested_at: datetime,
    served_price: float,
    currency: str,
    model_version: str,
    trigger: str,
    model_input: dict[str, float],
    stale_features: list[str],
    dropped_features: list[str],
    guardrail_actions: list[GuardrailAction],
    unclamped_price: float | None,
    degraded: bool,
    policy_pack: dict,
    trace_id: str | None = None,
) -> dict:
    if trigger not in _TRIGGERS:
        raise ValueError(
            f"unknown trigger '{trigger}' — allowed: {sorted(_TRIGGERS)}. "
            "Fix: use a closed-enum value; new triggers need a schema change."
        )
    arm = policy_arm(sku)
    record = {
        "decision_id": str(uuid.uuid4()),
        "trace_id": trace_id or str(uuid.uuid4()),
        "sku": sku,
        "requested_at": requested_at.isoformat(),
        "served_price": round(float(served_price), 4),
        "currency": currency,
        "model_version": model_version,
        "trigger": trigger,
        "policy_arm": arm,
        "model_input": {
            "features": {k: (None if v != v else v) for k, v in model_input.items()},  # NaN -> null
        },
        "stale_features": sorted(stale_features),
        "dropped_features": sorted(dropped_features),
        "guardrail_actions": [
            {
                "rule": a.rule,
                "from_price": round(a.from_price, 4),
                "to_price": round(a.to_price, 4),
                "market": a.market,
                "detail": a.detail,
            }
            for a in guardrail_actions
        ],
        "unclamped_price": None if unclamped_price is None else round(float(unclamped_price), 4),
        "degraded": bool(degraded),
        "policy_pack": policy_pack,
        "fingerprint": fingerprint(sku, model_version, model_input),
    }
    _arm = record["policy_arm"]
    assert _arm in _ARMS
    return record
