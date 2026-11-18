"""Real-data demand model on UCI Online Retail (Step 2 — the "trained on real
data" deliverable).

Design (honest by construction):
  * daily aggregation → features at day t predict units at day t+1
  * lags use shift(1)/shift(7) — ONLY past data (no leakage)
  * TEMPORAL split: last 20% of days = test (never random — time series)
  * compared against a naive persistence baseline (tomorrow = today);
    a model that can't beat persistence has learned nothing worth shipping
  * metrics land in reports/uci_demand_metrics.json (committed — artifacts/ is
    gitignored, reports are the record)

Usage: python3 -m pricing.training.uci_demand
"""
from __future__ import annotations

import argparse
import json
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REPORTS = REPO_ROOT / "reports"


def daily_frame(df):
    """Daily units + average price with strictly-past lag features."""
    import pandas as pd

    daily = (
        df.set_index("InvoiceDate")
        .resample("D")
        .agg(units=("Quantity", "sum"), avg_price=("UnitPrice", "mean"))
        .fillna(0.0)
    )
    frame = pd.DataFrame(index=daily.index)
    frame["units"] = daily["units"]
    frame["avg_price"] = daily["avg_price"]
    # strictly-past lags (leakage guard: everything is shift(1)-based)
    frame["units_lag1"] = daily["units"].shift(1)
    frame["units_lag7"] = daily["units"].shift(7)
    frame["units_roll7"] = daily["units"].shift(1).rolling(7).mean()
    frame["price_lag1"] = daily["avg_price"].shift(1)
    # calendar of the TARGET day (dates are known in advance — legal features)
    target = daily.index + pd.Timedelta(days=1)
    frame["target_dow"] = target.day_of_week
    frame["target_month"] = target.month
    # target: next-day units
    frame["units_next"] = daily["units"].shift(-1)
    return frame.dropna()


def evaluate(frame, seed: int = 7) -> dict:
    """Temporal80/20 split; XGBoost vs naive persistence baseline."""
    from xgboost import XGBRegressor

    features = ["units_lag1", "units_lag7", "units_roll7", "avg_price",
                "price_lag1", "target_dow", "target_month"]
    X, y = frame[features].to_numpy(), frame["units_next"].to_numpy()
    split = int(len(frame) * 0.8)
    if split < 30 or len(frame) - split < 10:
        raise ValueError(
            f"not enough daily rows after lagging ({len(frame)}) — "
            "Fix: use the full UCI dataset (run without fixture limits)."
        )

    model = XGBRegressor(n_estimators=120, max_depth=4, learning_rate=0.1,
                         subsample=0.9, random_state=seed)
    model.fit(X[:split], y[:split])
    pred = model.predict(X[split:])
    naive = frame["units_lag1"].to_numpy()[split:]  # tomorrow = today

    def mae(a, b) -> float:
        return float(abs(a - b).mean())

    mae_model, mae_naive = mae(pred, y[split:]), mae(naive, y[split:])
    skill = 1.0 - (mae_model / mae_naive) if mae_naive else float("nan")
    return {
        "dataset": "UCI Online Retail (id352)",
        "rows_daily": int(len(frame)),
        "split": f"temporal 80/20 ({split} train / {len(frame) - split} test days)",
        "mae_model": round(mae_model, 2),
        "mae_naive_persistence": round(mae_naive, 2),
        "skill_vs_naive": round(skill, 4),
        "beats_naive": bool(mae_model < mae_naive),
        "feature_importance": {
            name: round(float(score), 4)
            for name, score in zip(features, model.feature_importances_)
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m pricing.training.uci_demand",
        description="Train + evaluate the daily demand model on UCI data.",
    )
    parser.add_argument("--no-report", action="store_true", help="skip writing the report")
    args = parser.parse_args(argv)

    from pricing.training.uci import load

    df = load()
    frame = daily_frame(df)
    metrics = evaluate(frame)

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "uci_demand_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")

    print(f"daily rows {metrics['rows_daily']} | {metrics['split']}")
    print(f"MAE model {metrics['mae_model']} vs naive {metrics['mae_naive_persistence']} "
          f"→ skill {metrics['skill_vs_naive']:+.1%} "
          f"({'beats naive ✓' if metrics['beats_naive'] else 'DOES NOT beat naive ✗'})")
    print(f"metrics → reports/uci_demand_metrics.json")

    if not args.no_report:
        top = sorted(metrics["feature_importance"].items(), key=lambda kv: -kv[1])[:4]
        report = REPORTS / "uci_demand.md"
        report.write_text("\n".join([
            "# Demand model on real data (Step 2)",
            "",
            f"- **Dataset:** {metrics['dataset']} · {metrics['rows_daily']} daily rows",
            f"- **Split:** {metrics['split']} (temporal — no shuffling, no leakage)",
            f"- **MAE:** model **{metrics['mae_model']}** vs naive persistence "
            f"**{metrics['mae_naive_persistence']}** → skill "
            f"**{metrics['skill_vs_naive']:+.1%}**",
            f"- **Verdict:** {'beats the naive baseline ✓' if metrics['beats_naive'] else 'fails to beat naive ✗ — do not ship'}",
            "",
            "Top features: " + ", ".join(f"`{k}` ({v:.2f})" for k, v in top),
            "",
            "> Honest note: much of the skill comes from calendar features "
            "(day-of-week) — the model mostly learns *when* people buy, not "
            "*how price changes demand*. Price elasticity needs within-product "
            "controls (roadmap step 4), which is why this is a demand model, "
            "not an elasticity claim.",
        ]))
        print(f"report → {report.relative_to(REPO_ROOT)}")
    return 0 if metrics["beats_naive"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
