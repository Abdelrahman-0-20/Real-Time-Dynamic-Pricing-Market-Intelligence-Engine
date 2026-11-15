"""The hot path, in one use case (ARCHITECTURE §4):

    snapshot -> staleness classification -> model score -> guardrails -> record

Rules encoded here:
  * The service depends ONLY on ports — swap in-memory adapters for
    Redis/Kafka later without touching this file (ADR-002).
  * The model PROPOSES; guardrails DISPOSE. Every price passes the clamp
    chain, including fallbacks (ADR-007 rail 3, generalized).
  * A price is produced whenever ANY anchor exists (price_last, base_cost,
    or category median). A snapshot with literally zero anchors is a
    contract violation and raises — an honest error beats a fake price.
  * Degraded decisions are flagged, never hidden (staleness contract §4.2).
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime

from pricing.core import decision as decision_records
from pricing.core.features import FeatureDictionary, classify
from pricing.core.guardrails import apply_guardrails
from pricing.core.policy import PolicyPack
from pricing.core.ports import Clock, DecisionLog, FeatureStore, ModelServer

_STAGES = ("snapshot", "inference", "guardrails", "serialize")


class PriceService:
    def __init__(
        self,
        *,
        store: FeatureStore,
        dictionary: FeatureDictionary,
        model: ModelServer,
        log: DecisionLog,
        clock: Clock,
        pack: PolicyPack,
        pack_fallback: bool,
        currency: str = "EGP",
    ) -> None:
        self._store = store
        self._dictionary = dictionary
        self._model = model
        self._log = log
        self._clock = clock
        self._pack = pack
        self._pack_fallback = pack_fallback
        self._currency = currency

    @property
    def pack(self) -> PolicyPack:
        return self._pack

    @property
    def model_version(self) -> str:
        return self._model.model_version

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def dictionary(self) -> FeatureDictionary:
        return self._dictionary

    def price(self, sku: str, trace_id: str | None = None, spans: dict | None = None) -> dict:
        def mark(stage: str, since: int) -> int:
            if spans is not None:
                spans[stage] = time.perf_counter_ns() - since
            return time.perf_counter_ns()

        t = time.perf_counter_ns()
        now = self._clock.now()
        snapshot = self._store.snapshot(f"sku:{sku}", now)
        result = classify(snapshot, self._dictionary, now)
        t = mark("snapshot", t)

        features = result.model_input
        base_cost = features.get("base_cost")
        median = features.get("category_median_price")
        price_last = features.get("price_last")

        degraded = result.degraded or self._pack_fallback
        trigger = "model"
        unclamped: float | None = None
        fallback_used = False

        if degraded or base_cost is None or median is None:
            fallback_used = True
        else:
            try:
                proposed = float(self._model.score(features))
                unclamped = proposed
            except Exception:
                fallback_used = True
                degraded = True
        t = mark("inference", t)

        if fallback_used:
            trigger = "rule_fallback"
            proposed = self._anchor_price(features, price_last, base_cost, median)
        # Band: collapse to the safest computable anchor when inputs are thin.
        band_cost = base_cost if base_cost is not None else (
            price_last / (1 + self._pack.min_margin_pct) if price_last else 0.0
        )
        band_median = median if median is not None else (price_last or band_cost)
        floor, ceiling = self._pack.band(band_cost, band_median)
        last_ref = price_last if price_last and price_last > 0 else proposed
        final_price, actions = apply_guardrails(
            proposed,
            last_price=last_ref,
            floor=floor,
            ceiling=ceiling,
            max_step_pct=self._pack.max_step_pct,
            market=self._pack.market,
        )
        t = mark("guardrails", t)

        record = decision_records.build_record(
            sku=sku,
            requested_at=now,
            served_price=final_price,
            currency=self._currency,
            model_version=self._model.model_version,
            trigger=trigger,
            model_input=features,
            stale_features=result.stale,
            dropped_features=result.dropped,
            guardrail_actions=actions,
            unclamped_price=unclamped,
            degraded=degraded,
            policy_pack={
                "market": self._pack.market,
                "version": self._pack.version,
                "status": self._pack.status,
            },
            trace_id=trace_id,
        )
        self._log.append(record)
        mark("serialize", t)
        return record

    @staticmethod
    def _anchor_price(
        features: dict[str, float],
        price_last: float | None,
        base_cost: float | None,
        median: float | None,
    ) -> float:
        """Rule-based degraded pricing: hold the last price, else cost-plus,
        else a market anchor. Never returns 0 unless every anchor is gone."""
        if price_last is not None and price_last > 0:
            return price_last
        if base_cost is not None and base_cost > 0:
            return base_cost * 1.15  # cost-plus; guardrails still clamp it
        if median is not None and median > 0:
            return median
        raise ValueError(
            "cannot price this SKU: snapshot has no base_cost, price_last, "
            "or category_median_price. Fix: check feature ingestion for this "
            "entity — a pricing decision needs at least one anchor."
        )
