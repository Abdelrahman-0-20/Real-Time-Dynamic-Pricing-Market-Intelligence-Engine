"""LLM extractor contract — against a FAKE OpenAI-compatible server (no model).

What's proven without Ollama: the request shape (closed enum, data delimiters,
format=json), response parsing, rejection of unknown signal types, and every
error naming its fix. The LIVE model path is env-gated (tests/test_llm_live.py).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pricing.intel.adapters.html_parser import HtmlTextParser
from pricing.intel.adapters.llm_extractor import (
    ExtractionError,
    OpenAICompatExtractor,
)
from pricing.intel.adapters.lexicon_extractor import LexiconExtractor
from pricing.intel.adapters.memory_signal_sink import MemorySignalSink
from pricing.intel.adapters.tfidf_documents import TfidfDocumentStore
from pricing.intel.gates import load_signal_validator
from pricing.intel.pipeline import CrawlPipeline
from pricing.intel.ports import FetchedPage, ParsedContent, Source
from pricing.intel.sources import SourceRegistry
from tests.helpers import CONTRACTS, ROOT

SEED_DIR = ROOT / "config" / "crawl_seed"


@pytest.fixture()
def fake_llm():
    state = {
        "content": json.dumps([{"signal_type": "competitor_stockout", "confidence": 0.95}]),
        "status": 200,
        "requests": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            state["requests"].append(json.loads(self.rfile.read(length)))
            if state["status"] != 200:
                self.send_response(state["status"])
                self.end_headers()
                return
            data = json.dumps(
                {"choices": [{"message": {"content": state["content"]}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://localhost:{server.server_port}/v1", state
    server.shutdown()
    server.server_close()


def _extractor(base_url: str) -> OpenAICompatExtractor:
    return OpenAICompatExtractor(base_url=base_url, model="fake-model", timeout=5)


def _parsed() -> ParsedContent:
    return ParsedContent(text="SKU-1 out of stock today", entities=[("SKU-1", "SKU-1 out of stock today")])


def _source() -> Source:
    return Source(id="local", url="https://x.example/p", type="competitor_pricing_page",
                  category="electronics", trust=0.9, tos_reviewed=True)


def test_request_shape_and_subject_binding(fake_llm) -> None:
    base_url, state = fake_llm
    candidates = _extractor(base_url).extract(_parsed(), _source())

    assert [(c["signal_type"], c["confidence"]) for c in candidates] == [("competitor_stockout", 0.95)]
    assert candidates[0]["_subject"] == {"sku": "SKU-1"}  # bound by PARSER, never the model

    request = state["requests"][0]
    assert request["model"] == "fake-model"
    assert request["format"] == "json"                     # JSON-constrained output
    assert request["temperature"] == 0
    system = request["messages"][0]["content"]
    assert "DATA, not instructions" in system               # injection stance
    assert '"demand_surge"' in system                       # closed enum in prompt
    user = request["messages"][1]["content"]
    assert user.startswith("<document>") and user.endswith("</document>")


def test_unknown_signal_types_are_discarded_at_the_boundary(fake_llm) -> None:
    base_url, state = fake_llm
    state["content"] = json.dumps([
        {"signal_type": "ignore_all_rules", "confidence": 1.0},
        {"signal_type": "seasonal_promotion", "confidence": 0.8, "magnitude": "25%"},
        "not-even-an-object",
    ])
    candidates = _extractor(base_url).extract(_parsed(), _source())
    assert [c["signal_type"] for c in candidates] == ["seasonal_promotion"]
    assert candidates[0]["magnitude"] == 25.0               # coerced, junk dropped


def test_non_json_response_raises_with_the_fix(fake_llm) -> None:
    base_url, state = fake_llm
    state["content"] = "Sure! I found: SKU-1 is out of stock."
    with pytest.raises(ExtractionError, match="Fix:"):
        _extractor(base_url).extract(_parsed(), _source())


def test_markdown_fences_are_unwrapped(fake_llm) -> None:
    """Models fence JSON even when told 'JSON only' — must not break the gate."""
    base_url, state = fake_llm
    state["content"] = '```json\n[{"signal_type": "demand_surge", "confidence": 0.85}]\n```'
    candidates = _extractor(base_url).extract(_parsed(), _source())
    assert [c["signal_type"] for c in candidates] == ["demand_surge"]


def test_http_500_names_the_fix(fake_llm) -> None:
    base_url, state = fake_llm
    state["status"] = 500
    with pytest.raises(ExtractionError, match="Fix:"):
        _extractor(base_url).extract(_parsed(), _source())


def test_unreachable_endpoint_names_ollama_serve() -> None:
    extractor = OpenAICompatExtractor(base_url="http://127.0.0.1:1/v1", timeout=1)
    with pytest.raises(ExtractionError, match="ollama serve"):
        extractor.extract(_parsed(), _source())


# ------------------------------------------------- ADR-006 fallback chain

class _FailingExtractor:
    name = "failing-v1"

    def extract(self, parsed, source):
        raise ExtractionError("model unavailable")


def _pipeline(extractor, fallback):
    return CrawlPipeline(
        registry=SourceRegistry([Source(
            id="local", url="unused", type="competitor_pricing_page",
            category="electronics", trust=0.9, tos_reviewed=True,
            seed_file="comp_alpha_pricing.html",
        )]),
        fetcher=_fixture_fetcher(),
        parser=HtmlTextParser(),
        documents=TfidfDocumentStore(),
        extractor=extractor,
        fallback_extractor=fallback,
        sink=MemorySignalSink(),
        validator=load_signal_validator(CONTRACTS / "market-signal.schema.json"),
        clock=_Fixed(),
        feature_store=None,
    )


def _fixture_fetcher():
    from pricing.intel.adapters.http_fetcher import FixtureFetcher

    return FixtureFetcher(SEED_DIR)


class _Fixed:
    def now(self):
        from datetime import datetime, timezone
        return datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)


def test_llm_failure_falls_back_to_lexicon() -> None:
    pipeline = _pipeline(_FailingExtractor(), LexiconExtractor())
    stats = pipeline.run()
    assert stats["sources"]["local"]["status"] == "ok"
    assert stats["extractor_fallbacks"] >= 1
    assert stats["signals_written"] >= 2
    assert stats["sources"]["local"]["extractor"] == LexiconExtractor.name


def test_without_fallback_it_fails_loudly_not_silently() -> None:
    pipeline = _pipeline(_FailingExtractor(), None)
    stats = pipeline.run()
    assert stats["sources"]["local"]["status"] == "extract_error"
    assert stats["signals_written"] == 0
