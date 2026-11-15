"""HTTP API tests: auth fail-closed, contract-valid responses, error envelopes.

The API is the product's public face — its error contract ({error, fix}) and
its auth stance (refuse to start open) are tested as behavior, not config.
"""
from __future__ import annotations

import json

import jsonschema
import pytest
from fastapi.testclient import TestClient

from pricing.api.main import create_app
from tests.helpers import CONTRACTS, ROOT, load_schema

API_KEY = "test-key-123"


def _ref(schema_name: str, def_name: str) -> dict:
    schema = json.loads((CONTRACTS / schema_name).read_text())
    return {"$defs": schema["$defs"], "$ref": f"#/$defs/{def_name}"}


@pytest.fixture()
def make_client(tmp_path, tiny_artifacts):
    def factory(**kwargs):
        kwargs.setdefault("api_key", API_KEY)
        kwargs.setdefault("artifacts_dir", str(tiny_artifacts))
        kwargs.setdefault("var_dir", str(tmp_path / "var"))
        kwargs.setdefault("config_dir", str(ROOT / "config"))
        app = create_app(**kwargs)
        client = TestClient(app)
        client.headers.update({"X-API-Key": API_KEY})
        return client

    return factory


# ---------------------------------------------------------------------- auth

def test_health_is_open_and_reports_the_stack(make_client) -> None:
    client = make_client()
    client.headers.clear()
    response = client.get("/health")
    assert response.status_code == 200
    jsonschema.validate(response.json(), _ref("price-api.schema.json", "HealthResponse"))
    assert response.json()["auth"] == "api_key"
    assert response.json()["pack"]["status"] == "placeholder"


def test_price_without_key_is_401_with_a_fix(make_client) -> None:
    client = make_client()
    client.headers.clear()
    response = client.post("/v1/price", json={"sku": "SKU-4471"})
    assert response.status_code == 401
    body = response.json()
    jsonschema.validate(body, _ref("price-api.schema.json", "ErrorEnvelope"))
    assert "X-API-Key" in body["fix"]


def test_wrong_key_is_401(make_client) -> None:
    client = make_client()
    client.headers.update({"X-API-Key": "wrong"})
    assert client.post("/v1/price", json={"sku": "SKU-1"}).status_code == 401


def test_startup_refuses_to_run_open(tmp_path, tiny_artifacts) -> None:
    with pytest.raises(RuntimeError, match="Fix:"):
        create_app(
            api_key=None, dev=False,
            artifacts_dir=str(tiny_artifacts), var_dir=str(tmp_path / "var"),
            config_dir=str(ROOT / "config"),
        )


def test_dev_mode_runs_open_and_says_so(tmp_path, tiny_artifacts) -> None:
    app = create_app(
        dev=True, api_key=None,
        artifacts_dir=str(tiny_artifacts), var_dir=str(tmp_path / "var"),
        config_dir=str(ROOT / "config"),
    )
    client = TestClient(app)  # no key header at all
    assert client.get("/health").json()["auth"] == "dev-open"
    assert client.post("/v1/price", json={"sku": "SKU-1"}).status_code == 200


def test_missing_artifacts_fail_startup_with_the_fix(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="Fix:"):
        create_app(
            api_key="k",
            artifacts_dir=str(tmp_path / "no-artifacts"),
            var_dir=str(tmp_path / "var"), config_dir=str(ROOT / "config"),
        )


# ------------------------------------------------------------ price/decisions

def test_price_response_is_a_decision_contract(make_client) -> None:
    client = make_client()
    response = client.post("/v1/price", json={"sku": "SKU-4471"})
    assert response.status_code == 200
    record = response.json()
    jsonschema.validate(record, load_schema("decision-record.schema.json"))
    assert record["served_price"] > 0
    assert record["trigger"] == "model"

    # Server-side serve time must ride along for the latency gate.
    serve_ms = float(response.headers["X-Serve-Ms"])
    assert serve_ms >= 0

    # The decision must be retrievable by its trace (audit through HTTP).
    fetched = client.get(f"/v1/decisions/{record['trace_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["fingerprint"] == record["fingerprint"]


def test_unknown_trace_is_404_with_a_fix(make_client) -> None:
    client = make_client()
    response = client.get("/v1/decisions/no-such-trace")
    assert response.status_code == 404
    body = response.json()
    jsonschema.validate(body, _ref("price-api.schema.json", "ErrorEnvelope"))
    assert "trace" in body["fix"]


def test_invalid_sku_is_422_envelope_naming_the_field(make_client) -> None:
    client = make_client()
    response = client.post("/v1/price", json={"sku": "not a sku!!"})
    assert response.status_code == 422
    body = response.json()
    jsonschema.validate(body, _ref("price-api.schema.json", "ErrorEnvelope"))
    assert "sku" in body["error"]
    assert "price-api.schema.json" in body["fix"]


def test_unknown_market_degrades_but_still_prices(make_client) -> None:
    client = make_client()
    response = client.post("/v1/price", json={"sku": "SKU-1", "market": "ZZ"})
    assert response.status_code == 200
    record = response.json()
    assert record["degraded"] is True            # ADR-008: stricter fallback, flagged
    assert record["policy_pack"]["status"] == "derived"


def test_batch_prices_and_caps(make_client) -> None:
    client = make_client()
    response = client.post(
        "/v1/prices", json={"items": [{"sku": f"SKU-{i}"} for i in range(3)]}
    )
    assert response.status_code == 200
    decisions = response.json()
    assert len(decisions) == 3
    for record in decisions:
        jsonschema.validate(record, load_schema("decision-record.schema.json"))

    too_many = client.post(
        "/v1/prices", json={"items": [{"sku": f"SKU-{i}"} for i in range(101)]}
    )
    assert too_many.status_code == 422
    assert "fix" in too_many.json()

    empty = client.post("/v1/prices", json={"items": []})
    assert empty.status_code == 422


# ------------------------------------------------------------- contract sync

def test_request_shapes_match_the_published_contract() -> None:
    valid = {"$defs": json.loads((CONTRACTS / "price-api.schema.json").read_text())["$defs"],
             "$ref": "#/$defs/PriceRequest"}
    jsonschema.validate({"sku": "SKU-4471", "market": "EG"}, valid)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"sku": "bad sku!", "market": "eg"}, valid)


# ------------------------------------------------------------------ reviews

def test_review_roundtrip_is_contract_valid_and_persisted(make_client, tmp_path) -> None:
    client = make_client()
    trace_id = client.post("/v1/price", json={"sku": "SKU-4471"}).json()["trace_id"]

    response = client.post(
        f"/v1/decisions/{trace_id}/review",
        json={"choice": "approve", "note": "clamps look right"},
    )
    assert response.status_code == 200
    entry = response.json()
    jsonschema.validate(entry, _ref("price-api.schema.json", "ReviewResponse"))
    assert entry["trace_id"] == trace_id
    assert entry["choice"] == "approve"

    # Persisted across requests (the console's button must not vanish).
    reviews = (tmp_path / "var" / "reviews.jsonl").read_text()
    assert entry["decision_id"] in reviews


def test_review_on_unknown_trace_is_404_with_a_fix(make_client) -> None:
    client = make_client()
    response = client.post(
        "/v1/decisions/no-such/review", json={"choice": "reject"}
    )
    assert response.status_code == 404
    assert "review" in response.json()["error"]
    assert "Fix" not in response.json()["error"] and response.json()["fix"]


def test_review_choice_is_a_closed_enum(make_client) -> None:
    client = make_client()
    trace_id = client.post("/v1/price", json={"sku": "SKU-2"}).json()["trace_id"]
    bad = client.post(
        f"/v1/decisions/{trace_id}/review", json={"choice": "rubber-stamp"}
    )
    assert bad.status_code == 422
    assert "choice" in bad.json()["error"]


def test_review_requires_auth(make_client) -> None:
    client = make_client()
    client.headers.clear()
    response = client.post(
        "/v1/decisions/x/review", json={"choice": "approve"}
    )
    assert response.status_code == 401


# ------------------------------------------------------- console data surface

def test_explain_returns_decision_plus_contributions(make_client) -> None:
    client = make_client()
    response = client.post("/v1/price/explain", json={"sku": "SKU-4471"})
    assert response.status_code == 200
    body = response.json()
    jsonschema.validate(body, _ref("price-api.schema.json", "ExplainResponse"))
    jsonschema.validate(body["decision"], load_schema("decision-record.schema.json"))
    assert body["contributions"], "HITL view needs the why, not just the price"
    features = {c["feature"] for c in body["contributions"]}
    assert "base_cost" in features
    # Sorted by absolute impact: the top entry dominates the rest.
    magnitudes = [abs(c["contribution"]) for c in body["contributions"]]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_decisions_list_is_bounded_and_valid(make_client) -> None:
    client = make_client()
    for i in range(3):
        client.post("/v1/price", json={"sku": f"SKU-{i}"})
    response = client.get("/v1/decisions")
    assert response.status_code == 200
    records = response.json()
    assert len(records) == 3
    for record in records:
        jsonschema.validate(record, load_schema("decision-record.schema.json"))

    assert client.get("/v1/decisions?limit=0").status_code == 422
    assert client.get("/v1/decisions?limit=101").status_code == 422
    assert len(client.get("/v1/decisions?limit=2").json()) == 2


def test_signals_404s_with_crawl_fix_then_serves_state(make_client, tmp_path) -> None:
    client = make_client()
    first = client.get("/v1/signals")
    assert first.status_code == 404
    assert "pricing.intel.run" in first.json()["fix"]

    # Simulate a completed crawl's state file (what the CLI writes).
    state = {
        "ran_at": "2026-11-15T16:00:00+00:00",
        "signals_written": 1, "quarantined": 1, "advisories": 0,
        "signals": [{"signal_type": "competitor_stockout", "confidence": 0.92}],
        "quarantine_records": [{"doc_id": "abc", "reason": "injection_suspect", "detail": "x"}],
    }
    (tmp_path / "var" / "intel_state.json").write_text(json.dumps(state))

    ok = client.get("/v1/signals")
    assert ok.status_code == 200
    body = ok.json()
    assert body["stats"]["signals_written"] == 1
    assert body["quarantines"][0]["reason"] == "injection_suspect"
