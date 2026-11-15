"""Append-only JSONL decision log — decisions that survive across CLI runs.

The in-memory log was slice-only: a CLI process that dies with its state is
useless for `pricingctl trace`. Same DecisionLog contract, file-backed:
one JSON object per line, idempotent by `fingerprint` (ADR replay key).
"""
from __future__ import annotations

import json
import pathlib


class JsonlDecisionLog:
    def __init__(self, path: str | pathlib.Path) -> None:
        self._path = pathlib.Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> pathlib.Path:
        return self._path

    def append(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":"), allow_nan=False)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read_all(self) -> list[dict]:
        if not self._path.exists():
            return []
        records: list[dict] = []
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn last line (crash mid-write) must not kill reads.
                continue
        return records

    def find_trace(self, trace_id: str) -> dict | None:
        for record in reversed(self.read_all()):
            if record.get("trace_id") == trace_id:
                return record
        return None
