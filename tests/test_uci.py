"""UCI loader + demand model tests (Step 2).

Network download is env-gated (UCI_LIVE=1); everything else runs on generated
fixtures — CI must never depend on UCI's servers.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from pricing.training.uci import load, write_report
from pricing.training.uci_demand import daily_frame, evaluate

LIVE = pytest.mark.skipif(
    not os.environ.get("UCI_LIVE"), reason="set UCI_LIVE=1 to hit archive.ics.uci.edu"
)


def _write_tiny_csv(path: pathlib.Path, days: int = 120) -> pathlib.Path:
    """Synthetic transactions: valid rows + every cleaning-rule violation."""
    import pandas as pd

    rows = []
    start = pd.Timestamp("2011-01-03")
    for day in range(days):
        date = start + pd.Timedelta(days=day)
        # weekday/weekend pattern so the demand model has something to learn
        n = 3 if date.day_of_week < 5 else 1
        for i in range(n):
            rows.append({
                "InvoiceNo": f"53{i:06d}", "StockCode": f"SKU-{day % 7}",
                "Quantity": 4 + (day % 5), "InvoiceDate": date + pd.Timedelta(hours=9),
                "UnitPrice": 3.5 + (day % 4), "CustomerID": 12345.0,
                "Country": "United Kingdom",
            })
    # rule violations — all must be dropped by load()
    rows += [
        {"InvoiceNo": "C5399991", "StockCode": "X", "Quantity": -3,
         "InvoiceDate": start, "UnitPrice": 5.0, "CustomerID": 1.0, "Country": "UK"},
        {"InvoiceNo": "5399992", "StockCode": "X", "Quantity": -2,   # negative
         "InvoiceDate": start, "UnitPrice": 5.0, "CustomerID": 1.0, "Country": "UK"},
        {"InvoiceNo": "5399993", "StockCode": "X", "Quantity": 4,     # zero price
         "InvoiceDate": start, "UnitPrice": 0.0, "CustomerID": 1.0, "Country": "UK"},
        {"InvoiceNo": "5399994", "StockCode": "X", "Quantity": 4,     # no date
         "InvoiceDate": None, "UnitPrice": 5.0, "CustomerID": 1.0, "Country": "UK"},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_cleaning_drops_every_rule_violation(tmp_path: pathlib.Path) -> None:
    df = load(_write_tiny_csv(tmp_path / "tiny.csv"))
    assert not df["InvoiceNo"].astype(str).str.startswith("C").any()
    assert (df["Quantity"] > 0).all()
    assert (df["UnitPrice"] > 0).all()
    assert df["InvoiceDate"].notna().all()
    assert df.attrs["rows_before_cleaning"] == len(df) + 4  # the 4 planted rows


def test_daily_frame_uses_only_past_data(tmp_path: pathlib.Path) -> None:
    import pandas as pd

    df = load(_write_tiny_csv(tmp_path / "tiny.csv"))
    frame = daily_frame(df)

    # Leakage guard: lag1 at day t must equal units at day t-1, measured
    # against the SAME unfed series the implementation builds (full index,
    # zero-filled) — recompute it here rather than trusting the trimmed frame.
    full = (
        df.set_index("InvoiceDate").resample("D")["Quantity"].sum().fillna(0.0)
    )
    for t in frame.index[1:8]:
        expected = full.loc[t - pd.Timedelta(days=1)]
        assert frame.loc[t, "units_lag1"] == expected, f"lag1 wrong at {t.date()}"

    # target is the NEXT day, never the same day
    assert not (frame["units_next"] == frame["units"]).all()
    assert len(frame) >= 40, "need enough days for the temporal split"


def test_model_beats_naive_on_a_learnable_pattern(tmp_path: pathlib.Path) -> None:
    """Weekend-zero pattern: persistence (tomorrow=today) is provably bad on
    Mondays — a model with calendar features must win. Deterministic seed."""
    df = load(_write_tiny_csv(tmp_path / "pattern.csv"))
    metrics = evaluate(daily_frame(df))
    assert metrics["beats_naive"] is True
    assert metrics["mae_model"] < metrics["mae_naive_persistence"]
    assert metrics["split"].startswith("temporal")
    assert "target_dow" in metrics["feature_importance"]  # calendar drove it


def test_report_writes_with_honest_limits(tmp_path: pathlib.Path, monkeypatch) -> None:
    import pricing.training.uci as uci

    monkeypatch.setattr(uci, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(uci, "FIGS_DIR", tmp_path / "figs")
    df = load(_write_tiny_csv(tmp_path / "tiny.csv"))
    report = write_report(df)
    text = report.read_text()
    assert "Honest limits" in text
    assert "No unit cost" in text
    assert (tmp_path / "figs" / "daily_revenue.png").exists()


@LIVE
def test_live_download_and_load() -> None:
    from pricing.training.uci import download

    df = load(download())
    assert len(df) > 500_000, "UCI Online Retail should have ~530k cleaned rows"
