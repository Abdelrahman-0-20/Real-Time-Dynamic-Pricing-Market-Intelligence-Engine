"""Training sanity: the model must learn signal, not noise or leakage.

The shuffled-target CONTROL is the anti-leakage alarm (train.py always
records it): if the real model ever does no better than a model trained on
shuffled targets, it has learned nothing, and these tests fail.
"""
from __future__ import annotations

import json
import pathlib

from pricing.training.synthetic import FEATURE_ORDER
from tests.helpers import load_dictionary


def test_training_produces_artifacts_and_beats_the_control(tmp_path: pathlib.Path) -> None:
    from pricing.training.train import train

    metrics = train(n=6000, seed=11, out_dir=tmp_path, control=True)

    # Artifacts the serving path depends on.
    assert (tmp_path / "xgb-baseline.json").exists()
    assert (tmp_path / "feature_order.json").exists()
    assert (tmp_path / "metrics.json").exists()

    # The model learned real signal: error far below the shuffled control.
    assert metrics["mae_price"] > 0
    assert metrics["control_mean_regret"] is not None
    assert metrics["mae_price"] * 3 < metrics["control_mae_price"], (
        f"model MAE {metrics['mae_price']:.2f} vs control {metrics['control_mae_price']:.2f} "
        "— real model is not clearly beating shuffled targets (leakage/no-signal alarm)"
    )
    # And economically meaningful: mean regret against the true oracle price.
    assert metrics["mean_regret"] < 0.10, f"regret too high: {metrics['mean_regret']:.2%}"


def test_artifacts_agree_with_the_feature_contract(tmp_path: pathlib.Path) -> None:
    from pricing.training.train import train

    train(n=2000, seed=5, out_dir=tmp_path, control=False)
    order = json.loads((tmp_path / "feature_order.json").read_text())

    assert order["feature_order"] == FEATURE_ORDER
    assert order["model_input_version"] == load_dictionary().version
    saved = json.loads((tmp_path / "metrics.json").read_text())
    assert saved["model_input_version"] == load_dictionary().version
