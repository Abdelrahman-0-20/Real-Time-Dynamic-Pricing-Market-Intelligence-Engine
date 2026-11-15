"""Kafka event source — real `confluent-kafka` client, broker-gated tests.

ADR-010: decoding + schema validation (the part that must be correct today)
runs and is unit-tested without a broker. The consumer connects lazily on
first poll, so importing/constructing never touches the network; the
integration test activates only when KAFKA_BROKER is set.

Flip-on: export KAFKA_BROKER=localhost:9092 && pytest tests/test_kafka_adapter.py
"""
from __future__ import annotations

import json
import os

from jsonschema import Draft202012Validator


class EventDecodeError(ValueError):
    """A message that violates the telemetry contract."""


class KafkaEventSource:
    def __init__(
        self,
        bootstrap: str | None = None,
        group: str = "pricing-ingest",
        topics: list[str] | None = None,
        schema_path: str | None = None,
        config: dict | None = None,
    ) -> None:
        self._bootstrap = bootstrap or os.environ.get("KAFKA_BROKER", "localhost:9092")
        self._group = group
        self._topics = topics or ["telemetry.raw"]
        self._consumer = None  # lazy: construction must not open sockets
        self._extra = config or {}
        self._unknown_topic_polls = 0
        self._validator = self._load_validator(schema_path)

    # Topic auto-creation/startup is async: tolerate a bounded window of
    # UNKNOWN_TOPIC, then fail LOUDLY — a consumer that reports "healthy but
    # empty" against a missing topic is a silent failure, the worst kind.
    _MAX_UNKNOWN_TOPIC_POLLS = 50

    @staticmethod
    def _load_validator(schema_path: str | None) -> Draft202012Validator:
        import pathlib

        if schema_path is None:
            schema_path = (
                pathlib.Path(__file__).resolve().parents[2]
                / "docs" / "contracts" / "telemetry-event.schema.json"
            )
        return Draft202012Validator(json.loads(pathlib.Path(schema_path).read_text()))

    # -- decoding: the broker-independent contract ---------------------------

    def decode(self, raw: bytes | str) -> dict:
        """Bytes -> validated event. Contract violations raise EventDecodeError."""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EventDecodeError(
                f"message is not JSON ({exc}). Fix: check the producer's "
                "serialization — docs/contracts/telemetry-event.schema.json "
                "is the contract."
            ) from None
        errors = sorted(self._validator.iter_errors(payload), key=lambda e: e.path)
        if errors:
            first = errors[0]
            location = ".".join(str(p) for p in first.path) or "<root>"
            raise EventDecodeError(
                f"schema violation at {location}: {first.message[:140]}. "
                "Fix: correct the producer payload or dead-letter this message."
            )
        return payload

    # -- broker I/O: lazy, gated ---------------------------------------------

    def _ensure_consumer(self):
        if self._consumer is None:
            try:
                from confluent_kafka import Consumer
            except ImportError as exc:
                raise RuntimeError(
                    "confluent-kafka is not installed. "
                    "Fix: pip install confluent-kafka"
                ) from None
            self._consumer = Consumer({
                "bootstrap.servers": self._bootstrap,
                "group.id": self._group,
                "auto.offset.reset": "earliest",
                **self._extra,
            })
            self._consumer.subscribe(self._topics)
        return self._consumer

    def poll(self, timeout: float = 1.0) -> dict | None:
        """Next validated event, or None. Raises EventDecodeError on bad data."""
        from confluent_kafka import KafkaError

        consumer = self._ensure_consumer()
        message = consumer.poll(timeout)
        if message is None:
            return None
        if message.error():
            code = message.error().code()
            if code == KafkaError._PARTITION_EOF:
                return None
            if code == KafkaError.UNKNOWN_TOPIC_OR_PARTITION:
                self._unknown_topic_polls += 1
                if self._unknown_topic_polls <= self._MAX_UNKNOWN_TOPIC_POLLS:
                    return None  # bounded startup window (topic auto-creation)
                raise ConnectionError(
                    f"topic(s) {self._topics} still unavailable after "
                    f"{self._unknown_topic_polls} polls on {self._bootstrap}. "
                    "Fix: create the topic (auto-create may be disabled) or "
                    "check the broker address."
                )
            raise ConnectionError(f"kafka error: {message.error()}")
        self._unknown_topic_polls = 0
        return self.decode(message.value())

    def close(self) -> None:
        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None
