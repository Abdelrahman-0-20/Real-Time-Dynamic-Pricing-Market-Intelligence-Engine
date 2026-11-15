"""SignalSink adapter: accepted signals + quarantine records, in memory.

The production version writes to the signal store / queue; the contract is
identical: save() only ever receives schema-valid signals (the gate ran
upstream), quarantine() records WHY nothing was written.
"""
from __future__ import annotations

from pricing.intel.gates import subject_key


class MemorySignalSink:
    def __init__(self) -> None:
        self._signals: list[dict] = []
        self._quarantines: list[dict] = []

    @property
    def signals(self) -> list[dict]:
        return list(self._signals)

    @property
    def quarantines(self) -> list[dict]:
        return list(self._quarantines)

    def find(self, signal_type: str, key: str) -> list[dict]:
        return [
            s for s in self._signals
            if s["signal_type"] == signal_type and subject_key(s) == key
        ]

    def save(self, signal: dict) -> None:
        self._signals.append(signal)

    def quarantine(self, doc_id: str, reason: str, detail: str) -> None:
        self._quarantines.append(
            {"doc_id": doc_id, "reason": reason, "detail": detail}
        )
