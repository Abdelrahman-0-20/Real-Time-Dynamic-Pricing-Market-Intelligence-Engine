"""Cold-path orchestration: fetch -> parse -> gates -> extract -> write.

Order matters and is deliberate:
  1. ToS review gate + circuit breaker BEFORE any network/fixture access
  2. injection gate on raw text BEFORE extraction (hostile content never
     reaches the extractor as anything but data)
  3. extraction (side-effect free)
  4. schema gate per candidate, assembled with pipeline-owned provenance
  5. contradiction resolution against existing signals
  6. sink + feature writer (the ADR-001 join into the feature store)

Every source run is isolated: one source failing cannot stop the others.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from pricing.intel import gates
from pricing.intel.features import build_candidate, signal_to_feature
from pricing.intel.ports import (
    DocumentStore,
    Fetcher,
    ParsedContent,
    Parser,
    SignalExtractor,
    SignalSink,
    Source,
)
from pricing.intel.sources import CircuitBreaker, SourceRegistry


class CrawlPipeline:
    def __init__(
        self,
        *,
        registry: SourceRegistry,
        fetcher: Fetcher,
        parser: Parser,
        documents: DocumentStore,
        extractor: SignalExtractor,
        sink: SignalSink,
        validator,
        clock,
        feature_store=None,          # FeatureStore port (hot-path store) or None
        breaker_clock=None,          # seconds-float clock for breakers
    ) -> None:
        self._registry = registry
        self._fetcher = fetcher
        self._parser = parser
        self._documents = documents
        self._extractor = extractor
        self._sink = sink
        self._validator = validator
        self._clock = clock
        self._feature_store = feature_store
        self._breaker_clock = breaker_clock or (lambda: clock.now().timestamp())
        self._breakers: dict[str, CircuitBreaker] = {}

    @property
    def has_feature_store(self) -> bool:
        """False for standalone CLI runs; the crawl worker wires the store."""
        return self._feature_store is not None

    def breaker_for(self, source_id: str) -> CircuitBreaker:
        if source_id not in self._breakers:
            self._breakers[source_id] = CircuitBreaker(self._breaker_clock)
        return self._breakers[source_id]

    def run(self, source_ids: list[str] | None = None) -> dict:
        selected = (
            [self._registry.get(sid) for sid in source_ids]
            if source_ids else self._registry.all()
        )
        stats: dict = {"sources": {}, "signals_written": 0,
                       "features_written": 0, "quarantined": 0, "advisories": 0}
        for source in selected:
            stats["sources"][source.id] = self._run_one(source, stats)
        return stats

    def _run_one(self, source: Source, stats: dict) -> dict:
        if not source.enabled:
            return {"status": "disabled"}
        if not source.tos_reviewed:
            # Refusal, not silence: unreviewed sources never fetch.
            return {"status": "refused_tos_not_reviewed"}
        breaker = self.breaker_for(source.id)
        if not breaker.allow():
            return {"status": "breaker_open", "state": breaker.state}

        trace_id = str(uuid.uuid4())
        try:
            page = self._fetcher.fetch(source)
            parsed = self._parser.parse(page, source)
        except Exception as exc:
            breaker.record_failure()
            return {"status": "fetch_or_parse_error", "error": str(exc)[:200]}

        doc_id = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()[:32]

        # Gate 1: injection — hostile text quarantines the whole document.
        if gates.looks_like_injection(parsed.text):
            self._sink.quarantine(doc_id, "injection_suspect",
                                  "instruction-like language in source text")
            stats["quarantined"] += 1
            breaker.record_success()  # fetch worked; content is the problem
            return {"status": "quarantined", "reason": "injection_suspect"}

        self._documents.save(source, parsed, page.fetched_at)
        candidates = self._extractor.extract(parsed, source)

        written = 0
        advisories = 0
        for candidate in candidates:
            subject = candidate.pop("_subject", {"category": source.category})
            signal = build_candidate(
                signal_type=candidate["signal_type"],
                source=source,
                doc_id=doc_id,
                extracted_by=self._extractor.name,
                model=self._extractor.name,
                trace_id=trace_id,
                observed_at=page.fetched_at,
                confidence=float(candidate["confidence"]),
                subject=subject,
                magnitude=candidate.get("magnitude"),
            )
            accepted, reason = gates.gate_signal(signal, self._validator)
            if not accepted:
                self._sink.quarantine(doc_id, "schema_or_confidence_gate", reason)
                stats["quarantined"] += 1
                continue

            key = gates.subject_key(signal)
            decision, why = gates.resolve_contradiction(
                self._sink.find(signal["signal_type"], key), signal,
                self._clock.now(),
            )
            if decision == "advisory":
                self._sink.quarantine(doc_id, "unresolved_contradiction", why)
                advisories += 1
                continue
            if decision == "reject":
                continue

            self._sink.save(signal)
            written += 1
            stats["signals_written"] += 1
            if self._feature_store is not None:
                mapped = signal_to_feature(signal, self._clock.now())
                if mapped is not None:
                    entity_key, name, value, ttl_ms = mapped
                    self._feature_store.put(
                        entity_key, name, value,
                        event_time=self._clock.now(), ttl_ms=ttl_ms,
                    )
                    stats["features_written"] += 1

        breaker.record_success()
        stats["advisories"] += advisories
        return {"status": "ok", "signals": written, "advisories": advisories}
