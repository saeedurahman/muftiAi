"""
Darul Ifta Deoband — HTML structure varies; static fetch first (no Selenium in default path).

We collect candidate detail URLs from Urdu index/search pages and pages that expose
fatwa links (href patterns commonly include query ids). Heavy JS-only routes are
skipped unless you extend with Selenium.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from mufti_scraper.cleaning import canonical_url, html_to_clean_text, normalize_text
from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.robots import RobotsCache
from mufti_scraper.sources.base import ParsedFatwa

logger = logging.getLogger(__name__)

BASE = "https://darulifta-deoband.com"
SOURCE_NAME = "Darul Ifta Deoband"

# Common patterns on deoband site (Urdu / English fatwa pages)
_DETAIL_HINTS = re.compile(
    r"(qta|fatwa|ifta|question|answer|masala|query)",
    re.I,
)


class DeobandSource:
    name = "deoband"

    def iter_detail_urls(
        self,
        client: PoliteHttpClient,
        robots: RobotsCache,
        limit: int | None,
    ) -> list[str]:
        seeds = [
            f"{BASE}/",
            f"{BASE}/ur/",
            f"{BASE}/ur/hind/",
        ]
        seen: set[str] = set()
        out: list[str] = []
        queue = list(seeds)
        visited_pages: set[str] = set()

        while queue and (limit is None or len(out) < limit):
            page_url = queue.pop(0)
            page_url = canonical_url(page_url)
            if page_url in visited_pages:
                continue
            visited_pages.add(page_url)
            if not robots.can_fetch(page_url):
                continue
            try:
                r = client.get(page_url)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
            except Exception as e:
                logger.warning("Deoband fetch %s: %s", page_url, e)
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                if href.startswith("#") or "javascript:" in href.lower():
                    continue
                full = canonical_url(urljoin(page_url, href))
                p = urlparse(full)
                if p.netloc and "darulifta-deoband.com" not in p.netloc.lower():
                    continue
                path = (p.path or "").lower()
                qs = (p.query or "").lower()
                if full in seen:
                    continue
                # Likely listing hub: enqueue for limited crawl
                if path.rstrip("/") in ("/ur", "/ur/hind", ""):
                    continue
                if _looks_like_detail(full):
                    seen.add(full)
                    out.append(full)
                    if limit is not None and len(out) >= limit:
                        return out
                elif _looks_like_listing(full) and full not in visited_pages and len(queue) < 200:
                    queue.append(full)
        return out

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        text = html_to_clean_text(html)
        if len(text) < 80:
            return None
        # Heuristic: first paragraph as title/question, rest as answer
        lines = [ln for ln in text.split("\n") if ln.strip()]
        if not lines:
            return None
        title = normalize_text(lines[0])
        body = normalize_text("\n".join(lines[1:])) if len(lines) > 1 else text
        if len(body) < 50:
            body = text
            question = title[:500]
        else:
            question = title
        return ParsedFatwa(
            question=question,
            answer=body,
            source=SOURCE_NAME,
            url=canonical_url(url),
            category=None,
            date=None,
        )


def _looks_like_detail(url: str) -> bool:
    p = urlparse(url)
    path = p.path or ""
    qs = p.query or ""
    if re.search(r"\d{4,}", path + "?" + qs):
        if _DETAIL_HINTS.search(path) or "q=" in qs or "id=" in qs:
            return True
    if "/ur/" in path and path.count("/") >= 4:
        return True
    return False


def _looks_like_listing(url: str) -> bool:
    p = urlparse(url)
    path = (p.path or "").lower()
    if path.endswith((".css", ".js", ".jpg", ".png", ".gif", ".pdf")):
        return False
    if "/ur/" in path and path.count("/") <= 4:
        return True
    return "search" in path or "tag" in path or "category" in path
