"""API wiring: one shared world (store, model, log) + per-market services.

The feature store and decision log are shared across markets — only the
guardrail pack differs — so a request for market ZZ doesn't fork reality.
Service instances are cached per market (resolved lazily, thread-safe).
"""
from __future__ import annotations

import pathlib
import threading
from types import SimpleNamespace

from pricing.adapters.clocks import SystemClock
from pricing.adapters.jsonl_decision_log import JsonlDecisionLog
from pricing.adapters.memory_features import InMemoryFeatureStore
from pricing.adapters.xgboost_model import XGBoostModel
from pricing.app.serve import PriceService
from pricing.compose import load_dictionary, load_packs
from pricing.core.policy import resolve_pack

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class PricingStack:
    def __init__(self, *, market: str = "EG", currency: str = "EGP",
                 config_dir=None, artifacts_dir=None, var_dir=None) -> None:
        config = pathlib.Path(config_dir) if config_dir else REPO_ROOT / "config"
        artifacts = pathlib.Path(artifacts_dir) if artifacts_dir else REPO_ROOT / "artifacts"
        var = pathlib.Path(var_dir) if var_dir else REPO_ROOT / "var"

        self.packs = load_packs(config / "guardrails")
        self.dictionary = load_dictionary(config / "features")
        self.var = var
        # XGBoostModel raises FileNotFoundError WITH the fix if artifacts missing
        # → uvicorn startup fails loudly instead of 500ing every request.
        self.model = XGBoostModel(
            artifacts / "xgb-baseline.json", artifacts / "feature_order.json",
            "xgb-baseline-v1",
        )
        self.store = InMemoryFeatureStore()
        self.log = JsonlDecisionLog(var / "decisions.jsonl")
        self.reviews = JsonlDecisionLog(var / "reviews.jsonl")
        self.clock = SystemClock()
        self.currency = currency
        self.default_market = market
        self._services: dict[str, tuple[PriceService, bool]] = {}
        self._seeded: set[str] = set()
        self._lock = threading.Lock()

    def seed_once(self, sku: str) -> None:
        """Seed the synthetic snapshot for `sku` exactly once (steady state:
        a warm feature store doesn't rewrite itself every request)."""
        from pricing.adapters.synthetic_seed import seed_sku_features

        with self._lock:
            if sku in self._seeded:
                return
            seed_sku_features(self.store, self.dictionary, sku, self.clock.now())
            self._seeded.add(sku)

    def service_for(self, market: str) -> tuple[PriceService, bool]:
        """Cached per-market service; returns (service, pack_fallback_used)."""
        with self._lock:
            existing = self._services.get(market)
            if existing is not None:
                return existing
            pack, fallback = resolve_pack(self.packs, market)
            service = PriceService(
                store=self.store, dictionary=self.dictionary, model=self.model,
                log=self.log, clock=self.clock, pack=pack,
                pack_fallback=fallback, currency=self.currency,
            )
            self._services[market] = (service, fallback)
            return service, fallback

    @property
    def health(self) -> dict:
        pack, _ = resolve_pack(self.packs, self.default_market)
        return {
            "status": "ok",
            "model_version": self.model.model_version,
            "dictionary_version": self.dictionary.version,
            "pack": {"market": pack.market, "version": pack.version,
                     "status": pack.status},
        }
