"""
Jamia Binoria (Banuri) — static HTML; requests + BeautifulSoup suffice.

Listing: ``/new-questions`` paginated via ``…/page/N`` (canonical on this WP
install); ``?paged=N`` kept as fallback. Older ``?page=`` repeats page 1 and
must not be tried before ``…/page/N``.
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
# Single known listing index; ``/fatwa-archive`` returned 404 (May 2026).
BANURI_LIST_PATHS: tuple[str, ...] = ("/new-questions",)
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

# Site chrome / widgets / navigation we *always* drop before extraction —
# confirmed via browser inspection of banuri.edu.pk fatwa permalinks.
_NOISE_SELECTOR = (
    "div.sidebar, aside, div.social-icons, "
    "div.share-buttons, div.related-questions, "
    "header, footer, nav, script, style, noscript"
)

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


def _listing_url_candidates(path: str, page: int) -> list[str]:
    """WordPress pagination URL shapes (Banuri uses ``path/page/N`` without a
    trailing slash; ``path/page/N/`` is 404; ``?page=`` echoes page 1).
    """
    p = path.rstrip("/") or path
    stem = f"{BASE}{p}"
    if page <= 1:
        return [stem]
    return [
        f"{stem}/page/{page}",
        f"{stem}/page/{page}/",
        f"{stem}?paged={page}",
        f"{stem}?page={page}",
    ]


def _strip_safe(soup: BeautifulSoup) -> None:
    """Remove only non-visible / executable clutter; keep layout chrome."""
    for tag in soup.select(_SAFE_STRIP_SELECTORS):
        tag.decompose()


def _strip_noise(soup: BeautifulSoup) -> None:
    """Drop site chrome and widget blocks before any text extraction."""
    for tag in soup.select(_NOISE_SELECTOR):
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
        # Banuri pagination has occasional gaps where a listing page yields no
        # new /readquestion/ links; tolerate up to 15 consecutive empty pages
        # before giving up so we don't truncate the crawl prematurely.
        max_stale = 15

        for list_path in BANURI_LIST_PATHS:
            stale = 0
            page = 1
            logger.info("Banuri listing: trying path=%s", list_path)
            while stale < max_stale:
                candidates = _listing_url_candidates(list_path, page)
                fetched: str | None = None
                winning_url: str | None = None
                for list_url in candidates:
                    if not robots.can_fetch(list_url):
                        logger.info("robots.txt disallows %s", list_url)
                        continue
                    try:
                        headers = self._next_headers()
                        if headers:
                            r = client.get(list_url, headers=headers)
                        else:
                            r = client.get(list_url)
                        r.raise_for_status()
                        fetched = r.text
                        winning_url = list_url
                        break
                    except Exception as e:
                        logger.debug(
                            "Banuri list fetch skip url=%s err=%s", list_url, e,
                        )
                        continue
                if fetched is None:
                    logger.info(
                        "Banuri: no listing response for path=%s page=%d — "
                        "advancing stale counter",
                        list_path,
                        page,
                    )
                    stale += 1
                    page += 1
                    if page > 5000:
                        break
                    continue

                soup = BeautifulSoup(fetched, "lxml")
                search_root: Tag | BeautifulSoup = soup
                for sel in _LISTING_CONTAINER_SELECTORS:
                    container_sel = soup.select_one(sel)
                    if container_sel is not None:
                        search_root = container_sel
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
                        "Banuri progress: path=%s via=%s page=%d collected=%d "
                        "stale=%d/%d",
                        list_path,
                        winning_url,
                        page,
                        len(out),
                        stale,
                        max_stale,
                    )
                page += 1
                if page > 5000:
                    break

            if limit is not None and len(out) >= limit:
                return out

        return out

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        logger.debug("Banuri raw HTML length: %d", len(html))

        soup = BeautifulSoup(html, "lxml")
        # Strip widgets/sidebars first so selectors below never see noise,
        # then drop iframes/scripts to mirror prior _strip_safe behaviour.
        _strip_noise(soup)
        _strip_safe(soup)

        container: Tag | BeautifulSoup = soup.select_one("div.question-details") or soup

        # Step 1 — confirmed selectors.
        q_el = container.select_one("div.question-text")
        a_el = container.select_one("div.answer-text")
        q_text = normalize_text(q_el.get_text(separator="\n")) if q_el else ""
        a_text = normalize_text(a_el.get_text(separator="\n")) if a_el else ""

        # Step 2 — fall back to the legacy سوال/جواب marker walk on the same
        # container so we don't bleed unrelated page text into the answer.
        if not q_text or not a_text:
            container_text = container.get_text(separator="\n")
            lines = [line.strip() for line in container_text.split("\n") if line.strip()]
            legacy_q, legacy_a = _extract_marker_q_a(lines)
            if not q_text and legacy_q:
                q_text = legacy_q
            if not a_text and legacy_a:
                a_text = legacy_a

        # Step 3 — last-resort fillers (kept from previous behaviour).
        if not q_text:
            q_text = _title_fallback(soup)
        if not a_text or len(a_text) < 20:
            paragraph_root: Tag | BeautifulSoup = (
                container if isinstance(container, Tag) else soup
            )
            lp = _longest_paragraph(paragraph_root)
            if len(lp) > len(a_text):
                a_text = lp

        q_text = _dedupe_lines(normalize_text(q_text))
        a_text = _dedupe_lines(normalize_text(a_text))

        # Step 4 — category from the confirmed info block, with legacy
        # fallbacks so older permalinks remain searchable.
        category: str | None = None
        cat_el = container.select_one("div.question-info span.category")
        if cat_el is not None:
            category = normalize_text(cat_el.get_text()) or None
        if not category:
            link_el = container.select_one(".cat-links a")
            if link_el is not None:
                category = normalize_text(link_el.get_text()) or None
        if not category:
            root = soup.body if soup.body is not None else soup
            category = _category_from_links(root)
        if category:
            category = category[:512]

        # Step 5 — date from the confirmed info span, then the URL tail.
        date_el = container.select_one("div.question-info span.date")
        date = normalize_text(date_el.get_text()) if date_el else None
        if not date:
            date = _date_from_url(url)

        if (
            not q_text
            or not a_text
            or len(q_text.strip()) < 20
            or len(a_text.strip()) < 50
            or q_text == a_text
        ):
            logger.debug(
                "Banuri parse failed: url=%s q=%d a=%d",
                url,
                len(q_text or ""),
                len(a_text or ""),
            )
            return None

        logger.debug(
            "Banuri parsed OK: url=%s cat=%s q=%d a=%d",
            url,
            category,
            len(q_text),
            len(a_text),
        )
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
