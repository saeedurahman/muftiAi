"""
Darul Uloom Karachi scraper (WordPress-first strategy).

This source no longer uses generic BFS over site pages because that primarily
found institutional content. Instead it targets WordPress post content via:

1) WP REST API: /wp-json/wp/v2/posts?per_page=100&page=N
2) Category listing fallback: ?cat=<id> plus pagination links
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from mufti_scraper.cleaning import canonical_url, normalize_text
from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.robots import RobotsCache
from mufti_scraper.sources.base import ParsedFatwa

logger = logging.getLogger(__name__)

BASE = "https://darululoomkarachi.edu.pk"
SOURCE_NAME = "Darul Uloom Karachi"
FATWA_CATS: list[int] = [1, 2, 3, 4, 28, 30, 32, 45]
_WP_POST_LINK_RE = re.compile(r"[?&]p=\d+", re.I)


class KarachiSource:
    name = "karachi"

    def __init__(self, max_depth: int = 3) -> None:
        # Kept only for backward compatibility with registry wiring.
        # New WordPress-based strategy no longer uses crawl depth.
        _ = max_depth
        self._api_post_cache: dict[str, ParsedFatwa] = {}

    def iter_detail_urls(
        self,
        client: PoliteHttpClient,
        robots: RobotsCache,
        limit: int | None,
    ) -> list[str]:
        return self.collect_urls(client, robots, limit)

    def collect_urls(
        self,
        client: PoliteHttpClient,
        robots: RobotsCache,
        limit: int | None,
    ) -> list[str]:
        """Collect Karachi post URLs via WP API first, category crawl second."""
        seen: set[str] = set()
        out: list[str] = []

        # STEP 1: WordPress REST API (preferred).
        api_endpoints = (
            f"{BASE}/wp-json/wp/v2/posts",
            f"{BASE}/index.php?rest_route=/wp/v2/posts",
        )
        for api_endpoint in api_endpoints:
            for page in range(1, 101):
                if limit is not None and len(out) >= limit:
                    break
                api_url = f"{api_endpoint}&per_page=100&page={page}" if "rest_route=" in api_endpoint else f"{api_endpoint}?per_page=100&page={page}"
                if not robots.can_fetch(api_url):
                    break
                try:
                    r = client.get(api_url)
                    if r.status_code == 404:
                        break
                    r.raise_for_status()
                except Exception as e:
                    logger.info("Karachi WP-API page %d failed: %s", page, e)
                    break

                try:
                    posts = r.json()
                except ValueError as e:
                    logger.info("Karachi WP-API decode failed page %d: %s", page, e)
                    break
                if not isinstance(posts, list):
                    break

                logger.info("Karachi WP-API: page %d -> %d posts", page, len(posts))
                if not posts:
                    break

                new_count = 0
                for post in posts:
                    if not isinstance(post, dict):
                        continue
                    link = normalize_text(str(post.get("link", "")))
                    if not link:
                        continue
                    full = canonical_url(link)
                    if full in seen:
                        continue
                    parsed = self._parse_wp_api_post(post)
                    if parsed is None:
                        continue
                    self._api_post_cache[full] = parsed
                    seen.add(full)
                    out.append(full)
                    new_count += 1
                    if limit is not None and len(out) >= limit:
                        break
                if new_count == 0:
                    break
            if out:
                break

        if out:
            return out[:limit] if limit else out

        # STEP 2: Category crawl fallback.
        for cat_id in FATWA_CATS:
            if limit is not None and len(out) >= limit:
                break
            next_pages: list[str] = [canonical_url(f"{BASE}/?cat={cat_id}")]
            visited: set[str] = set()
            while next_pages and (limit is None or len(out) < limit):
                page_url = next_pages.pop(0)
                if page_url in visited:
                    continue
                visited.add(page_url)
                if not robots.can_fetch(page_url):
                    continue
                try:
                    r = client.get(page_url)
                    r.raise_for_status()
                except Exception as e:
                    logger.warning("Karachi cat=%s page=%s: %s", cat_id, page_url, e)
                    continue
                soup = BeautifulSoup(r.text, "lxml")

                for a in soup.select("a[href]"):
                    href = a.get("href") or ""
                    full = canonical_url(urljoin(page_url, href))
                    if not _is_wp_post_link(full):
                        continue
                    if full in seen:
                        continue
                    seen.add(full)
                    out.append(full)
                    if limit is not None and len(out) >= limit:
                        break

                for nxt in _find_next_page_urls(soup, page_url):
                    if nxt not in visited:
                        next_pages.append(nxt)

        return out[:limit] if limit else out

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        cached = self._api_post_cache.get(canonical_url(url))
        if cached is not None:
            return cached

        soup = BeautifulSoup(html, "lxml")

        # STEP 1: Remove noise/chrome.
        for tag in soup.select(
            "nav, header, footer, .sidebar, .widget-area, .comments-area, script, style, .wp-block-navigation"
        ):
            tag.decompose()

        # STEP 2: Title as base question.
        title_el = soup.select_one(
            "h1.entry-title, h1.post-title, h1.wp-block-post-title, h1"
        )
        q_text = normalize_text(title_el.get_text()) if title_el else ""
        a_text = ""

        # STEP 3: Main content extraction and optional sawal/jawab split.
        content_el = soup.select_one(
            ".entry-content, .post-content, .wp-block-post-content, article .content, #content article, .site-main article"
        )
        if content_el:
            q_split, a_split = _split_sawal_jawab(content_el)
            if q_split and a_split:
                q_text = q_split
                a_text = a_split
            else:
                paragraphs = []
                for p in content_el.find_all(["p", "div"]):
                    t = normalize_text(p.get_text())
                    if len(t) > 20:
                        paragraphs.append(t)
                a_text = normalize_text("\n\n".join(paragraphs))

        # STEP 4: Category extraction.
        cat_el = soup.select_one(".cat-links a, .entry-categories a, [rel='category tag']")
        category = normalize_text(cat_el.get_text()) if cat_el else "karachi"

        # STEP 5: Date extraction.
        date_str = None
        date_el = soup.select_one(
            "time.entry-date, time[datetime], .published, meta[property='article:published_time']"
        )
        if date_el:
            date_str = date_el.get("datetime") or date_el.get("content") or None

        # STEP 6: Quality check.
        if len(q_text) < 10 or len(a_text) < 30:
            logger.debug(
                "Karachi: skipping low-quality parse url=%s q=%d a=%d",
                url,
                len(q_text),
                len(a_text),
            )
            return None

        return ParsedFatwa(
            question=q_text,
            answer=a_text,
            source="karachi",
            url=canonical_url(url),
            category=category,
            date=date_str or None,
        )

    def _parse_wp_api_post(self, post: dict) -> ParsedFatwa | None:
        """Parse a post directly from WP REST API JSON — no HTML needed."""
        title = normalize_text(str(post.get("title", {}).get("rendered", "")))
        content_html = str(post.get("content", {}).get("rendered", ""))
        content_soup = BeautifulSoup(content_html, "lxml")

        q_text, a_text = _split_sawal_jawab(content_soup)
        if not q_text:
            q_text = title
        if not a_text:
            a_text = normalize_text(content_soup.get_text())

        if len(q_text) < 10 or len(a_text) < 30:
            return None

        return ParsedFatwa(
            question=q_text,
            answer=a_text,
            source="karachi",
            url=normalize_text(str(post.get("link", ""))),
            category="karachi",
            date=normalize_text(str(post.get("date", ""))) or None,
        )


def _is_wp_post_link(url: str) -> bool:
    p = urlparse(url)
    if "darululoomkarachi.edu.pk" not in p.netloc.lower():
        return False
    blob = (p.path or "") + "?" + (p.query or "")
    return bool(_WP_POST_LINK_RE.search(blob))


def _find_next_page_urls(soup: BeautifulSoup, current_url: str) -> list[str]:
    out: list[str] = []
    seen_local: set[str] = set()
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        text = normalize_text(a.get_text()).lower()
        if not (
            "اگلا" in text
            or "next" in text
            or ">>" in text
            or re.search(r"[?&]paged=\d+", href, re.I)
            or re.search(r"/page/\d+", href, re.I)
        ):
            continue
        full = canonical_url(urljoin(current_url, href))
        if full in seen_local:
            continue
        seen_local.add(full)
        out.append(full)
    return out


def _split_sawal_jawab(root: Tag) -> tuple[str, str]:
    q_parts: list[str] = []
    a_parts: list[str] = []
    mode: str | None = None
    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "p", "div", "li", "td"]):
        text = normalize_text(el.get_text())
        if not text:
            continue
        if "سوال" in text and len(text) < 120:
            mode = "q"
            if len(text) > 10:
                q_parts.append(text)
            continue
        if "جواب" in text and len(text) < 160:
            mode = "a"
            if len(text) > 10:
                a_parts.append(text)
            continue
        if mode == "q":
            q_parts.append(text)
        elif mode == "a":
            a_parts.append(text)
    return normalize_text("\n".join(q_parts)), normalize_text("\n".join(a_parts))
