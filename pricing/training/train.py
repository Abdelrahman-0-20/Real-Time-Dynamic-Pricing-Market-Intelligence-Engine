"""Train the baseline pricing model on the synthetic market.

    python -m pricing.training.train            # writes artifacts/
    python -m pricing.training.train --n 50000 # more data, same shape

Artifacts: xgb-baseline.json, feature_order.json, metrics.json.
metrics.json always includes a shuffled-target CONTROL model's regret: if the
real model ever does no better than the control, it has learned nothing
(leakage/no-signal alarm), and tests fail.
"""
from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timezone

import numpy as np
from xgboost import XGBRegressor

from pricing.training.synthetic import FEATURE_ORDER, generate, margin_at

MODEL_VERSION = "xgb-baseline-v1"
MODEL_INPUT_VERSION = "fv-2026.11"


def _regret(y_true_price: np.ndarray, pred_price: np.ndarray, truth: dict[str, np.ndarray], idx) -> float:
    oracle = margin_at(truth, idx, y_true_price)
    actual = margin_at(truth, idx, pred_price)
    with np.errstate(divide="ignore", invalid="ignore"):
        per_row = np.where(oracle > 0, (oracle - actual) / oracle, 0.0)
    return float(np.mean(per_row))


def train(
    n: int = 20_000,
    seed: int = 7,
    out_dir: str | pathlib.Path = "artifacts",
    control: bool = True,
) -> dict:
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    X, y, truth = generate(n=n, seed=seed)
    split = int(n * 0.8)
    X_train, y_train = X[:split], y[:split]
    X_test, y_test = X[split:], y[split:]
    test_idx = slice(split, n)

    def make_model() -> XGBRegressor:
        return XGBRegressor(
            n_estimators=120,
            max_depth=6,
            learning_rate=0.10,
            subsample=0.9,
            n_jobs=2,
            random_state=seed,
        )

    model = make_model()
    model.fit(X_train, y_train)
    pred = model.predict(X_test)

    metrics: dict = {
        "model_version": MODEL_VERSION,
        "model_input_version": MODEL_INPUT_VERSION,
        "n_samples": n,
        "seed": seed,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "mae_price": float(np.mean(np.abs(pred - y_test))),
        "mean_regret": _regret(y_test, pred, truth, test_idx),
    }

    if control:
        control_model = make_model()
        shuffled = np.random.default_rng(seed).permutation(y_train)
        control_model.fit(X_train, shuffled)
        control_pred = control_model.predict(X_test)
        metrics["control_mean_regret"] = _regret(y_test, control_pred, truth, test_idx)
        metrics["control_mae_price"] = float(np.mean(np.abs(control_pred - y_test)))

    model.save_model(str(out / "xgb-baseline.json"))
    (out / "feature_order.json").write_text(
        json.dumps(
            {"feature_order": FEATURE_ORDER, "model_input_version": MODEL_INPUT_VERSION},
            indent=2,
        )
        + "\n"
    )
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pricing.training.train",
        description="Train the baseline XGBoost pricing model on synthetic data.",
    )
    parser.add_argument("--n", type=int, default=20_000, help="samples to generate (default 20000)")
    parser.add_argument("--seed", type=int, default=7, help="RNG seed for reproducibility")
    parser.add_argument("--out", default="artifacts", help="output directory (default artifacts)")
    parser.add_argument("--no-control", action="store_true", help="skip the shuffled-target control model")
    args = parser.parse_args(argv)

    metrics = train(n=args.n, seed=args.seed, out_dir=args.out, control=not args.no_control)
    print(f"model      {metrics['model_version']}  ({metrics['n_samples']} samples)")
    print(f"MAE        {metrics['mae_price']:.2f} price units")
    print(f"regret     {metrics['mean_regret']:.2%} vs oracle")
    if "control_mean_regret" in metrics:
        print(f"control    {metrics['control_mean_regret']:.2%} (shuffled targets — should be far worse)")
    print(f"written    {args.out}/xgb-baseline.json, feature_order.json, metrics.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
