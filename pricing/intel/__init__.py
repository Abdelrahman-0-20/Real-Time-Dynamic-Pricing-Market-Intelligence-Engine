"""Cold-path (Market Intelligence) bounded context.

Pipeline: fetch -> parse -> inject-gate -> extract -> schema-gate ->
contradiction-resolve -> signal sink -> feature writer (ADR-001 join).

Dependency rule mirrors ADR-002: this package is orchestration + gates only.
Concrete fetchers/parsers/extractors/stores live in pricing.intel.adapters
and are injected through the ports in pricing.intel.ports.
"""
