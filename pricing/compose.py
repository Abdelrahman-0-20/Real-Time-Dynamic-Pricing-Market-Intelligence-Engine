"""Composition root — the ONLY module allowed to wire adapters together.

Loads config (JSON), resolves the market's policy pack (ADR-008), and
assembles PriceService with concrete ports. Paths default to the repo root so
commands work from any directory (DX rule: no 'run me from the right folder').
"""
from __future__ import annotations

import json
import pathlib

from pricing.adapters.clocks import SystemClock
from pricing.adapters.memory_decision_log import InMemoryDecisionLog
from pricing.adapters.memory_features import InMemoryFeatureStore
from pricing.adapters.xgboost_model import XGBoostModel
from pricing.app.serve import PriceService
from pricing.core.features import FeatureDictionary
from pricing.core.policy import PolicyPack, resolve_pack

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_packs(config_dir: str | pathlib.Path | None = None) -> dict[str, PolicyPack]:
    directory = pathlib.Path(config_dir) if config_dir else REPO_ROOT / "config" / "guardrails"
    packs: dict[str, PolicyPack] = {}
    for path in sorted(directory.glob("*.json")):
        pack = PolicyPack.from_dict(json.loads(path.read_text()))
        packs[pack.market] = pack
    return packs


def load_dictionary(config_dir: str | pathlib.Path | None = None) -> FeatureDictionary:
    directory = pathlib.Path(config_dir) if config_dir else REPO_ROOT / "config" / "features"
    candidates = sorted(directory.glob("fv-*.json"))
    if not candidates:
        raise FileNotFoundError(
            f"no feature dictionary found in {directory}. "
            "Fix: expected config/features/fv-2026.11.json (see docs/feature-manifest.md)."
        )
    return FeatureDictionary.from_dict(json.loads(candidates[-1].read_text()))


def build_service(
    *,
    market: str = "EG",
    model_dir: str | pathlib.Path | None = None,
    config_dir: str | pathlib.Path | None = None,
    model_version: str = "xgb-baseline-v1",
    currency: str = "EGP",
) -> tuple[PriceService, InMemoryFeatureStore, InMemoryDecisionLog]:
    packs = load_packs(config_dir)
    pack, fallback_used = resolve_pack(packs, market)
    dictionary = load_dictionary(config_dir)
    model_root = pathlib.Path(model_dir) if model_dir else REPO_ROOT / "artifacts"
    model = XGBoostModel(
        model_root / "xgb-baseline.json",
        model_root / "feature_order.json",
        model_version,
    )
    store = InMemoryFeatureStore()
    log = InMemoryDecisionLog()
    service = PriceService(
        store=store,
        dictionary=dictionary,
        model=model,
        log=log,
        clock=SystemClock(),
        pack=pack,
        pack_fallback=fallback_used,
        currency=currency,
    )
    return service, store, log
