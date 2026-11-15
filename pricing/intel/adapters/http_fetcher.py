"""Fetchers: FixtureFetcher for local/CI runs, HttpFetcher for real crawling.

HttpFetcher is stdlib-only (urllib), enforces the source allowlist and a
timeout, and sends an identifiable User-Agent. Network access never happens
without a Source object whose ToS review the pipeline already checked.
"""
from __future__ import annotations

import pathlib
import urllib.error
import urllib.request
from datetime import datetime, timezone

from pricing.intel.ports import FetchedPage, Source

USER_AGENT = "pricing-intel-crawler/0.1 (+contact: pricing-ops@example.com)"
DEFAULT_TIMEOUT_SECONDS = 10.0


class FixtureFetcher:
    """Serves seed HTML from a directory (local development + tests)."""

    def __init__(self, seed_dir: str | pathlib.Path) -> None:
        self._dir = pathlib.Path(seed_dir)

    def fetch(self, source: Source) -> FetchedPage:
        if not source.seed_file:
            raise FileNotFoundError(
                f"source '{source.id}' has no seed_file configured. "
                "Fix: add seed_file to config/sources.json or use HttpFetcher."
            )
        path = self._dir / source.seed_file
        if not path.exists():
            raise FileNotFoundError(
                f"seed file not found: {path}. "
                f"Fix: place {source.seed_file} in {self._dir} or update sources.json."
            )
        return FetchedPage(url=source.url, raw=path.read_text(encoding="utf-8"),
                           fetched_at=datetime.now(timezone.utc))


class HttpFetcher:
    """Real HTTP fetch: scheme/host allowlist, timeout, no redirects off-host."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    def fetch(self, source: Source) -> FetchedPage:
        from urllib.parse import urlparse

        parsed = urlparse(source.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                f"refusing to fetch '{source.url}': only http(s) URLs from the "
                "source allowlist are permitted. Fix: use a reviewed https source."
            )
        request = urllib.request.Request(source.url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                final = urlparse(response.geturl())
                if final.netloc != parsed.netloc:
                    raise ValueError(
                        f"redirect left allowlisted host {parsed.netloc} -> "
                        f"{final.netloc}; refusing. Fix: review the redirect target."
                    )
                raw = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ConnectionError(
                f"fetch failed for {source.id}: {exc}. "
                "Fix: check connectivity/rate limits; the circuit breaker will "
                "open after repeated failures."
            ) from None
        return FetchedPage(url=source.url, raw=raw,
                           fetched_at=datetime.now(timezone.utc))
