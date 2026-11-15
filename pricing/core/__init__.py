"""Domain core (ADR-002): pure business rules.

Import rule (enforced by tests/test_dependency_direction.py and .importlinter):
no vendor, IO, framework, or messaging imports — ever. All I/O lives behind
ports implemented in pricing.adapters.
"""
