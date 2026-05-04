"""Base protocol for site-specific scrapers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.robots import RobotsCache


@dataclass
class ParsedFatwa:
    question: str
    answer: str
    source: str
    url: str
    category: str | None = None
    date: str | None = None


class FatwaSource(Protocol):
    """Each source yields detail URLs then parses pages into ParsedFatwa."""

    name: str

    def iter_detail_urls(self, client: PoliteHttpClient, robots: RobotsCache, limit: int | None) -> list[str]:
        ...

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        ...
