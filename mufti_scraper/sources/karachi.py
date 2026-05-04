"""
Jamia Darul Uloom Karachi — seed is informational; BFS discovers same-domain links.

Only enqueues paths that look like fatwa/content pages. Respects robots.txt and depth.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from mufti_scraper.cleaning import canonical_url, extract_readable_text, normalize_text
from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.robots import RobotsCache
from mufti_scraper.sources.base import ParsedFatwa

logger = logging.getLogger(__name__)

SEED = "https://darululoomkarachi.edu.pk/?page_id=20"
SOURCE_NAME = "Jamia Darul Uloom Karachi"

_CONTENT_PATH_RE = re.compile(
    r"(ifta|fatwa|fatawa|فتوی|سوال|question|article|post|page_id)",
    re.I,
)


class KarachiSource:
    name = "karachi"

    def __init__(self, max_depth: int = 3) -> None:
        self.max_depth = max_depth

    def iter_detail_urls(
        self,
        client: PoliteHttpClient,
        robots: RobotsCache,
        limit: int | None,
    ) -> list[str]:
        seed_netloc = urlparse(SEED).netloc.lower()
        seen_urls: set[str] = set()
        detail_urls: list[str] = []
        q: deque[tuple[str, int]] = deque([(canonical_url(SEED), 0)])

        while q and (limit is None or len(detail_urls) < limit):
            url, depth = q.popleft()
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if not robots.can_fetch(url):
                continue
            try:
                r = client.get(url)
                r.raise_for_status()
            except Exception as e:
                logger.warning("Karachi fetch %s: %s", url, e)
                continue
            soup = BeautifulSoup(r.text, "lxml")
            if _is_probable_content_page(url, soup):
                if url not in detail_urls:
                    detail_urls.append(url)
                    if limit is not None and len(detail_urls) >= limit:
                        break
            if depth >= self.max_depth:
                continue
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                if not href or href.startswith("#"):
                    continue
                full = canonical_url(urljoin(url, href))
                p = urlparse(full)
                if p.netloc.lower() != seed_netloc:
                    continue
                if full in seen_urls:
                    continue
                path_q = (p.path or "") + "?" + (p.query or "")
                if not _CONTENT_PATH_RE.search(path_q):
                    continue
                q.append((full, depth + 1))
        return detail_urls

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        soup = BeautifulSoup(html, "lxml")
        title_el = soup.select_one("h1, h2.entry-title, article h1")
        title = normalize_text(title_el.get_text()) if title_el else ""
        main = soup.select_one("article, main, .entry-content, .post-content, #content")
        if not main:
            main = soup.body
        if not main:
            return None
        text = extract_readable_text(str(main))
        if len(text) < 60:
            return None
        return ParsedFatwa(
            question=title or text[:400],
            answer=text,
            source=SOURCE_NAME,
            url=canonical_url(url),
            category=None,
            date=None,
        )


def _is_probable_content_page(url: str, soup: BeautifulSoup) -> bool:
    p = urlparse(url)
    blob = (p.path or "") + "?" + (p.query or "")
    if not _CONTENT_PATH_RE.search(blob):
        return False
    # Skip obvious navigational-only pages (short body)
    body = soup.body
    if not body:
        return False
    t = normalize_text(body.get_text())
    return len(t) > 200
