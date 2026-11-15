"""Lexicon extractor: deterministic rules over the closed signal vocabulary.

This is BOTH the step-4 default and tier-3 of the ADR-006 fallback chain
(the degraded extractor that runs when the LLM is down) — production code
with production tests, not a placeholder.

Structural injection safety: page text can only MATCH patterns; it can never
define a new signal_type, confidence, or polarity. Everything else is
assembled by the pipeline with pipeline-owned provenance.
"""
from __future__ import annotations

import re

from pricing.intel.ports import ParsedContent, Source

NAME = "lexicon-v1"

# (signal_type, compiled pattern, confidence, magnitude group or None)
_RULES: list[tuple[str, re.Pattern, float, str | None]] = [
    ("competitor_stockout",
     re.compile(r"\b(?:out of stock|sold out|currently unavailable|nafid al-makhzun)\b|نفد\s+المخزون", re.I),
     0.92, None),
    ("competitor_price_change",
     re.compile(r"\b(?:prices?\s+(?:rose|fell|raised|cut|dropped|hiked)|(?:rose|fell|raised|cut|dropped|hiked)\s+(?:the\s+)?prices?)\s*(?:by\s*)?(\d+(?:\.\d+)?)?\s*(%|percent|dollars)?", re.I),
     0.80, "1"),
    ("seasonal_promotion",
     re.compile(r"\b(\d+(?:\.\d+)?)\s*%\s*off\b|\bdiscount of\s+(\d+(?:\.\d+)?)\s*%", re.I),
     0.88, None),  # magnitude parsed separately below
    ("demand_surge",
     re.compile(r"\b(?:selling out fast|high demand|limit\s+\d+\s+per)\b", re.I),
     0.85, None),
    # Deliberately WEAK rule: a bare "sale" mention must NOT become a signal.
    # Its confidence sits below the gate threshold on purpose (gate test).
    ("seasonal_promotion",
     re.compile(r"\b(?:sale|promo|promotion|عروض|تخفيضات)\b", re.I),
     0.62, None),
]

_PROMO_MAGNITUDE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*off|\bdiscount of\s+(\d+(?:\.\d+)?)\s*%", re.I)


class LexiconExtractor:
    name = NAME

    def extract(self, parsed: ParsedContent, source: Source) -> list[dict]:
        scopes: list[tuple[dict, str]] = []
        if parsed.entities:
            scopes = [({"sku": sku}, chunk) for sku, chunk in parsed.entities]
        else:
            scopes = [({"category": source.category}, parsed.text)]

        results: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for subject, text in scopes:
            for signal_type, pattern, confidence, _ in _RULES:
                if not pattern.search(text):
                    continue
                key = (signal_type, str(subject))
                if key in seen:
                    # strongest rule for this subject wins (0.88 promo beats 0.62)
                    continue
                seen.add(key)
                magnitude = self._magnitude(signal_type, text)
                results.append({
                    "signal_type": signal_type,
                    "confidence": confidence,
                    "magnitude": magnitude,
                    "_subject": subject,
                })
        # `seen` keeps the FIRST matching rule per (type, subject); rules are
        # ordered strongest-first, so a bare "sale" (0.62) never shadows the
        # concrete "25% off" (0.88), and the weak rule alone trips the gate.
        return results

    @staticmethod
    def _magnitude(signal_type: str, text: str) -> float | None:
        if signal_type != "seasonal_promotion":
            return None
        match = _PROMO_MAGNITUDE.search(text)
        if not match:
            return None
        return float(match.group(1) or match.group(2))
