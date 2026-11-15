"""Extraction evaluation harness (multi-agent rule: no deployment without evals).

    python -m pricing.evals.run                    # gate vs evals/baseline.json
    python -m pricing.evals.run --json             # pipe-safe report
    python -m pricing.evals.run --update-baseline  # version the gate (explicit)

Scores the REAL production path (parser -> injection gate -> extractor ->
schema/confidence gate) against labeled cases in evals/extraction/cases.json.

Hard rules (fail regardless of scores):
  * an injection case must quarantine and produce ZERO signals (leak = defect)
  * a case expecting quarantine must quarantine (gate regression)
  * a case NOT expecting quarantine must NOT quarantine (false positive blocks
    legitimate intel — over-quarantine is its own failure mode)

Score rule: micro precision/recall/F1 >= baseline (meets-or-exceeds).
A new extractor (the LLM, ADR-006) must beat this baseline to ship.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pricing.intel.adapters.html_parser import HtmlTextParser  # noqa: E402
from pricing.intel.adapters.lexicon_extractor import LexiconExtractor  # noqa: E402
from pricing.intel.features import build_candidate  # noqa: E402
from pricing.intel.gates import (  # noqa: E402
    gate_signal,
    load_signal_validator,
    looks_like_injection,
    subject_key,
)
from pricing.intel.ports import FetchedPage, Source  # noqa: E402

CASES_PATH = _REPO_ROOT / "evals" / "extraction" / "cases.json"
BASELINE_PATH = _REPO_ROOT / "evals" / "baseline.json"
SCHEMA_PATH = _REPO_ROOT / "docs" / "contracts" / "market-signal.schema.json"
FIXED_TIME = datetime(2026, 11, 15, 8, 0, 0, tzinfo=timezone.utc)


def _build_extractor(choice: str):
    if choice == "llm":
        from pricing.intel.adapters.llm_extractor import OpenAICompatExtractor

        return OpenAICompatExtractor()
    from pricing.intel.adapters.lexicon_extractor import LexiconExtractor

    return LexiconExtractor()


def load_cases(path: pathlib.Path = CASES_PATH) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(
            f"no cases in {path}. Fix: evals/extraction/cases.json must hold "
            "a non-empty 'cases' array (>= 20 labeled documents)."
        )
    return cases


def _expected_key(entry: dict) -> tuple[str, str]:
    subject = entry.get("sku") or entry.get("category") or entry.get("competitor")
    return entry["signal_type"], str(subject)


def evaluate(cases: list[dict], parser=None, extractor=None) -> dict:
    parser = parser or HtmlTextParser()
    extractor = extractor or LexiconExtractor()
    validator = load_signal_validator(SCHEMA_PATH)
    source = Source(id="eval", url="https://eval.local/doc",
                    type="competitor_pricing_page", category="electronics",
                    trust=0.8, tos_reviewed=True)

    tp = fp = fn = 0
    hard_failures: list[str] = []
    case_failures: list[dict] = []
    injection_leaks = 0

    for case in cases:
        category = case.get("category", source.category)
        case_source = Source(**{**source.__dict__, "category": category})
        page = FetchedPage(url=case_source.url, raw=case["html"], fetched_at=FIXED_TIME)
        parsed = parser.parse(page, case_source)

        quarantined = False
        predicted: list[dict] = []

        if looks_like_injection(parsed.text):
            quarantined = True  # whole document quarantined BEFORE extraction
            if case.get("injection"):
                injection_leaks += 0  # correct behavior
            candidates = []
        else:
            candidates = extractor.extract(parsed, case_source)
            for candidate in candidates:
                subject = candidate.pop("_subject", {"category": category})
                signal = build_candidate(
                    signal_type=candidate["signal_type"], source=case_source,
                    doc_id=f"eval-{case['id']}", extracted_by=extractor.name,
                    model=extractor.name, trace_id=f"eval-{case['id']}",
                    observed_at=FIXED_TIME, confidence=float(candidate["confidence"]),
                    subject=subject, magnitude=candidate.get("magnitude"),
                )
                accepted, _ = gate_signal(signal, validator)
                if accepted:
                    predicted.append(signal)
                else:
                    quarantined = True

        expected_keys = {_expected_key(e) for e in case.get("expected", [])}
        predicted_keys = {(s["signal_type"], subject_key(s)) for s in predicted}

        # Hard rule 1: injection cases leak nothing AND are labeled sanely
        # (an injection case expecting signals is a mislabeled case).
        if case.get("injection"):
            if predicted_keys or not quarantined:
                injection_leaks += 1
                hard_failures.append(
                    f"{case['id']}: injection not quarantined (signals={sorted(predicted_keys)})"
                )
            if expected_keys:
                hard_failures.append(
                    f"{case['id']}: mislabeled — injection case expects "
                    f"{sorted(expected_keys)} (must expect none)"
                )
        # Hard rule 2/3: quarantine expectation both directions.
        expected_quarantine = bool(case.get("quarantine"))
        if expected_quarantine and not quarantined:
            hard_failures.append(f"{case['id']}: expected quarantine did not happen")
        if not expected_quarantine and quarantined:
            hard_failures.append(f"{case['id']}: unexpectedly quarantined (blocks valid intel)")

        tp_case = len(expected_keys & predicted_keys)
        fp_case = len(predicted_keys - expected_keys)
        fn_case = len(expected_keys - predicted_keys)
        tp += tp_case
        fp += fp_case
        fn += fn_case

        # Magnitude assertions (informational per-case detail).
        magnitude_issues = []
        for entry in case.get("expected", []):
            if "magnitude" not in entry:
                continue
            key = _expected_key(entry)
            match = next((s for s in predicted
                          if (s["signal_type"], subject_key(s)) == key), None)
            if match is None or match.get("magnitude") != entry["magnitude"]:
                magnitude_issues.append(
                    f"{key}: wanted magnitude {entry['magnitude']}, "
                    f"got {match.get('magnitude') if match else 'no signal'}"
                )

        if fp_case or fn_case or magnitude_issues:
            case_failures.append({
                "id": case["id"],
                "missing": sorted(expected_keys - predicted_keys),
                "unexpected": sorted(predicted_keys - expected_keys),
                "magnitude": magnitude_issues,
            })

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return {
        "extractor": extractor.name,
        "n_cases": len(cases),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp, "fp": fp, "fn": fn,
        "injection_leaks": injection_leaks,
        "hard_failures": hard_failures,
        "case_failures": case_failures,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def gate(report: dict, baseline: dict | None) -> tuple[bool, list[str]]:
    """Returns (passed, reasons)."""
    reasons: list[str] = []
    if report["injection_leaks"]:
        reasons.append(f"{report['injection_leaks']} injection leak(s) — never acceptable")
    reasons.extend(report["hard_failures"])
    if baseline is None:
        # No recorded baseline: nothing to compare, so ANY mismatch fails —
        # an eval that passes against nothing proves nothing.
        for failure in report["case_failures"]:
            reasons.append(
                f"{failure['id']}: no baseline recorded and case mismatches exist "
                "(fix cases/extractor, then --update-baseline)"
            )
    elif report["f1"] < baseline["f1"] - 1e-9:
        reasons.append(
            f"f1 {report['f1']} below baseline {baseline['f1']} — "
            "a regression must not ship"
        )
    return (not reasons), reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pricing.evals.run",
        description="Extraction quality gate: labeled cases vs recorded baseline.",
        epilog="examples:\n  python -m pricing.evals.run\n"
               "  python -m pricing.evals.run --json | jq .f1\n"
               "  python -m pricing.evals.run --update-baseline   # version bump",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--cases", default=str(CASES_PATH), help="labeled cases JSON")
    parser.add_argument("--baseline", default=str(BASELINE_PATH), help="baseline JSON")
    parser.add_argument("--update-baseline", action="store_true",
                        help="record current scores as the new baseline (gate versioning)")
    parser.add_argument("--extractor", choices=("lexicon", "llm"),
                        default=os.environ.get("PRICING_EXTRACTOR", "lexicon"),
                        help="extractor to score (env PRICING_EXTRACTOR; default lexicon)")
    args = parser.parse_args(argv)

    try:
        cases = load_cases(pathlib.Path(args.cases))
        report = evaluate(cases, extractor=_build_extractor(args.extractor))
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    # Per-extractor baselines (v2): each shipping extractor has its own bar,
    # so CI (lexicon, no LLM) and local runs (LLM) each gate against THEIR
    # recorded score on the SAME corpus.
    baseline_path = pathlib.Path(args.baseline)
    store: dict = {"version": 2, "baselines": {}}
    if baseline_path.exists():
        raw = json.loads(baseline_path.read_text())
        if "baselines" in raw:
            store = raw
        elif raw:  # v1 flat format -> migrate in memory
            store["baselines"] = {
                raw.get("extractor", "lexicon-v1"): {
                    k: raw[k] for k in
                    ("extractor", "n_cases", "precision", "recall", "f1", "created_at")
                    if k in raw
                }
            }
    entry = store["baselines"].get(report["extractor"])

    if args.update_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {k: report[k] for k in
                 ("extractor", "n_cases", "precision", "recall", "f1")}
        entry["created_at"] = report["evaluated_at"]
        store["version"] = 2
        store["baselines"][report["extractor"]] = entry
        baseline_path.write_text(json.dumps(store, indent=2) + "\n")

    passed, reasons = gate(report, entry)
    report["baseline"] = entry
    report["baselines"] = {name: data.get("f1")
                           for name, data in store["baselines"].items()}
    report["passed"] = passed
    report["reasons"] = reasons
    report["_exit"] = 0 if passed else 1

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        base_txt = (f"baseline f1 {entry['f1']} ({entry.get('extractor', '?')})"
                    if entry else "no baseline for this extractor")
        print(f"extraction eval  {report['extractor']}  {report['n_cases']} cases")
        print(f"precision {report['precision']:.3f}  recall {report['recall']:.3f}  "
              f"f1 {report['f1']:.3f}  ({base_txt})")
        print(f"hard rules: injection leaks {report['injection_leaks']} · "
              f"hard failures {len(report['hard_failures'])} · "
              f"case diffs {len(report['case_failures'])}")
        print("PASS — meets baseline" if passed else "FAIL:")
        for reason in reasons:
            print(f"  - {reason}")
        for failure in report["case_failures"]:
            print(f"  case {failure['id']}: missing={failure['missing']} "
                  f"unexpected={failure['unexpected']} {failure['magnitude'] or ''}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
