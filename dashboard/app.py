"""Price Intelligence Dashboard — READ-ONLY projection of the pricing system.

    streamlit run dashboard/app.py            # opens http://localhost:8501

Rules (blueprint):
  * READ-ONLY: this app only ever GETs. No review buttons (the console owns
    the write surface), no POST/PUT anywhere — enforced by tests scanning
    this file for write verbs.
  * API-first: anything the API serves comes through the contract; committed
    report files (EDA, eval, bench) are read directly from disk.
  * Secrets: the API key lives in st.session_state only — never written,
    never cached to disk, never logged.
  * Every loader degrades to a friendly st.error with a fix — never a
    traceback in the UI.

Sections map to the reference plan's dashboard (their §6), with honest
placeholders where roadmap step 4 (forecasting/elasticity) lands.
"""
from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request

import pandas as pd
import streamlit as st

REPO = pathlib.Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Pure functions — no streamlit, no network: unit-tested directly.
# ---------------------------------------------------------------------------


def margin_stats(decisions: list[dict]) -> dict:
    """Aggregate expected impact across decisions (P0a's field)."""
    impacts = [d["impact"] for d in decisions if d.get("impact")]
    margins = [i["margin_pct"] for i in impacts]
    projected = [i.get("projected_daily_margin") for i in impacts
                 if i.get("projected_daily_margin") is not None]
    return {
        "decisions": len(decisions),
        "with_impact": len(impacts),
        "avg_margin_pct": round(sum(margins) / len(margins), 4) if margins else None,
        "projected_daily_total": round(sum(projected), 2) if projected else None,
        "degraded": sum(1 for d in decisions if d.get("degraded")),
        "holdout": sum(1 for d in decisions if d.get("policy_arm") == "holdout"),
    }


def decisions_by_day(decisions: list[dict]) -> pd.DataFrame:
    """Counts per calendar day (oldest first) — for the overview chart."""
    if not decisions:
        return pd.DataFrame({"day": [], "decisions": []})
    days = [d["requested_at"][:10] for d in decisions]
    counts: dict[str, int] = {}
    for day in sorted(days):
        counts[day] = counts.get(day, 0) + 1
    return pd.DataFrame({"day": list(counts), "decisions": list(counts.values())})


def rec_rows(decisions: list[dict], limit: int = 15) -> list[dict]:
    """Recommendation rows (newest first) — the 'what should we charge' view.

    The API returns decisions OLDEST-first (`tail()`), so newest-first means
    reversing the whole list, then taking `limit` — slicing first would show
    the oldest N (the bug this test caught).
    """
    rows = []
    for d in list(reversed(decisions))[:limit]:
        impact = d.get("impact") or {}
        rows.append({
            "sku": d["sku"],
            "price": d["served_price"],
            "unit_margin": impact.get("unit_margin"),
            "margin_pct": impact.get("margin_pct"),
            "trigger": d["trigger"],
            "arm": d["policy_arm"],
            "degraded": d["degraded"],
            "clamps": len(d["guardrail_actions"]),
            "at": d["requested_at"][11:19],
        })
    return rows


def signal_rows(signals: list[dict]) -> list[dict]:
    rows = []
    for s in signals:
        subject = s.get("subject") or {}
        rows.append({
            "type": s["signal_type"],
            "subject": subject.get("sku") or subject.get("category") or "?",
            "confidence": s["confidence"],
            "trust": s["source_trust"],
            "observed": str(s["observed_at"])[:19],
        })
    return rows


def load_report_json(relpath: str) -> dict | None:
    """Read a committed report artifact; None when absent (never raises)."""
    path = REPO / relpath
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def clamp_rows(decisions: list[dict]) -> list[dict]:
    """Every guardrail clamp ever applied — the Simulation Lab's v1 content
    (the π(p) curve itself arrives with roadmap step 4)."""
    rows = []
    for d in reversed(decisions):
        for action in d.get("guardrail_actions", []):
            rows.append({
                "sku": d["sku"],
                "rule": action["rule"],
                "from": action["from_price"],
                "to": action["to_price"],
                "change": round(action["to_price"] - action["from_price"], 2),
                "at": d["requested_at"][11:19],
            })
    return rows[:20]


# ---------------------------------------------------------------------------
# IO — GET only.
# ---------------------------------------------------------------------------


def api_get(base: str, key: str, path: str, timeout: float = 5.0):
    """GET a JSON endpoint. Raises with a fix-hinting message on failure."""
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        headers={"X-API-Key": key} if key else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch(base: str, key: str, path: str):
    """api_get with UI-grade error handling: friendly message, never a crash."""
    try:
        return api_get(base, key, path)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            st.error("401 — missing or invalid API key.\n\nFix: paste the key "
                     "into the sidebar (this session only; never written to disk).")
        else:
            st.error(f"HTTP {exc.code} on {path}.\n\nFix: check the API is "
                     "running (`uvicorn pricing.api.main:create_app --factory "
                     "--ws wsproto --port 8000`).")
        return None
    except Exception as exc:  # noqa: BLE001 — UI must degrade, not traceback
        st.error(f"Cannot reach the API at `{base}` ({type(exc).__name__}).\n\n"
                 "Fix: start it with PRICING_API_KEY set — see docs/API.md.")
        return None


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def render() -> None:
    st.set_page_config(page_title="Price Intelligence", page_icon="📈",
                       layout="wide")
    st.title("📈 Price Intelligence Dashboard")

    with st.sidebar:
        st.caption("READ-ONLY view — approvals happen in the Pricing Console")
        base = st.text_input("API base", value="http://127.0.0.1:8000")
        key = st.text_input("API key", type="password",
                            help="Session memory only — never written to disk.")
        st.caption(f"data: {REPO.name}/ · key in session state: "
                   f"{'yes' if key else 'no'}")

    health = fetch(base, key, "/health")
    if health is None:
        st.stop()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("status", health["status"])
    col2.metric("model", health["model_version"])
    col3.metric("features", health["dictionary_version"])
    col4.metric("pack", f"{health['pack']['market']}/{health['pack']['version']}")

    decisions = fetch(base, key, "/v1/decisions?limit=100") or []
    signals_state = fetch(base, key, "/v1/signals")
    signals = (signals_state or {}).get("signals", [])
    quarantines = (signals_state or {}).get("quarantines", [])

    t1, t2, t3, t4, t5, t6, t7 = st.tabs([
        "Market Overview", "Competitor Intelligence", "Recommendations",
        "Demand Forecasting", "Price Elasticity", "Simulation Lab",
        "Model Monitoring",
    ])

    with t1:  # ------------------------------------------------------------ Market
        st.subheader("Market Overview")
        if not decisions:
            st.info("No decisions yet — fix: price a SKU in the console or via "
                    "`POST /v1/price`.")
        else:
            stats = margin_stats(decisions)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("decisions", stats["decisions"])
            c2.metric("avg margin",
                      f"{stats['avg_margin_pct']:.1%}" if stats["avg_margin_pct"] is not None else "–")
            c3.metric("projected daily (current velocity)",
                      f"{stats['projected_daily_total']:,.0f}"
                      if stats["projected_daily_total"] is not None else "–")
            c4.metric("degraded", stats["degraded"])
            st.caption("projected daily = unit margin × observed velocity — "
                       "**not a forecast** (forecast: roadmap step 4).")
            st.line_chart(decisions_by_day(decisions).set_index("day"))
            st.image(str(REPO / "reports" / "figs" / "daily_revenue.png"),
                     caption="UCI real data: daily revenue (EDA, step 2)",
                     use_container_width=True)

    with t2:  # ------------------------------------------------------- Competitor
        st.subheader("Competitor Intelligence")
        if signals_state is None:
            st.info("No crawl state — fix: `python3 -m pricing.intel.run` "
                    "then refresh.")
        elif not signals:
            st.info("Crawl ran but produced no signals.")
        else:
            st.caption(f"last crawl {signals_state.get('ran_at', '?')[:19]} · "
                       f"{len(quarantines)} quarantined (gates working)")
            st.dataframe(pd.DataFrame(signal_rows(signals)), use_container_width=True)
            if quarantines:
                st.expander(f"Quarantined ({len(quarantines)})").table(
                    pd.DataFrame([{"reason": q["reason"], "detail": q["detail"][:80]}
                                  for q in quarantines]))

    with t3:  # -------------------------------------------------- Recommendations
        st.subheader("Pricing Recommendations")
        if not decisions:
            st.info("Nothing priced yet — fix: price a SKU first.")
        else:
            st.caption("Newest first · impact = expected margin at the served price")
            st.dataframe(pd.DataFrame(rec_rows(decisions)), use_container_width=True)

    with t4:  # -------------------------------------------------- Demand (step 4)
        st.subheader("Demand Forecasting")
        demand = load_report_json("reports/uci_demand_metrics.json")
        if demand:
            c1, c2, c3 = st.columns(3)
            c1.metric("MAE (model)", demand["mae_model"])
            c2.metric("MAE (naive baseline)", demand["mae_naive_persistence"])
            c3.metric("skill vs naive", f"{demand['skill_vs_naive']:+.1%}")
            st.caption(demand["split"] + " · real UCI retail data (step 2)")
            st.bar_chart(pd.Series(demand["feature_importance"]))
            st.info("Per-SKU demand forecasts arrive in roadmap step 4 — "
                    "this section shows the trained daily model as it stands.")
        else:
            st.info("No demand metrics — fix: `python3 -m pricing.training.uci_demand`")

    with t5:  # -------------------------------------------------- Elasticity (s4)
        st.subheader("Price Elasticity")
        st.image(str(REPO / "reports" / "figs" / "price_vs_quantity.png"),
                 caption="Real data: price vs quantity — confounded by product mix",
                 use_container_width=True)
        eda = (REPO / "reports" / "uci_eda.md").read_text() if (REPO / "reports" / "uci_eda.md").exists() else ""
        corr = next((line for line in eda.splitlines() if "correlation =" in line), None)
        if corr:
            st.markdown(corr.replace("**", ""))
        st.info("Controlled elasticity estimation (within-product) arrives in "
                "roadmap step 4 — naive correlation here is deliberately "
                "labeled as confounded.")

    with t6:  # -------------------------------------------------- Simulation (s4)
        st.subheader("Simulation Lab")
        rows = clamp_rows(decisions)
        if rows:
            st.caption("v1: what the guardrails actually did to proposed prices")
            st.dataframe(pd.DataFrame(rows), use_container_width=True)
        else:
            st.info("No clamps recorded yet.")
        st.warning("π(p) profit-curve simulation arrives with the demand-curve "
                   "optimizer (roadmap step 4).")

    with t7:  # -------------------------------------------------- Monitoring
        st.subheader("Model Monitoring")
        baseline = load_report_json("evals/baseline.json")
        bench = load_report_json("var/last_bench.json")
        if baseline:
            entries = baseline.get("baselines", {})
            rows = [{"extractor": name, "f1": data.get("f1"),
                     "cases": data.get("n_cases")}
                    for name, data in entries.items()]
            st.caption("extraction eval baselines (gates run in CI)")
            st.table(pd.DataFrame(rows))
        else:
            st.info("No eval baseline — fix: `python3 -m pricing.evals.run --update-baseline`")
        if bench:
            c1, c2, c3 = st.columns(3)
            c1.metric("HTTP e2e p99", f"{bench['e2e_p99_ms']} ms")
            c2.metric("serve p99", f"{bench['serve_p99_ms']} ms")
            c3.metric("gate", bench["result"])
            st.caption(f"last bench {bench.get('git_sha', '?')} · threshold "
                       f"{bench['gate_max_p99_ms']} ms")
        else:
            st.info("No bench yet — fix: `python3 benchmarks/http_latency.py`")
        st.caption("Drift monitoring activates with production traffic "
                   "(roadmap: shadow → canary → holdout).")


if __name__ == "__main__":
    render()
