"""
Al Mufti Online (Jamia Tur Rasheed) — WordPress-style URLs.

Homepage and category archives list posts: /YYYY/MM/DD/id/
Category pages paginate: /category/fatwa/{id}/page/N/
Selenium not required: HTML contains post links.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from mufti_scraper.cleaning import canonical_url, normalize_text
from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.robots import RobotsCache
from mufti_scraper.sources.base import ParsedFatwa

logger = logging.getLogger(__name__)

BASE = "https://almuftionline.com"
SOURCE_NAME = "Al Mufti Online (Jamia Tur Rasheed)"

_POST_RE = re.compile(r"^/\d{4}/\d{2}/\d{2}/\d+/?$")


def _abs(base: str, href: str) -> str:
    return canonical_url(urljoin(base, href))


class AlMuftiSource:
    name = "almuftionline"

    def iter_detail_urls(
        self,
        client: PoliteHttpClient,
        robots: RobotsCache,
        limit: int | None,
    ) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []

        raw = self._discover_category_urls(client, robots)
        raw.append(BASE + "/")
        if len(raw) <= 1:
            raw.insert(0, f"{BASE}/category/fatwa/19/")
        seeds = list(dict.fromkeys(raw))

        for seed in seeds:
            if limit is not None and len(out) >= limit:
                break
            for u in self._urls_from_archive(client, robots, seed, limit, seen, out):
                if limit is not None and len(out) >= limit:
                    return out
        return out

    def _discover_category_urls(
        self, client: PoliteHttpClient, robots: RobotsCache
    ) -> list[str]:
        home = BASE + "/"
        if not robots.can_fetch(home):
            return []
        try:
            r = client.get(home)
            r.raise_for_status()
        except Exception as e:
            logger.warning("Al Mufti homepage failed: %s", e)
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cats: set[str] = set()
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            if "/category/fatwa/" in href:
                full = _abs(BASE, href)
                p = urlparse(full)
                m = re.match(r"/category/fatwa/\d+", p.path or "")
                if m:
                    base_cat = f"{p.scheme}://{p.netloc}{m.group(0)}/"
                    cats.add(canonical_url(base_cat))
        return sorted(cats)

    def _urls_from_archive(
        self,
        client: PoliteHttpClient,
        robots: RobotsCache,
        start_url: str,
        limit: int | None,
        seen: set[str],
        out: list[str],
    ) -> list[str]:
        """Follow WordPress pagination for a category or home."""
        parsed = urlparse(start_url)
        path = parsed.path or "/"
        if path.rstrip("/") == "":
            base_path = "/"
        else:
            base_path = path.rstrip("/") + "/"
        page = 1
        stale = 0
        while stale < 3:
            if page == 1:
                page_url = f"{parsed.scheme}://{parsed.netloc}{base_path}"
            else:
                page_url = (
                    f"{parsed.scheme}://{parsed.netloc}"
                    f"{base_path.rstrip('/')}/page/{page}/"
                )
            if not robots.can_fetch(page_url):
                break
            try:
                r = client.get(page_url)
                r.raise_for_status()
            except Exception as e:
                logger.warning("Al Mufti list %s: %s", page_url, e)
                break
            soup = BeautifulSoup(r.text, "lxml")
            found = 0
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                full = _abs(BASE, href)
                p2 = urlparse(full)
                if _POST_RE.match(p2.path or ""):
                    if full not in seen:
                        seen.add(full)
                        out.append(full)
                        found += 1
                        if limit is not None and len(out) >= limit:
                            return out
            if found == 0:
                stale += 1
            else:
                stale = 0
            page += 1
            if page > 2000:
                break
        return out

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        soup = BeautifulSoup(html, "lxml")
        title_el = soup.select_one("h1.entry-title, article h1, h1")
        title = normalize_text(title_el.get_text()) if title_el else ""

        article = soup.select_one("article, .entry-content, main")
        if not article:
            article = soup.body
        if not article:
            return None

        category = _category_from_article(soup)
        date = _date_from_meta(soup, url)

        q_text, a_text = _split_sawal_jawab_wp(article)
        if not q_text:
            q_text = title
        if not a_text:
            a_text = normalize_text(article.get_text())
        if not a_text or len(a_text) < 40:
            return None

        return ParsedFatwa(
            question=q_text,
            answer=a_text,
            source=SOURCE_NAME,
            url=canonical_url(url),
            category=category,
            date=date,
        )


def _category_from_article(soup: BeautifulSoup) -> str | None:
    for a in soup.select('a[rel="category tag"], .cat-links a, a[href*="/category/fatwa/"]'):
        t = normalize_text(a.get_text())
        if t:
            return t[:512]
    return None


def _date_from_meta(soup: BeautifulSoup, url: str) -> str | None:
    t = soup.select_one("time[datetime]")
    if t and t.get("datetime"):
        return t["datetime"][:10]
    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _split_sawal_jawab_wp(root: Tag) -> tuple[str, str]:
    q_parts: list[str] = []
    a_parts: list[str] = []
    mode: str | None = None
    for el in root.find_all(["h2", "h3", "h4", "h5", "p", "div", "li", "td"]):
        text = normalize_text(el.get_text())
        if not text:
            continue
        if text.startswith("سوال") or (text == "سوال"):
            mode = "q"
            if len(text) > 10:
                q_parts.append(text)
            continue
        if "الجواب" in text or text.startswith("جواب"):
            mode = "a"
            if len(text) > 20:
                a_parts.append(text)
            continue
        if mode == "q":
            q_parts.append(text)
        elif mode == "a":
            a_parts.append(text)
    return normalize_text("\n".join(q_parts)), normalize_text("\n".join(a_parts))
