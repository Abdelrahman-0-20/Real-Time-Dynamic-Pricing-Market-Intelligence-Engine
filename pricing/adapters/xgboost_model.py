"""XGBoost ModelServer adapter (ADR-005: XGBoost primary, in-process).

The only place xgboost/numpy may appear on the serving path — behind the
ModelServer port. Missing features map to NaN, XGBoost's native missing path.
"""
from __future__ import annotations

import json
import pathlib

import numpy as np
import xgboost as xgb

_ARTIFACT_HINT = (
    "model artifact not found at '{path}'. "
    "Fix: run `python -m pricing.training.train` from the repo root first."
)


class XGBoostModel:
    def __init__(self, model_path: str | pathlib.Path, feature_order_path: str | pathlib.Path, version: str) -> None:
        model_path = pathlib.Path(model_path)
        feature_order_path = pathlib.Path(feature_order_path)
        if not model_path.exists():
            raise FileNotFoundError(_ARTIFACT_HINT.format(path=model_path))
        if not feature_order_path.exists():
            raise FileNotFoundError(_ARTIFACT_HINT.format(path=feature_order_path))
        self._booster = xgb.Booster()
        self._booster.load_model(str(model_path))
        # Single-row inference has no parallel work to spread: the default
        # thread pool costs more in sync overhead than it saves. Measured on
        # this path: default 8.2ms -> nthread=1 4.2ms -> inplace_predict 1.8ms.
        self._booster.set_param({"nthread": 1})
        payload = json.loads(feature_order_path.read_text())
        self._order: list[str] = payload["feature_order"]
        self.model_version = version

    @property
    def feature_order(self) -> list[str]:
        return list(self._order)

    def score(self, features: dict[str, float]) -> float:
        row = np.asarray(
            [[float(features.get(name, np.nan)) for name in self._order]],
            dtype=np.float32,
        )
        # inplace_predict skips DMatrix construction/feature-name validation
        # per call — the difference between blowing and holding the 15ms
        # inference budget (see docs/latency-harness.md stage budgets).
        return float(self._booster.inplace_predict(row)[0])

    def explain(self, features: dict[str, float]) -> list[tuple[str, float]]:
        """Per-feature contributions (SHAP) — the HITL 'why' behind a price.

        Not on the hot path: pred_contribs uses DMatrix (slower, fine here).
        Returns (feature, contribution) sorted by absolute impact.
        """
        row = np.asarray(
            [[float(features.get(name, np.nan)) for name in self._order]],
            dtype=np.float32,
        )
        matrix = xgb.DMatrix(row, missing=np.nan)
        contribs = self._booster.predict(matrix, pred_contribs=True)[0]
        pairs = list(zip(self._order, (float(c) for c in contribs[:-1])))
        return sorted(pairs, key=lambda p: -abs(p[1]))
