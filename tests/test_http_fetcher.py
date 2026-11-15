"""REAL HTTP crawl: live local server, allowlist, redirect refusal, e2e signals.

No mocks of the transport: HttpFetcher speaks to an actual socket. The
server serves the committed seed pages and one hostile redirect endpoint.
"""
from __future__ import annotations

import pathlib
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pricing.intel.adapters.html_parser import HtmlTextParser
from pricing.intel.adapters.http_fetcher import HttpFetcher
from pricing.intel.adapters.lexicon_extractor import LexiconExtractor
from pricing.intel.adapters.memory_signal_sink import MemorySignalSink
from pricing.intel.adapters.tfidf_documents import TfidfDocumentStore
from pricing.intel.gates import load_signal_validator
from pricing.intel.pipeline import CrawlPipeline
from pricing.intel.ports import Source
from pricing.intel.sources import SourceRegistry
from tests.helpers import CONTRACTS, ROOT

SEED_DIR = ROOT / "config" / "crawl_seed"


class _Handler(BaseHTTPRequestHandler):
    seed_dir: pathlib.Path = SEED_DIR

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/redirect":
            # Different host spelling on purpose: netloc mismatch must refuse.
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{self.server.server_port}/comp_alpha_pricing.html",
            )
            self.end_headers()
            return
        name = self.path.lstrip("/")
        target = self.seed_dir / name
        if target.is_file() and target.suffix == ".html":
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args) -> None:  # silence test output
        pass


@pytest.fixture()
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://localhost:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_fetch_reads_real_bytes_over_a_socket(http_server) -> None:
    source = Source(id="local", url=f"{http_server}/comp_alpha_pricing.html",
                    type="competitor_pricing_page", category="electronics",
                    trust=0.9, tos_reviewed=True, seed_file=None)
    page = HttpFetcher(timeout=5).fetch(source)
    assert "SKU-4471" in page.raw
    assert page.fetched_at.tzinfo is not None
    assert page.url == source.url


def test_redirect_to_another_host_is_refused(http_server) -> None:
    source = Source(id="local", url=f"{http_server}/redirect",
                    type="competitor_pricing_page", category="electronics",
                    trust=0.9, tos_reviewed=True, seed_file=None)
    with pytest.raises(ValueError, match="redirect left allowlisted host"):
        HttpFetcher(timeout=5).fetch(source)


def test_http_error_names_the_fix(http_server) -> None:
    source = Source(id="local", url=f"{http_server}/missing.html",
                    type="competitor_pricing_page", category="electronics",
                    trust=0.9, tos_reviewed=True, seed_file=None)
    with pytest.raises(ConnectionError, match="Fix:"):
        HttpFetcher(timeout=5).fetch(source)


def test_full_cold_path_over_real_http(http_server) -> None:
    """fetch -> parse -> gates -> signals, with the network actually involved."""
    source = Source(id="local-pricing", url=f"{http_server}/comp_alpha_pricing.html",
                    type="competitor_pricing_page", category="electronics",
                    trust=0.9, tos_reviewed=True, seed_file=None)
    sink = MemorySignalSink()
    pipeline = CrawlPipeline(
        registry=SourceRegistry([source]),
        fetcher=HttpFetcher(timeout=5),
        parser=HtmlTextParser(),
        documents=TfidfDocumentStore(),
        extractor=LexiconExtractor(),
        sink=sink,
        validator=load_signal_validator(CONTRACTS / "market-signal.schema.json"),
        clock=_UTC(),
        feature_store=None,
    )
    stats = pipeline.run()
    assert stats["sources"]["local-pricing"]["status"] == "ok"
    assert stats["signals_written"] == 2  # stockout SKU-7780 + promo SKU-1123
    kinds = {(s["signal_type"], s["subject"].get("sku")) for s in sink.signals}
    assert ("competitor_stockout", "SKU-7780") in kinds
    assert ("seasonal_promotion", "SKU-1123") in kinds


class _UTC:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
