"""Cold-path CLI — cron/Airflow entry point (ADR-009 scheduling story).

    python -m pricing.intel.run                          # crawl all reviewed sources (fixture mode)
    python -m pricing.intel.run --source comp-alpha-pricing --json
    python -m pricing.intel.run --http                   # real HTTP instead of seed files

Exit codes: 0 ok · 2 usage · 3 completed with quarantines/advisories
(degraded intel, pricing unaffected) · 1 unexpected failure.
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
from pricing.intel.adapters.http_fetcher import FixtureFetcher, HttpFetcher  # noqa: E402
from pricing.intel.adapters.lexicon_extractor import LexiconExtractor  # noqa: E402
from pricing.intel.adapters.memory_signal_sink import MemorySignalSink  # noqa: E402
from pricing.intel.adapters.tfidf_documents import TfidfDocumentStore  # noqa: E402
from pricing.intel.gates import load_signal_validator  # noqa: E402
from pricing.intel.pipeline import CrawlPipeline  # noqa: E402
from pricing.intel.sources import SourceRegistry  # noqa: E402


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def persist_state(var_dir, stats: dict, sink, mode: str) -> pathlib.Path:
    """Write var/intel_state.json — the cold path's own output artifact.

    BOTH entry points call this (`python -m pricing.intel.run` and
    `pricingctl crawl run`): the API's GET /v1/signals reads this file, so
    whichever command a user runs, the state lands. Keys must stay identical
    between the two callers — one format, one reader.
    """
    var_dir = pathlib.Path(var_dir)
    var_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "signals_written": stats["signals_written"],
        "quarantined": stats["quarantined"],
        "advisories": stats["advisories"],
        "sources": stats["sources"],
        "signals": sink.signals,
        "quarantine_records": sink.quarantines,
        "mode": mode,
    }
    path = var_dir / "intel_state.json"
    path.write_text(json.dumps(state, indent=2) + "\n")
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pricing.intel.run",
        description="Crawl reviewed sources, extract market signals, write features.",
        epilog="examples:\n"
               "  python -m pricing.intel.run\n"
               "  python -m pricing.intel.run --source comp-alpha-pricing --json\n"
               "  python -m pricing.intel.run --http",
    )
    parser.add_argument("--source", action="append", default=None, metavar="ID",
                        help="crawl only this source (repeatable)")
    parser.add_argument("--config", default=str(_REPO_ROOT / "config" / "sources.json"),
                        help="source registry JSON (default config/sources.json)")
    parser.add_argument("--seed-dir", default=str(_REPO_ROOT / "config" / "crawl_seed"),
                        help="fixture HTML directory for local runs")
    parser.add_argument("--http", action="store_true",
                        help="fetch over real HTTP instead of seed files")
    parser.add_argument("--extractor", choices=("lexicon", "llm"),
                        default=os.environ.get("PRICING_EXTRACTOR", "lexicon"),
                        help="signal extractor (llm = Ollama, falls back to lexicon on failure; default lexicon)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output only (pipe-safe)")
    args = parser.parse_args(argv)

    registry = SourceRegistry.from_config(args.config)
    sink = MemorySignalSink()
    documents = TfidfDocumentStore()

    if args.extractor == "llm":
        from pricing.intel.adapters.llm_extractor import OpenAICompatExtractor

        # ADR-006 fallback chain: LLM primary, lexicon tier-3.
        primary_extractor, fallback_extractor = OpenAICompatExtractor(), LexiconExtractor()
    else:
        primary_extractor, fallback_extractor = LexiconExtractor(), None

    pipeline = CrawlPipeline(
        registry=registry,
        fetcher=HttpFetcher() if args.http else FixtureFetcher(args.seed_dir),
        parser=HtmlTextParser(),
        documents=documents,
        extractor=primary_extractor,
        fallback_extractor=fallback_extractor,
        sink=sink,
        validator=load_signal_validator(
            _REPO_ROOT / "docs" / "contracts" / "market-signal.schema.json"
        ),
        clock=_SystemClock(),
        feature_store=None,
    )

    try:
        stats = pipeline.run(args.source)
    except ValueError as exc:  # unknown --source, bad config: actionable messages
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    stats["signals"] = len(sink.signals)
    stats["quarantine_records"] = sink.quarantines
    stats["feature_store_wired"] = pipeline.has_feature_store

    # Persist the artifact GET /v1/signals reads — one format, both entry
    # points (this command and `pricingctl crawl run`) write the same file.
    persist_state(_REPO_ROOT / "var", stats, sink,
                  mode="http" if args.http else "fixture")

    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        features = (
            str(stats["features_written"]) if pipeline.has_feature_store
            else "n/a (standalone CLI — the crawl worker wires the feature store)"
        )
        print(f"signals {stats['signals']} written | features {features} "
              f"| quarantined {stats['quarantined']} | advisories {stats['advisories']}")
        for sid, result in stats["sources"].items():
            print(f"  {sid:28s} {result['status']}")
        for q in sink.quarantines:
            print(f"  quarantine {q['doc_id'][:12]}… {q['reason']}: {q['detail'][:70]}")

    if stats["quarantined"] or stats["advisories"]:
        return 3  # degraded intel — pricing continues, but it happened
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
