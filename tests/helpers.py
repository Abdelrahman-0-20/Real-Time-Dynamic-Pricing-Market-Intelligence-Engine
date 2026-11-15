"""Shared test utilities: config loading, schema validation, service factory."""
from __future__ import annotations

import json
import pathlib
from datetime import timedelta
from types import SimpleNamespace

import jsonschema

from pricing.adapters.clocks import FixedClock
from pricing.adapters.memory_decision_log import InMemoryDecisionLog
from pricing.adapters.memory_features import InMemoryFeatureStore
from pricing.adapters.xgboost_model import XGBoostModel
from pricing.app.serve import PriceService
from pricing.core.features import FeatureDictionary
from pricing.core.policy import PolicyPack
from pricing.training.synthetic import FEATURE_ORDER, generate

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
CONTRACTS = ROOT / "docs" / "contracts"

# One shared synthetic sample (500 rows) — fast, deterministic across tests.
_ROWS = generate(n=500, seed=11)


def load_schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text())


def validate_record(record: dict) -> None:
    """A served record must satisfy the published decision contract."""
    jsonschema.validate(instance=record, schema=load_schema("decision-record.schema.json"))


def load_dictionary() -> FeatureDictionary:
    return FeatureDictionary.from_dict(
        json.loads((CONFIG / "features" / "fv-2026.11.json").read_text())
    )


def load_eg_pack() -> PolicyPack:
    return PolicyPack.from_dict(json.loads((CONFIG / "guardrails" / "EG.json").read_text()))


def make_service(
    artifacts: pathlib.Path,
    *,
    clock: FixedClock | None = None,
    pack: PolicyPack | None = None,
    pack_fallback: bool = False,
    model: object | None = None,
    model_version: str = "xgb-test-v0",
) -> SimpleNamespace:
    clock = clock or FixedClock()
    dictionary = load_dictionary()
    store = InMemoryFeatureStore()
    log = InMemoryDecisionLog()
    if model is None:
        model = XGBoostModel(
            artifacts / "xgb-baseline.json",
            artifacts / "feature_order.json",
            model_version,
        )
    service = PriceService(
        store=store,
        dictionary=dictionary,
        model=model,
        log=log,
        clock=clock,
        pack=pack or load_eg_pack(),
        pack_fallback=pack_fallback,
        currency="EGP",
    )
    return SimpleNamespace(
        service=service, store=store, log=log, clock=clock,
        dictionary=dictionary, pack=service.pack,
    )


def seed_sku(
    ctx: SimpleNamespace,
    sku: str,
    row: int = 0,
    age_ms: int = 0,
    overrides: dict | None = None,
) -> str:
    """Write a full feature snapshot for `sku` from synthetic row `row`.

    overrides: name -> value | None (None = do not write that feature)
               | (value, age_ms) tuple for per-feature age/value control.
    """
    X, _, _ = _ROWS
    now = ctx.clock.now()
    base_time = now - timedelta(milliseconds=age_ms)
    overrides = overrides or {}
    for j, name in enumerate(FEATURE_ORDER):
        if name in overrides:
            spec = overrides[name]
            if spec is None:
                continue
            if isinstance(spec, tuple):
                value, feature_age = spec
                event_time = now - timedelta(milliseconds=feature_age)
            else:
                value, event_time = spec, base_time
        else:
            value, event_time = float(X[row, j]), base_time
        ctx.store.put(
            f"sku:{sku}", name, value, event_time,
            ttl_ms=ctx.dictionary.specs[name].ttl_ms,
        )
    return f"sku:{sku}"


class StubModel:
    """ModelServer test double: fixed output or a hard failure."""

    def __init__(self, propose: float = 1000.0, fail: bool = False) -> None:
        self.model_version = "stub-v1"
        self._propose = propose
        self._fail = fail

    def score(self, features: dict[str, float]) -> float:
        if self._fail:
            raise RuntimeError("model server down")
        return self._propose
