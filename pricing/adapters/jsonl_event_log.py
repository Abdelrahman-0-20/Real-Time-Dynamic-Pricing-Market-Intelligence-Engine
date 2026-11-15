"""JSONL event log — the local replayable stand-in for a Kafka topic (ADR-010).

Schema-validated on write: garbage never enters the stream. Torn last lines
(crash mid-append) are tolerated on read. Swap-on condition: a Kafka broker
exists -> KafkaEventSource (same decode+validate contract) takes over.
"""
from __future__ import annotations

import json
import pathlib

from jsonschema import Draft202012Validator

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = _REPO_ROOT / "docs" / "contracts" / "telemetry-event.schema.json"


class JsonlEventLog:
    def __init__(self, path: str | pathlib.Path,
                 schema_path: str | pathlib.Path = DEFAULT_SCHEMA) -> None:
        self._path = pathlib.Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._validator = Draft202012Validator(
            json.loads(pathlib.Path(schema_path).read_text())
        )

    @property
    def path(self) -> pathlib.Path:
        return self._path

    def append(self, event: dict) -> None:
        errors = sorted(self._validator.iter_errors(event), key=lambda e: e.path)
        if errors:
            first = errors[0]
            location = ".".join(str(p) for p in first.path) or "<root>"
            raise ValueError(
                f"telemetry event rejected by schema: {location}: "
                f"{first.message[:140]}. Fix: correct the producer payload "
                "(docs/contracts/telemetry-event.schema.json is the contract)."
            )
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")

    def read_all(self) -> list[dict]:
        if not self._path.exists():
            return []
        events: list[dict] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn write from a crash — skip, never crash the reader
            if isinstance(payload, dict):
                events.append(payload)
        return events
