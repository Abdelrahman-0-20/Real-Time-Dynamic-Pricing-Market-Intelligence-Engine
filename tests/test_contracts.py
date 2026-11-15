"""Contract gate: every fixture must prove its schema boundary.

Valid fixtures must pass; invalid/adversarial fixtures must fail — and for the
named ones, fail for the *expected* reason (an injection fixture that fails on
a typo instead of the closed enum would be a silent gate regression).
"""
from __future__ import annotations

import json
import pathlib

import pytest
from jsonschema import Draft202012Validator, ValidationError

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = ROOT / "tests" / "fixtures"

SCHEMA_BY_PREFIX = {
    "telemetry-event": "telemetry-event.schema.json",
    "market-document": "market-document.schema.json",
    "market-signal": "market-signal.schema.json",
    "feature-record": "feature-record.schema.json",
    "decision-record": "decision-record.schema.json",
}

# Adversarial/negative fixtures must fail for these specific reasons.
EXPECTED_KEYWORD = {
    "market-signal.injection-open-vocabulary.json": "enum",
    "market-signal.low-confidence.json": "minimum",
    "decision-record.guardrail-bypass.json": "enum",
    "decision-record.unknown-arm.json": "enum",
    "decision-record.signal-trigger-no-evidence.json": "required",
    "decision-record.signal-trigger-low-confidence.json": "minimum",
    "feature-record.missing-ttl.json": "required",
    "telemetry-event.missing-event-time.json": "required",
    "telemetry-event.negative-quantity.json": "minimum",
    "market-document.bad-content-hash.json": "pattern",
    "feature-record.bad-feature-name.json": "pattern",
}


def _schema_for(fixture: pathlib.Path) -> dict:
    for prefix, name in SCHEMA_BY_PREFIX.items():
        if fixture.name.startswith(prefix):
            return json.loads((CONTRACTS / name).read_text())
    raise AssertionError(
        f"no schema mapped to fixture '{fixture.name}' — "
        "add its prefix to SCHEMA_BY_PREFIX in tests/test_contracts.py"
    )


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


VALID = sorted((FIXTURES / "valid").glob("*.json"))
INVALID = sorted((FIXTURES / "invalid").glob("*.json"))


def test_fixture_sets_are_present() -> None:
    assert len(VALID) >= 5, "valid fixture set shrank below coverage floor"
    assert len(INVALID) >= 8, "invalid fixture set shrank below coverage floor"
    missing = set(EXPECTED_KEYWORD) - {p.name for p in INVALID}
    assert not missing, f"expected-failure fixtures missing: {sorted(missing)}"


def test_schemas_are_well_formed() -> None:
    for schema_path in sorted(CONTRACTS.glob("*.schema.json")):
        Draft202012Validator.check_schema(json.loads(schema_path.read_text()))


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name)
def test_valid_fixtures_pass_the_gate(path: pathlib.Path) -> None:
    Draft202012Validator(_schema_for(path)).validate(_load(path))


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.name)
def test_invalid_fixtures_fail_the_gate(path: pathlib.Path) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(_schema_for(path)).validate(_load(path))


@pytest.mark.parametrize("name", sorted(EXPECTED_KEYWORD), ids=str)
def test_failures_fail_for_the_right_reason(name: str) -> None:
    path = FIXTURES / "invalid" / name
    with pytest.raises(ValidationError) as exc:
        Draft202012Validator(_schema_for(path)).validate(_load(path))
    assert exc.value.validator == EXPECTED_KEYWORD[name], (
        f"{name}: expected rejection by '{EXPECTED_KEYWORD[name]}', "
        f"got '{exc.value.validator}' ({exc.value.message[:80]})"
    )
