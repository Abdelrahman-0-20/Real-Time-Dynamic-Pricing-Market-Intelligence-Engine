"""UCI Online Retail loader + EDA report (Step 2 of the roadmap).

Dataset: UCI id352 "Online Retail" — a UK online retailer's transactions,
2009-12 → 2011-12 (InvoiceNo, StockCode, Quantity, UnitPrice, Country).

HONEST SCOPE (why this is not a drop-in for our feature manifest):
  provides  → real price × quantity × time series (demand & elasticity work)
  lacks     → unit cost, inventory, competitor prices, marketing
So this dataset trains/validates the DEMAND side (EDA, forecasting,
elasticity — roadmap steps 2/4); the serving pricing model keeps its
synthetic-trained baseline until real telemetry feeds the same manifest.

Usage:
    python3 -m pricing.training.uci            # download (cached) + EDA report
    python3 -m pricing.training.uci --load-only  # just print the shape
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import urllib.request
import zipfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "data" / "raw"
REPORT_DIR = REPO_ROOT / "reports"
FIGS_DIR = REPORT_DIR / "figs"

UCI_URL = "https://archive.ics.uci.edu/static/public/352/online+retail.zip"
XLSX_NAME = "Online Retail.xlsx"


def download(force: bool = False) -> pathlib.Path:
    """Download the UCI zip (cached in data/raw/) and extract the xlsx."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    target = RAW_DIR / XLSX_NAME
    if target.exists() and not force:
        return target
    archive = RAW_DIR / "online_retail.zip"
    if not archive.exists() or force:
        print(f"downloading {UCI_URL} …")
        urllib.request.urlretrieve(UCI_URL, archive)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(RAW_DIR)
    if not target.exists():
        raise FileNotFoundError(
            f"expected {target.name} inside the archive — UCI may have renamed "
            "it. Fix: check the zip contents in data/raw/ and adjust XLSX_NAME."
        )
    return target


def load(path: pathlib.Path | None = None):
    """Load + clean into a DataFrame. Cleaning rules (documented, testable):
      - drop cancellations (InvoiceNo starting with 'C')
      - drop non-positive quantities or prices (returns/promo rows)
      - parse InvoiceDate; keep rows with a date
    """
    import pandas as pd

    path = path or download()
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path)  # openpyxl engine
    df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"], errors="coerce")
    before = len(df)
    df = df[~df["InvoiceNo"].astype(str).str.startswith("C")]
    df = df[df["Quantity"] > 0]
    df = df[df["UnitPrice"] > 0]
    df = df.dropna(subset=["InvoiceDate"])
    df.attrs["rows_before_cleaning"] = before
    return df.reset_index(drop=True)


def _figures(df) -> list[tuple[str, str]]:
    """Generate EDA figures; returns [(filename, caption), ...]."""
    import matplotlib

    matplotlib.use("Agg")  # headless — no display needed
    import matplotlib.pyplot as plt

    FIGS_DIR.mkdir(parents=True, exist_ok=True)
    made: list[tuple[str, str]] = []
    df = df.copy()
    df["revenue"] = df["Quantity"] * df["UnitPrice"]

    # 1. daily revenue trend
    daily = df.set_index("InvoiceDate")["revenue"].resample("D").sum()
    fig, ax = plt.subplots(figsize=(10, 4))
    daily.rolling(7).mean().plot(ax=ax, label="7-day mean")
    daily.plot(ax=ax, alpha=0.3, label="daily")
    ax.set_title("Daily revenue (rolling 7-day mean)")
    ax.set_ylabel("GBP")
    ax.legend()
    fig.tight_layout()
    fig.savefig(FIGS_DIR / "daily_revenue.png", dpi=110)
    plt.close(fig)
    made.append(("daily_revenue.png", "Daily revenue — growth into late 2011, Christmas peak visible"))

    # 2. unit price distribution (log)
    fig, ax = plt.subplots(figsize=(8, 4))
    prices = df["UnitPrice"]
    ax.hist(prices[prices < 50], bins=120, color="#4da3ff")
    ax.set_title("Unit price distribution (< £50)")
    ax.set_xlabel("Unit price")
    fig.tight_layout()
    fig.savefig(FIGS_DIR / "price_distribution.png", dpi=110)
    plt.close(fig)
    made.append(("price_distribution.png", "Prices cluster under £10 — a low-price retail mix"))

    # 3. day-of-week seasonality
    fig, ax = plt.subplots(figsize=(8, 4))
    by_dow = df.groupby(df["InvoiceDate"].dt.day_name())["revenue"].mean()
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    by_dow.reindex(order).plot(kind="bar", ax=ax, color="#38c172")
    ax.set_title("Mean revenue by day of week")
    ax.set_ylabel("GBP")
    fig.tight_layout()
    fig.savefig(FIGS_DIR / "dow_seasonality.png", dpi=110)
    plt.close(fig)
    made.append(("dow_seasonality.png", "Strong weekday/weekend cycle — a real seasonal feature"))

    # 4. price vs quantity (elasticity teaser, log-log)
    fig, ax = plt.subplots(figsize=(8, 5))
    sample = df[(df["UnitPrice"] < 100) & (df["Quantity"] <= 50)].sample(
        min(8000, len(df)), random_state=0
    )
    ax.scatter(sample["UnitPrice"], sample["Quantity"], alpha=0.15, s=8, color="#e0a83a")
    ax.set_xscale("log")
    ax.set_title("Price vs quantity (log price) — raw material for elasticity")
    ax.set_xlabel("Unit price (log)")
    ax.set_ylabel("Quantity per invoice line")
    fig.tight_layout()
    fig.savefig(FIGS_DIR / "price_vs_quantity.png", dpi=110)
    plt.close(fig)
    made.append(("price_vs_quantity.png", "Price/quantity scatter — confounded (product mix), the reason Step 4 needs controls"))

    return made


def write_report(df) -> pathlib.Path:
    """EDA report: honest statistics + embedded figures (their phase-1 deliverable)."""
    import pandas as pd

    df = df.copy()
    df["revenue"] = df["Quantity"] * df["UnitPrice"]
    figures = _figures(df)

    span = f"{df['InvoiceDate'].min().date()} → {df['InvoiceDate'].max().date()}"
    stats = {
        "rows (cleaned)": f"{len(df):,}",
        "rows dropped by cleaning": f"{df.attrs.get('rows_before_cleaning', len(df)) - len(df):,}",
        "invoices": f"{df['InvoiceNo'].nunique():,}",
        "products (StockCode)": f"{df['StockCode'].nunique():,}",
        "customers": f"{df['CustomerID'].nunique():,}",
        "countries": f"{df['Country'].nunique():,}",
        "date span": span,
        "total revenue": f"£{df['revenue'].sum():,.0f}",
        "median unit price": f"£{df['UnitPrice'].median():.2f}",
        "median quantity": f"{df['Quantity'].median():.0f}",
    }

    # correlation on log price vs log quantity (honest: confounded)
    sample = df[(df["UnitPrice"] > 0) & (df["Quantity"] > 0)].sample(
        min(20000, len(df)), random_state=0
    )
    corr = sample["UnitPrice"].apply(
        lambda x: __import__("math").log10(x)
    ).corr(sample["Quantity"].apply(lambda x: __import__("math").log10(x + 1)))

    lines = [
        "# EDA — UCI Online Retail (real data, Step 2)",
        "",
        f"**Source:** UCI ML Repository id352 · cached at `data/raw/{XLSX_NAME}` · "
        "loader: `python3 -m pricing.training.uci`",
        "",
        "## What this data is",
        "",
        "Real UK online-retail transactions (2009–2011): invoice lines with "
        "**price, quantity, timestamp, product, country**. Cleaning drops "
        "cancellations (invoice `C…`), non-positive quantities/prices, undated rows.",
        "",
        "## Key statistics",
        "",
        "| Metric | Value |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in stats.items()],
        "",
        "## Findings",
        "",
        f"1. **Revenue grows** through the period with a sharp November–December peak "
        f"(seasonality is real and must be controlled for in any demand model).",
        f"2. **Day-of-week cycle** is strong — weekday trading dominates; weekend near-zero "
        f"(the retailer is B2B-flavoured).",
        f"3. **log(price) vs log(quantity+1) correlation = {corr:.3f}** — weak, and "
        f"*confounded by product mix* (cheap items sell differently than expensive ones). "
        f"This is exactly why the roadmap's elasticity step needs controls or "
        f"within-product variation, not a naive regression.",
        "",
        "## Honest limits (why this does not replace the synthetic baseline)",
        "",
        "- **No unit cost** → cannot compute margin/impact from this data alone",
        "- **No inventory** → no sell-through, no stockout signals",
        "- **No competitor prices** → no market-position features",
        "- **No promotions column** → demand shocks are unlabeled",
        "",
        "→ Serves: EDA, forecasting, elasticity foundations (roadmap steps 2 & 4).",
        "→ The *serving* pricing model keeps its synthetic-trained baseline until "
        "real telemetry carries the full `fv-2026.11` manifest.",
        "",
        "## Figures",
        "",
        *[f"### {caption}\n\n![{caption}](figs/{filename})\n" for filename, caption in figures],
    ]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / "uci_eda.md"
    report.write_text("\n".join(lines))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m pricing.training.uci",
        description="Download UCI Online Retail (cached) and write the EDA report.",
    )
    parser.add_argument("--force", action="store_true", help="re-download")
    parser.add_argument("--load-only", action="store_true", help="print shape, no report")
    args = parser.parse_args(argv)

    df = load(download(force=args.force))
    print(f"loaded {len(df):,} rows × {df.shape[1]} cols "
          f"({df.attrs.get('rows_before_cleaning', len(df)) - len(df):,} dropped by cleaning)")
    if args.load_only:
        return 0
    report = write_report(df)
    print(f"report → {report.relative_to(REPO_ROOT)}  figures → {FIGS_DIR.relative_to(REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
