"""pricingctl contract tests: exit codes, JSON mode, fix-hinting errors.

Exit codes are an API (docs/pricingctl.md): 0 ok · 2 usage · 3 degraded ·
4 not found · 5 unreachable. These tests are the regression guard for that
contract — a script somewhere depends on them.
"""
from __future__ import annotations

import json

import jsonschema
import pytest

from pricing.ctl.main import main
from tests.helpers import load_schema


@pytest.fixture()
def ctl_env(tmp_path, monkeypatch, tiny_artifacts):
    monkeypatch.setenv("PRICINGCTL_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("PRICINGCTL_ARTIFACTS", str(tiny_artifacts))
    return tmp_path


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# ------------------------------------------------------------------ status

def test_status_json_reports_all_sections(capsys, ctl_env) -> None:
    code = main(["status", "--json"])
    payload = _json_out(capsys)
    assert code in (0, 3)
    for key in ("model", "features", "latency", "intel", "sources", "warnings"):
        assert key in payload
    assert payload["features"]["count"] == 15
    assert payload["sources"]["refused"] == 1  # ToS-unreviewed source visible


def test_status_missing_model_exits_5_with_fix(capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PRICINGCTL_ARTIFACTS", str(tmp_path / "no-artifacts"))
    monkeypatch.setenv("PRICINGCTL_VAR", str(tmp_path / "var"))
    code = main(["status"])
    err = capsys.readouterr().err
    assert code == 5
    assert "Fix:" in err and "pricing.training.train" in err


def test_status_marks_degraded_after_quarantining_crawl(capsys, ctl_env) -> None:
    main(["crawl", "run"])  # seeds contain a below-threshold candidate -> quarantine
    capsys.readouterr()
    code = main(["status"])
    out = capsys.readouterr().out
    assert code == 3, "quarantined intel must surface as degraded"
    assert "DEGRADED" in out


# -------------------------------------------------------------------- price

def test_price_get_json_is_schema_valid_and_traceable(capsys, ctl_env) -> None:
    code = main(["price", "get", "--sku", "SKU-4471", "--json"])
    payload = _json_out(capsys)
    assert code == 0
    record = {k: v for k, v in payload.items() if not k.startswith("_")}
    jsonschema.validate(instance=record, schema=load_schema("decision-record.schema.json"))
    assert record["served_price"] > 0

    capsys.readouterr()
    trace_code = main(["trace", record["trace_id"], "--json"])
    traced = _json_out(capsys)
    assert trace_code == 0
    assert traced["fingerprint"] == record["fingerprint"]


def test_price_explain_shows_contributions(capsys, ctl_env) -> None:
    code = main(["price", "explain", "--sku", "SKU-4471", "--json"])
    payload = _json_out(capsys)
    assert code == 0
    assert payload["contributions"], "explain must show per-feature contributions"
    top = payload["contributions"][0]
    assert {"feature", "contribution"} <= set(top)


def test_price_simulate_clamps_and_logs_nothing(capsys, ctl_env) -> None:
    code = main(["price", "simulate", "--sku", "SKU-4471", "--price", "99999", "--json"])
    payload = _json_out(capsys)
    assert code == 0
    assert payload["served"] < 99999
    assert payload["guardrail_actions"], "extreme price must be clamped"
    # Simulation must not write a decision.
    assert not (ctl_env / "var" / "decisions.jsonl").exists()


def test_price_simulate_rejects_nonpositive_price(capsys, ctl_env) -> None:
    # argparse accepts "-5" as a float value; OUR validator must reject it
    # with exit 2 (usage) and a fix, not crash or clamp silently.
    code = main(["price", "simulate", "--sku", "SKU-1", "--price", "-5"])
    assert code == 2
    assert "Fix:" in capsys.readouterr().err


def test_unknown_market_uses_strict_pack_and_degrades(capsys, ctl_env) -> None:
    code = main(["price", "get", "--sku", "SKU-1", "--market", "ZZ", "--json"])
    payload = _json_out(capsys)
    assert code == 3, "pack fallback must flag the decision degraded (ADR-008)"
    assert payload["degraded"] is True
    assert payload["policy_pack"]["status"] == "derived"


# ------------------------------------------------------------------- trace

def test_unknown_trace_exits_4_with_fix(capsys, ctl_env) -> None:
    code = main(["trace", "no-such-trace"])
    err = capsys.readouterr().err
    assert code == 4
    assert "Fix:" in err


# ------------------------------------------------------------------- crawl

def test_crawl_dry_run_fetches_nothing(capsys, ctl_env) -> None:
    code = main(["crawl", "run", "--dry-run", "--json"])
    payload = _json_out(capsys)
    assert code == 0
    statuses = {p["source"]: p["status"] for p in payload["plan"]}
    assert statuses["shady-deals-aggregator"] == "refused_tos_not_reviewed"
    assert statuses["comp-alpha-pricing"] == "would_fetch"
    assert not (ctl_env / "var" / "intel_state.json").exists()


def test_crawl_run_persists_state_for_later_commands(capsys, ctl_env) -> None:
    code = main(["crawl", "run", "--json"])
    payload = _json_out(capsys)
    assert code == 3  # seed data contains a gated candidate
    assert payload["signals_written"] >= 4

    capsys.readouterr()
    assert main(["crawl", "status"]) == 3
    capsys.readouterr()
    assert main(["signals", "list"]) == 0
    capsys.readouterr()
    assert main(["signals", "list", "--quarantined"]) == 0


def test_crawl_status_without_state_exits_3_with_fix(capsys, ctl_env) -> None:
    code = main(["crawl", "status"])
    assert code == 3
    assert "Fix:" in capsys.readouterr().err


# ------------------------------------------------- model / eval / bench / config

def test_model_list_shows_metrics(capsys, ctl_env) -> None:
    code = main(["model", "list", "--json"])
    payload = _json_out(capsys)
    assert code == 0
    assert "mean_regret" in payload and "model_input_version" in payload


def test_eval_run_passes_the_gate(capsys, ctl_env) -> None:
    code = main(["eval", "run", "--json"])
    payload = _json_out(capsys)
    assert code == 0, payload.get("reasons")
    assert payload["passed"] is True
    assert payload["f1"] >= payload["baseline"]["f1"]


def test_bench_persists_and_status_reads_it(capsys, ctl_env) -> None:
    code = main(["bench", "--n", "200"])
    out = capsys.readouterr().out
    assert code == 0
    assert "gate: PASS" in out

    capsys.readouterr()
    main(["status", "--json"])
    payload = _json_out(capsys)
    assert payload["latency"] is not None
    assert payload["latency"]["p99_ms"] < 50


def test_config_list_json(capsys, ctl_env) -> None:
    code = main(["config", "list", "--json"])
    payload = _json_out(capsys)
    assert code == 0
    assert len(payload["sources"]) == 4
    assert payload["guardrail_packs"][0]["status"] == "placeholder"
    assert payload["feature_dictionary"]["model_input_version"] == "fv-2026.11"


# ------------------------------------------------------ interface contract

def test_help_and_version_are_available() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


def test_unknown_command_is_usage_error_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["definitely-not-a-command"])
    assert exc.value.code == 2


def test_no_arguments_prints_help_and_exits_2(capsys) -> None:
    code = main([])
    assert code == 2
    assert "examples" in capsys.readouterr().out
