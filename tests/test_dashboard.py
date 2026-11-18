"""Dashboard tests: the READ-ONLY rule is enforced as code, plus pure logic.

The dashboard is a projection — if anyone ever adds a write call here, this
file fails CI. That's the blueprint's red-team rule made executable.
"""
from __future__ import annotations

import importlib
import pathlib
import subprocess
import sys
import time
import urllib.request

import pytest

APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "app.py"


def test_import_is_side_effect_free() -> None:
    """Importing the module must not render a page or touch the network."""
    module = importlib.import_module("dashboard.app")
    assert hasattr(module, "render")
    assert module.REPO.name  # repo root resolved


def test_dashboard_is_read_only() -> None:
    """Blue-team gate: no write verbs may ever appear in the dashboard."""
    source = APP_PATH.read_text()
    # Write-CALL patterns only (prose like "no review buttons" is allowed —
    # the docstring is allowed to describe what it refuses to do).
    forbidden = [".post(", '"POST"', "'POST'", ".put(", ".delete(", "api_post"]
    violations = [token for token in forbidden if token in source]
    assert not violations, (
        f"dashboard must be read-only, found write-ish tokens: {violations}. "
        "Approvals belong to the Pricing Console (POST /v1/decisions/.../review)."
    )
    # It may only ever build GET requests.
    assert 'method="POST"' not in source and "method='POST'" not in source


def test_margin_stats_aggregates_and_tolerates_missing() -> None:
    from dashboard.app import margin_stats

    assert margin_stats([]) == {
        "decisions": 0, "with_impact": 0, "avg_margin_pct": None,
        "projected_daily_total": None, "degraded": 0, "holdout": 0,
    }
    decisions = [
        {"degraded": False, "policy_arm": "treatment",
         "impact": {"margin_pct": 0.5, "projected_daily_margin": 100.0}},
        {"degraded": True, "policy_arm": "holdout",
         "impact": {"margin_pct": 0.3, "projected_daily_margin": None}},
        {"degraded": False, "policy_arm": "treatment"},  # old record: no impact
    ]
    stats = margin_stats(decisions)
    assert stats["decisions"] == 3
    assert stats["with_impact"] == 2
    assert stats["avg_margin_pct"] == 0.4
    assert stats["projected_daily_total"] == 100.0
    assert stats["degraded"] == 1 and stats["holdout"] == 1


def test_decisions_by_day_counts_and_orders() -> None:
    from dashboard.app import decisions_by_day

    frame = decisions_by_day([
        {"requested_at": "2026-11-18T10:00:00+00:00"},
        {"requested_at": "2026-11-18T11:00:00+00:00"},
        {"requested_at": "2026-11-17T09:00:00+00:00"},
    ])
    assert list(frame["day"]) == ["2026-11-17", "2026-11-18"]
    assert list(frame["decisions"]) == [1, 2]
    assert decisions_by_day([]).empty


def test_rec_rows_flags_degraded_and_limits() -> None:
    from dashboard.app import rec_rows

    decisions = [
        {"sku": f"SKU-{i}", "served_price": 10.0, "trigger": "model",
         "policy_arm": "treatment", "degraded": False, "guardrail_actions": [],
         "requested_at": "2026-11-18T10:00:00+00:00"}
        for i in range(20)
    ]
    decisions[0]["degraded"] = True      # oldest — must NOT be in the newest5
    decisions[-1]["degraded"] = True     # newest — must be rows[0]
    rows = rec_rows(decisions, limit=5)
    assert len(rows) == 5
    assert rows[0]["sku"] == "SKU-19"          # newest first (bug this guards)
    assert any(r["degraded"] for r in rows)


def test_load_report_json_is_failure_tolerant() -> None:
    from dashboard.app import load_report_json

    assert load_report_json("evals/baseline.json") is not None   # committed file
    assert load_report_json("does/not/exist.json") is None       # absent → None
    # corrupt file → None, never an exception
    assert load_report_json("README.md") is None  # not JSON


def test_clamp_rows_surfaces_guardrail_history() -> None:
    from dashboard.app import clamp_rows

    decisions = [
        {"sku": "SKU-1", "requested_at": "2026-11-18T10:00:00+00:00",
         "guardrail_actions": [{"rule": "margin_ceiling", "from_price": 129.9,
                                "to_price": 119.99}]},
        {"sku": "SKU-2", "requested_at": "2026-11-18T11:00:00+00:00",
         "guardrail_actions": []},
    ]
    rows = clamp_rows(decisions)
    assert rows[0]["rule"] == "margin_ceiling"
    assert rows[0]["change"] == pytest.approx(-9.91, abs=1e-6)
    assert len(rows) == 1  # empty-clamp decisions contribute nothing


def test_headless_server_serves_the_page() -> None:
    """Launch Streamlit headless, expect HTTP 200, then shut down."""
    port = "8519"
    process = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(APP_PATH),
         "--server.headless", "true", "--server.port", port,
         "--browser.gatherUsageStats", "false"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 40
        code = 0
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=2
                ) as response:
                    code = response.status
                break
            except Exception:  # noqa: BLE001 — still booting
                time.sleep(0.5)
        assert code == 200, "streamlit never served the page within 40s"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
