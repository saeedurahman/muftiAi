"""
Darul Ifta Al-Ikhlas — ASP.NET; published fatawa live under category.aspx and article.aspx.

The ask form (askquestionurdu.aspx) is not scraped as Q&A. We crawl category listing
pages discovered from /articles/ and known category IDs, then detail links.
Selenium not used: listings are server-rendered HTML.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from mufti_scraper.cleaning import canonical_url, html_to_clean_text, normalize_text
from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.robots import RobotsCache
from mufti_scraper.sources.base import ParsedFatwa

logger = logging.getLogger(__name__)

BASE = "https://alikhlasonline.com"
SOURCE_NAME = "Darul Ifta Al-Ikhlas (Al-Ikhlas Online)"

# Seed category IDs from site navigation (Urdu fatawa section)
DEFAULT_CATEGORY_IDS = list(range(1, 41))  # extend as site grows


class AlIkhlasSource:
    name = "alikhlas"

    def iter_detail_urls(
        self,
        client: PoliteHttpClient,
        robots: RobotsCache,
        limit: int | None,
    ) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []

        list_pages: list[str] = [f"{BASE}/articles/"]
        for cid in DEFAULT_CATEGORY_IDS:
            list_pages.append(f"{BASE}/category.aspx?id={cid}&lang=1")

        # Discover extra category links from articles index
        idx = f"{BASE}/articles/"
        if robots.can_fetch(idx):
            try:
                r = client.get(idx)
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "lxml")
                for a in soup.select("a[href]"):
                    href = a.get("href") or ""
                    if "category.aspx" in href.lower():
                        full = canonical_url(urljoin(BASE, href))
                        if full not in list_pages:
                            list_pages.append(full)
            except Exception as e:
                logger.warning("Al-Ikhlas articles index: %s", e)

        for lp in list_pages:
            if limit is not None and len(out) >= limit:
                break
            if not robots.can_fetch(lp):
                continue
            try:
                r = client.get(lp)
                r.raise_for_status()
            except Exception as e:
                logger.warning("Al-Ikhlas list %s: %s", lp, e)
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                if "article.aspx" not in href.lower():
                    continue
                full = canonical_url(urljoin(BASE, href))
                if full in seen:
                    continue
                seen.add(full)
                out.append(full)
                if limit is not None and len(out) >= limit:
                    return out
        return out

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        soup = BeautifulSoup(html, "lxml")
        h = soup.select_one("h1, h2, h3")
        title = normalize_text(h.get_text()) if h else ""
        main = soup.select_one("#main, main, .content, form, body")
        if not main:
            main = soup.body
        text = html_to_clean_text(str(main)) if main else ""
        if len(text) < 80:
            return None
        return ParsedFatwa(
            question=title or text[:400],
            answer=text,
            source=SOURCE_NAME,
            url=canonical_url(url),
            category=None,
            date=None,
        )
