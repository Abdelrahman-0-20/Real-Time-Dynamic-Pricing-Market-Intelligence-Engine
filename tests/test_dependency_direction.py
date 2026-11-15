"""ADR-002's dependency rule, enforced before import-linter can run.

The hexagonal boundary must hold from line one: pricing.core is pure domain
code (no vendor, no IO, no frameworks), and it must never import adapters.
A violation here means the guardrails are no longer testable in isolation.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "pricing" / "core"

FORBIDDEN = {
    # the other side of the hexagon
    "pricing.adapters",
    # vendor / IO / framework surface — belongs behind ports in adapters
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
}


def _violations(path: pathlib.Path) -> list[str]:
    found: list[str] = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
        for mod in modules:
            for bad in FORBIDDEN:
                if mod == bad or mod.startswith(bad + "."):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} imports '{mod}'")
    return found


def test_core_package_exists() -> None:
    assert CORE.is_dir(), (
        "pricing/core is missing — ADR-002's domain core must exist; "
        "create it with pure business rules only"
    )


def test_core_imports_nothing_forbidden() -> None:
    files = sorted(CORE.rglob("*.py"))
    assert files, "pricing/core contains no Python files"
    violations = [v for f in files for v in _violations(f)]
    assert not violations, (
        "ADR-002 dependency violation(s) in pricing.core:\n  "
        + "\n  ".join(violations)
        + "\n  Fix: move the I/O behind a port in pricing.adapters; "
          "core keeps only the pure rule."
    )


def test_adapters_do_not_exist_as_a_surrogate_core() -> None:
    """Adapters may import the core, but must not reimplement guardrail rules."""
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
