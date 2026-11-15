"""HTML parsing: text extraction + data-sku entity chunks (stdlib only).

Production swaps in a Docling adapter behind the same Parser port (ADR-009);
this implementation already provides what the extractor needs: readable text
and per-product chunks so signals bind to the right SKU.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

from pricing.intel.ports import FetchedPage, ParsedContent, Source

_SKU_RE = re.compile(r"SKU-[A-Z0-9]+", re.IGNORECASE)
_SKIP_TAGS = {"script", "style", "noscript"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        for name, value in attrs:
            if name.startswith("data-") and value and _SKU_RE.search(str(value)):
                self._chunks.append(f"\n[ENTITY {_SKU_RE.search(str(value)).group(0)}]\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return "\n".join(self._chunks)


class HtmlTextParser:
    """Port implementation: whole-page text + (sku, chunk) entities."""

    name = "html-text-parser-v1"

    def parse(self, page: FetchedPage, source: Source) -> ParsedContent:
        extractor = _TextExtractor()
        extractor.feed(page.raw)
        full_text = extractor.text()

        entities: list[tuple[str, str]] = []
        for match in re.finditer(
            r"\[ENTITY (SKU-[A-Z0-9]+)\](.*?)(?=\[ENTITY |\Z)",
            full_text,
            re.IGNORECASE | re.DOTALL,
        ):
            sku = match.group(1).upper()
            chunk = match.group(2).strip()
            if chunk:
                entities.append((sku, chunk))

        return ParsedContent(text=full_text, entities=entities)
