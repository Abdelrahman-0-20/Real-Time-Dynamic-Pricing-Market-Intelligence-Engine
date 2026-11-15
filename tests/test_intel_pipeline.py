"""End-to-end cold path: crawl seeds -> signals -> features -> HOT PATH prices.

The ADR-001 claim under test: the feature store is the only join, so a
signal extracted from scraped HTML must show up in a served DecisionRecord
without the pricing service knowing where it came from.
"""
from __future__ import annotations

import json
import pathlib

import jsonschema
import pytest

from pricing.adapters.clocks import FixedClock
from pricing.intel.adapters.html_parser import HtmlTextParser
from pricing.intel.adapters.http_fetcher import FixtureFetcher, HttpFetcher
from pricing.intel.adapters.lexicon_extractor import LexiconExtractor
from pricing.intel.adapters.memory_signal_sink import MemorySignalSink
from pricing.intel.adapters.tfidf_documents import TfidfDocumentStore
from pricing.intel.gates import load_signal_validator
from pricing.intel.pipeline import CrawlPipeline
from pricing.intel.ports import Source
from pricing.intel.sources import SourceRegistry
from tests.helpers import CONTRACTS, ROOT, load_schema, make_service, seed_sku, validate_record

SEED_DIR = ROOT / "config" / "crawl_seed"
INTEL_FIXTURES = ROOT / "tests" / "fixtures" / "intel"


def make_pipeline(sources, seed_dir=SEED_DIR, feature_store=None, clock=None):
    clock = clock or FixedClock()
    sink = MemorySignalSink()
    documents = TfidfDocumentStore()
    pipeline = CrawlPipeline(
        registry=SourceRegistry(sources),
        fetcher=FixtureFetcher(seed_dir),
        parser=HtmlTextParser(),
        documents=documents,
        extractor=LexiconExtractor(),
        sink=sink,
        validator=load_signal_validator(CONTRACTS / "market-signal.schema.json"),
        clock=clock,
        feature_store=feature_store,
    )
    return pipeline, sink, documents, clock


def config_sources() -> list[Source]:
    return SourceRegistry.from_config(ROOT / "config" / "sources.json").all()


# ------------------------------------------------------------ end-to-end crawl

def test_seed_crawl_extracts_real_signals(tmp_path=None) -> None:
    from pricing.adapters.memory_features import InMemoryFeatureStore

    store = InMemoryFeatureStore()
    pipeline, sink, documents, _ = make_pipeline(config_sources(), feature_store=store)
    stats = pipeline.run()

    assert stats["signals_written"] >= 4, stats
    assert stats["features_written"] >= 4, stats
    # Written features are decayed signal values on the right entities.
    now = FixedClock().now()
    sku_features = store.snapshot("sku:SKU-7780", now)
    assert "signal_competitor_stockout" in sku_features
    assert 0 < sku_features["signal_competitor_stockout"].value <= 0.92

    # Every written signal satisfies the published contract.
    validator = load_signal_validator(CONTRACTS / "market-signal.schema.json")
    for signal in sink.signals:
        jsonschema.validate(instance=signal, schema=load_schema("market-signal.schema.json"))
        list(validator.iter_errors(signal)) == []

    by_type_subject = {
        (s["signal_type"], s["subject"].get("sku") or s["subject"].get("category"))
        for s in sink.signals
    }
    assert ("competitor_stockout", "SKU-7780") in by_type_subject
    assert ("seasonal_promotion", "SKU-1123") in by_type_subject
    assert ("demand_surge", "SKU-5501") in by_type_subject
    assert ("competitor_price_change", "electronics") in by_type_subject

    promo = next(s for s in sink.signals if s["signal_type"] == "seasonal_promotion")
    assert promo["magnitude"] == pytest.approx(25.0)
    assert promo["source_trust"] == pytest.approx(0.9)  # pipeline-owned trust

    # The bare-"sale" mention in the news article must be gated (conf 0.62).
    assert stats["quarantined"] >= 1
    assert any(q["reason"] == "schema_or_confidence_gate" for q in sink.quarantines)


def test_unreviewed_source_is_refused_without_touching_the_network() -> None:
    class SpyFetcher:
        calls = 0

        def fetch(self, source):
            self.calls += 1
            raise AssertionError("unreviewed source must never be fetched")

    pipeline, sink, _, _ = make_pipeline(config_sources())
    pipeline._fetcher = SpyFetcher()  # deliberate: refusal happens pre-fetch
    stats = pipeline.run(["shady-deals-aggregator"])

    assert stats["sources"]["shady-deals-aggregator"]["status"] == "refused_tos_not_reviewed"
    assert SpyFetcher.calls == 0


def test_injected_document_is_quarantined_and_produces_zero_signals() -> None:
    hostile = Source(
        id="injected-deals", url="https://evil.example/deals",
        type="competitor_pricing_page", category="electronics",
        trust=0.9, tos_reviewed=True, seed_file="injected_deals.html",
    )
    pipeline, sink, _, _ = make_pipeline([hostile], seed_dir=INTEL_FIXTURES)
    stats = pipeline.run()

    assert stats["sources"]["injected-deals"]["status"] == "quarantined"
    assert sink.signals == [], "hostile content must never become a signal"
    assert stats["signals_written"] == 0
    assert sink.quarantines[0]["reason"] == "injection_suspect"


def test_broken_source_trips_the_breaker_instead_of_retrying_forever() -> None:
    broken = Source(id="broken", url="https://broken.example/x",
                    type="competitor_pricing_page", category="electronics",
                    trust=0.5, tos_reviewed=True, seed_file=None)  # no seed => error
    pipeline, _, _, clock = make_pipeline([broken])
    statuses = [pipeline.run(["broken"])["sources"]["broken"]["status"] for _ in range(6)]

    assert statuses[:5] == ["fetch_or_parse_error"] * 5
    assert statuses[5] == "breaker_open"
    clock.advance(seconds=61)
    assert pipeline.run(["broken"])["sources"]["broken"]["status"] == "fetch_or_parse_error"


def test_document_store_dedups_and_retrieves() -> None:
    pipeline, _, documents, _ = make_pipeline(config_sources())
    pipeline.run()
    assert len(documents.doc_ids) == 3  # three seed pages, all distinct

    hits = documents.search("wireless earbuds price SKU-4471", k=3)
    assert hits, "retrieval returned nothing"
    first_text = documents._docs[hits[0]]
    assert "SKU-4471" in first_text


# ------------------------------------------------- THE JOIN: cold -> hot path

def test_hot_path_prices_with_the_extracted_signal_feature(tiny_artifacts: pathlib.Path) -> None:
    ctx = make_service(tiny_artifacts)
    seed_sku(ctx, "SKU-7780", row=11)  # base features for the SKU

    pipeline, sink, _, _ = make_pipeline(config_sources(), feature_store=ctx.store)
    stats = pipeline.run()
    assert stats["features_written"] >= 4

    record = ctx.service.price("SKU-7780")
    validate_record(record)

    features = record["model_input"]["features"]
    assert "signal_competitor_stockout" in features, (
        "the extracted stockout signal never reached the pricing model"
    )
    assert 0 < features["signal_competitor_stockout"] <= 0.92
    assert record["degraded"] is False


def test_category_signals_land_on_category_entity_not_a_sku() -> None:
    ctx_store = None
    from pricing.adapters.memory_features import InMemoryFeatureStore

    store = InMemoryFeatureStore()
    pipeline, _, _, _ = make_pipeline(config_sources(), feature_store=store)
    pipeline.run()

    now = FixedClock().now()
    entity_values = store.snapshot("category:electronics", now)
    assert "signal_competitor_price_change" in entity_values
    # No SKU entity was touched by the category-level news signals.
    assert store.snapshot("sku:SKU-4471", now) == {}


# ------------------------------------------------------------------ CLI

def test_cli_default_run_is_honest_about_quarantines(capsys) -> None:
    from pricing.intel.run import main

    code = main([])
    out = capsys.readouterr().out
    assert code == 3, "quarantines exist in the seed data -> degraded intel exit"
    assert "signals" in out and "quarantined" in out


def test_cli_json_is_pipe_safe(capsys) -> None:
    from pricing.intel.run import main

    code = main(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code in (0, 3)
    assert payload["signals_written"] >= 4
    assert "sources" in payload


def test_cli_unknown_source_names_the_fix(capsys) -> None:
    from pricing.intel.run import main

    code = main(["--source", "not-a-source"])
    err = capsys.readouterr().err
    assert code == 2
    assert "Fix:" in err and "not-a-source" in err


def test_http_fetcher_refuses_non_http_schemes() -> None:
    source = Source(id="x", url="file:///etc/passwd", type="market_news_article",
                    category="electronics", trust=0.5, tos_reviewed=True)
    with pytest.raises(ValueError, match="allowlist"):
        HttpFetcher().fetch(source)
