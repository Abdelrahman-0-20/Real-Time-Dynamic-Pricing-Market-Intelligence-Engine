"""ADR-002's dependency rule, enforced before import-linter can run.

The hexagonal boundary must hold from line one: pricing.core is pure domain
code (no vendor, no IO, no frameworks), the application layer speaks only
through ports, and neither may import the other side of the hexagon by
accident. A violation here means the guardrails are no longer testable in
isolation.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "pricing" / "core"
APP = ROOT / "pricing" / "app"

VENDOR = {
    # vendor / IO / framework surface — belongs behind ports in adapters
    "numpy",
    "redis",
    "feast",
    "kafka",
    "confluent_kafka",
    "flask",
    "fastapi",
    "sqlalchemy",
    "psycopg",
    "requests",
    "httpx",
    "onnxruntime",
    "xgboost",
    "lightgbm",
    "sklearn",
}
CORE_FORBIDDEN = VENDOR | {"pricing.adapters", "pricing.app"}
APP_FORBIDDEN = VENDOR | {"pricing.adapters"}


def _violations(path: pathlib.Path, forbidden: set[str]) -> list[str]:
    found: list[str] = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
        for mod in modules:
            for bad in forbidden:
                if mod == bad or mod.startswith(bad + "."):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} imports '{mod}'")
    return found


def _scan(directory: pathlib.Path, forbidden: set[str], label: str) -> None:
    assert directory.is_dir(), (
        f"{directory.relative_to(ROOT)} is missing — ADR-002's {label} must exist"
    )
    files = sorted(directory.rglob("*.py"))
    assert files, f"{directory.relative_to(ROOT)} contains no Python files"
    violations = [v for f in files for v in _violations(f, forbidden)]
    assert not violations, (
        f"ADR-002 dependency violation(s) in {label}:\n  "
        + "\n  ".join(violations)
        + "\n  Fix: move the I/O behind a port in pricing.adapters; "
          f"{label} keeps only pure rules."
    )


def test_core_imports_nothing_forbidden() -> None:
    _scan(CORE, CORE_FORBIDDEN, "pricing.core")


def test_app_imports_neither_adapters_nor_vendor() -> None:
    _scan(APP, APP_FORBIDDEN, "pricing.app")


def test_adapters_do_not_reimplement_domain_rules() -> None:
    """Adapters may import the core, but must not smuggle guardrail logic back."""
    adapters = ROOT / "pricing" / "adapters"
    if not adapters.is_dir():
        return
    offenders = [
        f"{p.relative_to(ROOT)}"
        for p in adapters.rglob("*.py")
        if "def apply_guardrails" in p.read_text()
    ]
    assert not offenders, (
        f"guardrail logic found in adapter(s) {offenders} — "
        "rules belong in pricing.core.guardrails (ADR-002)"
    )
