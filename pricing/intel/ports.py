"""Ports for the cold path — pure Protocol definitions, no implementations."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Source:
    id: str
    url: str
    type: str                 # competitor_pricing_page | market_news_article
    category: str
    trust: float              # 0..1, weights contradiction resolution
    tos_reviewed: bool
    enabled: bool = True
    seed_file: str | None = None


@dataclass(frozen=True)
class ParsedContent:
    text: str
    entities: list[tuple[str, str]] = field(default_factory=list)
    # entities: (sku, chunk_text) carved from data-sku blocks; empty for news


@dataclass(frozen=True)
class FetchedPage:
    url: str
    raw: str
    fetched_at: datetime


class Fetcher(Protocol):
    def fetch(self, source: Source) -> FetchedPage:
        ...


class Parser(Protocol):
    name: str

    def parse(self, page: FetchedPage, source: Source) -> ParsedContent:
        ...


class DocumentStore(Protocol):
    def save(self, source: Source, parsed: ParsedContent, fetched_at: datetime) -> str:
        """Persist the document (content-hash deduped); return doc_id."""
        ...

    def search(self, query: str, k: int = 5) -> list[str]:
        """Return doc_ids most similar to `query` (retrieval for context)."""
        ...


class SignalExtractor(Protocol):
    name: str

    def extract(self, parsed: ParsedContent, source: Source) -> list[dict]:
        """Return RAW signal candidates (pre-gate dicts). Must not write."""
        ...


class SignalSink(Protocol):
    def find(self, signal_type: str, subject_key: str) -> list[dict]:
        ...

    def save(self, signal: dict) -> None:
        ...

    def quarantine(self, doc_id: str, reason: str, detail: str) -> None:
        ...

    @property
    def signals(self) -> list[dict]: ...

    @property
    def quarantines(self) -> list[dict]: ...
