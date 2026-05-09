"""
Jamia Binoria (Banuri) — static HTML; requests + BeautifulSoup suffice.

Listing: /new-questions?page=N
Detail: /readquestion/{slug}/{dd-mm-yyyy}
"""

from __future__ import annotations

import itertools
import logging
import re
from collections.abc import Iterator
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from mufti_scraper.cleaning import canonical_url, normalize_text
from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.robots import RobotsCache
from mufti_scraper.sources.base import ParsedFatwa

logger = logging.getLogger(__name__)

BASE = "https://www.banuri.edu.pk"
LIST_PATH = "/new-questions"
SOURCE_NAME = "Jamia Binoria (Darul Ifta)"

# Selectors for the actual question listing container, tried in order.
# We narrow the anchor scan to the listing block when present so unrelated
# /readquestion/ links elsewhere on the page don't skew counts; if nothing
# matches we fall back to the full document.
_LISTING_CONTAINER_SELECTORS = (
    "div.questions",
    "ul.questions",
    "div.question-list",
    "ul.question-list",
    "div.list-group",
    "section.questions",
    "main",
    "#content",
    ".content",
)

_SAFE_STRIP_SELECTORS = "script, style, noscript, iframe"

_ANSWER_FOOTER_MARKERS = (
    "دارالافتاء",
    "فتویٰ نمبر",
    "صفحہ پرنٹ",
    "سوال پوچھیں",
    "واللہ اعلم",
    "فقط",
)


def _abs(href: str) -> str:
    return urljoin(BASE, href)


def _strip_safe(soup: BeautifulSoup) -> None:
    """Remove only non-visible / executable clutter; keep layout chrome."""
    for tag in soup.select(_SAFE_STRIP_SELECTORS):
        tag.decompose()


def _dedupe_lines(text: str) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            result.append(line)
    return "\n".join(result)


def _extract_marker_q_a(lines: list[str]) -> tuple[str, str]:
    """Parse سوال / جواب blocks from normalized line list."""
    q_text = ""
    a_text = ""

    for i, line in enumerate(lines):
        if re.search(r"^سوال\s*[:：]?\s*$", line) or line == "سوال":
            q_lines: list[str] = []
            for j in range(i + 1, min(i + 10, len(lines))):
                if re.search(r"جواب", lines[j]):
                    break
                if len(lines[j]) > 10:
                    q_lines.append(lines[j])
            q_text = "\n".join(q_lines)
            break
        if re.search(r"سوال\s*[:：]", line):
            after = re.split(r"سوال\s*[:：]", line, 1)[-1].strip()
            if len(after) > 10:
                q_text = after
            break

    for i, line in enumerate(lines):
        if re.search(r"^جواب\s*[:：]?\s*$", line) or line == "جواب":
            a_lines: list[str] = []
            for j in range(i + 1, len(lines)):
                if any(marker in lines[j] for marker in _ANSWER_FOOTER_MARKERS):
                    a_lines.append(lines[j])
                    break
                if len(lines[j]) > 5:
                    a_lines.append(lines[j])
            a_text = "\n".join(a_lines)
            break
        if re.search(r"جواب\s*[:：]", line):
            after = re.split(r"جواب\s*[:：]", line, 1)[-1].strip()
            if len(after) > 10:
                a_text = after
            break

    return normalize_text(q_text), normalize_text(a_text)


def _title_fallback(soup: BeautifulSoup) -> str:
    t_el = soup.select_one("title")
    if not t_el:
        return ""
    raw = normalize_text(t_el.get_text())
    for sep in (" | ", " - "):
        if sep in raw:
            raw = raw.split(sep)[0].strip()
    return normalize_text(raw)


def _longest_paragraph(soup: BeautifulSoup) -> str:
    best = ""
    for p in soup.find_all("p"):
        t = normalize_text(p.get_text())
        if len(t) > len(best):
            best = t
    return best


class BanuriSource:
    name = "banuri"

    def __init__(self, ua_list: list[str] | None = None) -> None:
        """
        Optional ``ua_list`` rotates the ``User-Agent`` header across listing
        page fetches. When omitted, the underlying ``PoliteHttpClient``'s
        configured UA is used unchanged.
        """
        self._ua_list: list[str] = list(ua_list) if ua_list else []
        self._ua_cycle: Iterator[str] | None = (
            itertools.cycle(self._ua_list) if self._ua_list else None
        )

    def _next_headers(self) -> dict[str, str] | None:
        if self._ua_cycle is None:
            return None
        return {"User-Agent": next(self._ua_cycle)}

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
        # Banuri pagination has occasional gaps where a listing page yields no
        # new /readquestion/ links; tolerate up to 15 consecutive empty pages
        # before giving up so we don't truncate the crawl prematurely.
        max_stale = 15

        while stale < max_stale:
            list_url = f"{BASE}{LIST_PATH}" + (f"?page={page}" if page > 1 else "")
            if not robots.can_fetch(list_url):
                logger.info("robots.txt disallows %s", list_url)
                break
            try:
                headers = self._next_headers()
                if headers:
                    r = client.get(list_url, headers=headers)
                else:
                    r = client.get(list_url)
                r.raise_for_status()
            except Exception as e:
                logger.exception("Banuri list fetch failed %s: %s", list_url, e)
                break
            soup = BeautifulSoup(r.text, "lxml")
            search_root: Tag | BeautifulSoup = soup
            for sel in _LISTING_CONTAINER_SELECTORS:
                container = soup.select_one(sel)
                if container is not None:
                    search_root = container
                    break
            new_count = 0
            for a in search_root.select("a[href]"):
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
            if page % 50 == 0:
                logger.info(
                    "Banuri progress: page=%d collected=%d stale=%d/%d",
                    page,
                    len(out),
                    stale,
                    max_stale,
                )
            page += 1
            if page > 5000:
                break
        return out

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        logger.debug("Banuri raw HTML length: %d", len(html))

        soup = BeautifulSoup(html, "lxml")
        _strip_safe(soup)

        full_text = soup.get_text(separator="\n")
        lines = [line.strip() for line in full_text.split("\n") if line.strip()]

        q_text, a_text = _extract_marker_q_a(lines)

        if not q_text:
            q_text = _title_fallback(soup)

        if not a_text or len(a_text) < 20:
            lp = _longest_paragraph(soup)
            if len(lp) > len(a_text):
                a_text = lp

        q_text = _dedupe_lines(normalize_text(q_text))
        a_text = _dedupe_lines(normalize_text(a_text))

        if len(q_text.strip()) < 10 or len(a_text.strip()) < 20:
            logger.debug(
                "Banuri parse result: q=%d chars [%s...] a=%d chars [%s...]",
                len(q_text),
                q_text[:80],
                len(a_text),
                a_text[:80],
            )
            logger.debug("Banuri: too short q=%d a=%d", len(q_text), len(a_text))
            return None

        root = soup.body if soup.body is not None else soup
        category = _category_from_links(root)
        date = _date_from_url(url)
        return ParsedFatwa(
            question=q_text,
            answer=a_text,
            source=SOURCE_NAME,
            url=canonical_url(url),
            category=category,
            date=date,
        )


def _category_from_links(root: Tag | BeautifulSoup) -> str | None:
    for a in root.select('a[href*="/questions/"]'):
        t = normalize_text(a.get_text())
        if t and len(t) < 200:
            return t
    return None


def _date_from_url(url: str) -> str | None:
    # .../slug/17-01-2026
    m = re.search(r"/(\d{2}-\d{2}-\d{4})/?$", urlparse(url).path)
    return m.group(1) if m else None
