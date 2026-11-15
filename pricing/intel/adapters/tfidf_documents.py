"""DocumentStore adapter: TF-IDF + cosine over stored documents.

Stands in for the production Vector DB (ADR-009) behind the same port:
content-hash dedup on write, similarity search for retrieval. Real embeddings
swap in without touching the pipeline.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from sklearn.feature_extraction.text import TfidfVectorizer

from pricing.intel.ports import ParsedContent, Source


class TfidfDocumentStore:
    def __init__(self) -> None:
        self._docs: dict[str, str] = {}          # doc_id -> text
        self._sources: dict[str, str] = {}       # doc_id -> source id
        self._vectorizer: TfidfVectorizer | None = None
        self._matrix = None
        self._order: list[str] = []

    @property
    def doc_ids(self) -> list[str]:
        return list(self._docs)

    def save(self, source: Source, parsed: ParsedContent, fetched_at: datetime) -> str:
        doc_id = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()[:32]
        if doc_id in self._docs:
            return doc_id  # content-hash dedup: identical content extracted once
        self._docs[doc_id] = parsed.text
        self._sources[doc_id] = source.id
        self._rebuild()
        return doc_id

    def _rebuild(self) -> None:
        self._order = list(self._docs)
        self._vectorizer = TfidfVectorizer(stop_words="english", max_features=5000)
        self._matrix = self._vectorizer.fit_transform(
            [self._docs[d] for d in self._order]
        )

    def search(self, query: str, k: int = 5) -> list[str]:
        if not self._docs or self._vectorizer is None:
            return []
        query_vec = self._vectorizer.transform([query])
        scores = (self._matrix @ query_vec.T).toarray().ravel()
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [self._order[i] for i in ranked[:k] if scores[i] > 0]
