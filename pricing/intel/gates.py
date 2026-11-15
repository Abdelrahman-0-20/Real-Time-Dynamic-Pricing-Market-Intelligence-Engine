"""Gates: everything an untrusted document must survive before becoming a signal.

Three gates (ADR-006), all pure functions:

  1. Injection gate   — instruction-like text quarantines the WHOLE document
                        before any extraction runs.
  2. Schema gate      — assembled signals validate against the published
                        MarketSignal contract; confidence must clear 0.7.
                        Closed enums mean page text cannot invent signal types.
  3. Contradiction    — same key, opposite polarity: deterministic weighting
                        (confidence x source trust x recency); an unresolved
                        conflict writes NOTHING and flags advisory.

jsonschema is permitted here: pure validation, no IO — the vendor/IO rule
applies to frameworks, transports, and numeric stacks (ADR-009).
"""
from __future__ import annotations

import math
import re
from datetime import datetime

from jsonschema import Draft202012Validator

MIN_CONFIDENCE = 0.7
CONTRADICTION_WINDOW_HOURS = 48.0
UNRESOLVED_RATIO = 0.8  # scores within 20% of each other => unresolved

# Imperative/instruction-like content aimed at hijacking the pipeline.
_INJECTION_PATTERNS = [
    r"\b(?:ignore|disregard|forget)\s+(?:the\s+)?(?:previous|above|prior|all)\s+(?:instructions?|prompts?|messages?)",
    r"\byou\s+are\s+now\s+(?:a|an|the)\b",
    r"\bsystem\s*prompt\b",
    r"\boutput\s+(?:a|an|the)\s+signal\s+(?:for|showing|of)\b",
    r"\bset\s+(?:the\s+)?price\s+to\b",
    r"\bpretend\s+(?:to\s+be|you\s+are)\b",
    r"\bdo\s+not\s+(?:follow|obey)\s+(?:the\s+)?(?:rules|instructions)\b",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.IGNORECASE)


def looks_like_injection(text: str) -> bool:
    """True when the document contains instruction-like language (hostile)."""
    return bool(_INJECTION_RE.search(text))


def load_signal_validator(schema_path) -> Draft202012Validator:
    import json
    import pathlib

    return Draft202012Validator(json.loads(pathlib.Path(schema_path).read_text()))


def gate_signal(candidate: dict, validator: Draft202012Validator) -> tuple[bool, str]:
    """Schema + confidence gate. Returns (accepted, reason_if_rejected)."""
    if float(candidate.get("confidence", 0)) < MIN_CONFIDENCE:
        return False, f"confidence {candidate.get('confidence')} below {MIN_CONFIDENCE}"
    errors = sorted(validator.iter_errors(candidate), key=lambda e: e.path)
    if errors:
        first = errors[0]
        location = ".".join(str(p) for p in first.path) or "<root>"
        return False, f"schema: {location}: {first.message[:120]}"
    return True, ""


def subject_key(signal: dict) -> str:
    subject = signal.get("subject", {})
    return str(subject.get("sku") or subject.get("category") or subject.get("competitor"))


def _score(signal: dict, now: datetime) -> float:
    observed = datetime.fromisoformat(signal["observed_at"])
    age_hours = max(0.0, (now - observed).total_seconds() / 3600.0)
    recency = math.exp(-age_hours / 24.0)
    return float(signal["confidence"]) * float(signal["source_trust"]) * recency


def resolve_contradiction(
    existing: list[dict], incoming: dict, now: datetime
) -> tuple[str, str]:
    """Decide an incoming signal against existing ones of the same key.

    Returns (decision, reason) where decision is one of:
      "accept"        — no conflict, or incoming strictly stronger
      "replace"       — incoming wins a real contradiction
      "reject"        — keep existing (incoming weaker)
      "advisory"      — unresolved contradiction: write NOTHING, flag review
    """
    opposite = {"positive": "negative", "negative": "positive"}
    conflicts = [
        e for e in existing
        if e["signal_type"] == incoming["signal_type"]
        and opposite.get(e.get("polarity")) == incoming.get("polarity")
    ]
    if not conflicts:
        return "accept", "no conflict"

    incoming_score = _score(incoming, now)
    best_existing = max(conflicts, key=lambda e: _score(e, now))
    existing_score = _score(best_existing, now)

    if existing_score <= 0 or incoming_score / existing_score > 1 / UNRESOLVED_RATIO:
        return "replace", f"incoming {incoming_score:.3f} beats {existing_score:.3f}"
    if incoming_score / existing_score >= UNRESOLVED_RATIO:
        return "advisory", (
            f"unresolved contradiction: {incoming_score:.3f} vs "
            f"{existing_score:.3f} within {UNRESOLVED_RATIO} ratio"
        )
    return "reject", f"incoming {incoming_score:.3f} loses to {existing_score:.3f}"
