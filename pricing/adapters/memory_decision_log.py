"""DecisionLog adapter (step 3: in-memory).

Production replaces this with the append-only decision store; the idempotency
contract stays the same: a replay of an identical `fingerprint` must not
create a second effective decision.
"""
from __future__ import annotations


class InMemoryDecisionLog:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record: dict) -> None:
        self.records.append(record)

    def by_sku(self, sku: str) -> list[dict]:
        return [r for r in self.records if r["sku"] == sku]
