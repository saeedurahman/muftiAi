"""
Jamia Binoria (Banuri) — static HTML; requests + BeautifulSoup suffice.

Listing: /new-questions?page=N
Detail: /readquestion/{slug}/{dd-mm-yyyy}
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from mufti_scraper.cleaning import canonical_url, html_to_clean_text, normalize_text
from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.robots import RobotsCache
from mufti_scraper.sources.base import ParsedFatwa

logger = logging.getLogger(__name__)

BASE = "https://www.banuri.edu.pk"
LIST_PATH = "/new-questions"
SOURCE_NAME = "Jamia Binoria (Darul Ifta)"


def _abs(href: str) -> str:
    return urljoin(BASE, href)


class BanuriSource:
    name = "banuri"

    def iter_detail_urls(
        self,
        client: PoliteHttpClient,
        robots: RobotsCache,
        limit: int | None,
    ) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        stale = 0
        page = 1
        max_stale = 5

        while stale < max_stale:
            list_url = f"{BASE}{LIST_PATH}" + (f"?page={page}" if page > 1 else "")
            if not robots.can_fetch(list_url):
                logger.info("robots.txt disallows %s", list_url)
                break
            try:
                r = client.get(list_url)
                r.raise_for_status()
            except Exception as e:
                logger.exception("Banuri list fetch failed %s: %s", list_url, e)
                break
            soup = BeautifulSoup(r.text, "lxml")
            new_count = 0
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                if "/readquestion/" not in href:
                    continue
                full = canonical_url(_abs(href))
                if full not in seen:
                    seen.add(full)
                    out.append(full)
                    new_count += 1
                    if limit is not None and len(out) >= limit:
                        return out
            if new_count == 0:
                stale += 1
            else:
                stale = 0
            page += 1
            if page > 5000:
                break
        return out

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        soup = BeautifulSoup(html, "lxml")
        # Title / question from first main h2/h3 after breadcrumbs
        title_el = soup.select_one("h2, h3")
        title = normalize_text(title_el.get_text()) if title_el else ""

        main = soup.select_one("main, article, .content, #content, body")
        if not main:
            main = soup

        q_text, a_text = _split_sawal_jawab(main)
        if not q_text and title:
            q_text = title
        if not a_text:
            # Fallback: strip nav and take remainder
            a_text = html_to_clean_text(str(main))
            if q_text and a_text.startswith(q_text):
                a_text = normalize_text(a_text[len(q_text) :])

        if not a_text or len(a_text) < 30:
            return None

        category = _category_from_links(main)
        date = _date_from_url(url)
        return ParsedFatwa(
            question=q_text or title,
            answer=a_text,
            source=SOURCE_NAME,
            url=canonical_url(url),
            category=category,
            date=date,
        )


def _split_sawal_jawab(root: Tag) -> tuple[str, str]:
    """Find 'سوال' / 'جواب' headings (Urdu) and collect following sibling text."""
    q_parts: list[str] = []
    a_parts: list[str] = []
    mode: str | None = None
    for el in root.find_all(["h2", "h3", "h4", "h5", "p", "div", "li"]):
        text = normalize_text(el.get_text())
        if not text:
            continue
        if "سوال" in text and len(text) < 80:
            mode = "q"
            continue
        if "جواب" in text and len(text) < 120:
            mode = "a"
            continue
        if mode == "q":
            q_parts.append(text)
        elif mode == "a":
            a_parts.append(text)
    return normalize_text("\n".join(q_parts)), normalize_text("\n".join(a_parts))


def _category_from_links(root: Tag) -> str | None:
    for a in root.select('a[href*="/questions/"]'):
        t = normalize_text(a.get_text())
        if t and len(t) < 200:
            return t
    return None


def _date_from_url(url: str) -> str | None:
    # .../slug/17-01-2026
    m = re.search(r"/(\d{2}-\d{2}-\d{4})/?$", urlparse(url).path)
    return m.group(1) if m else None
