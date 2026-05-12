"""
Darul Ifta Al-Ikhlas — ASP.NET; published fatawa live under category.aspx and article.aspx.

The ask form (askquestionurdu.aspx) is not scraped as Q&A. We crawl category listing
pages discovered from /articles/ and known category IDs, then detail links.
Selenium not used: listings are server-rendered HTML.
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from mufti_scraper.cleaning import canonical_url, html_to_clean_text, normalize_text
from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.robots import RobotsCache
from mufti_scraper.sources.base import ParsedFatwa

logger = logging.getLogger(__name__)

BASE = "https://alikhlasonline.com"
SOURCE_NAME = "Darul Ifta Al-Ikhlas (Al-Ikhlas Online)"

# Probe a moderate range first to avoid connection resets while still covering
# far beyond the old 1..40 window.
DEFAULT_CATEGORY_IDS = list(range(1, 201))

# Anchor-text and href hints that flag a "next page" link in a category list.
_NEXT_TEXT_HINTS: tuple[str, ...] = ("اگلا", "next", ">>", "›", "»")
_PAGE_QS_RE = re.compile(r"[?&]page=\d+", re.I)
# ASP.NET style homepage paths a missing category id often redirects to.
_HOMEPAGE_PATH_SUFFIXES: tuple[str, ...] = (
    "/default.aspx",
    "/index.aspx",
    "/home.aspx",
)

# Selectors confirmed via browser inspection of alikhlasonline.com fatawa.
_NOISE_SELECTOR = (
    ".hidden-print, .col-md-4.text-center.hidden-print, "
    ".container.hidden-print, footer, header, nav, "
    "script, style, noscript"
)

# Main content containers, tried in order.
_CONTAINER_SELECTORS: tuple[str, ...] = (
    "#printthis",
    ".section-to-print",
    ".col-md-8.minHeight",
)

# Detail anchor pattern. We collect only ``detail.aspx?id=`` URLs because the
# legacy ``article.aspx?id=`` pages use a different layout that doesn't carry
# the confirmed ``<b class='text-danger'>سوال:</b>`` markers. Restricting the
# filter keeps URL collection aligned with what ``parse_page`` can extract.
_DETAIL_HREF_RE = re.compile(r"detail\.aspx\?id=", re.I)

# Question / answer label markers carried inside a leading ``<b
# class='text-danger'>`` element on each fatwa paragraph.
_Q_LABEL = "سوال"
_A_LABEL = "جواب"

# Breadcrumb root labels that should be ignored as categories. The site
# breadcrumb on detail pages typically contains a single anchor pointing at
# the homepage, so accepting it would mislabel every fatwa as "Home".
_GENERIC_CATEGORY_TERMS: frozenset[str] = frozenset(
    {"home", "index", "main", "صفحہ اول", "خانہ"}
)


class AlIkhlasSource:
    name = "alikhlas"

    def __init__(self) -> None:
        # Article URL -> category name. Populated while crawling listing pages
        # so ``parse_page`` (which only sees html + url) can attribute a
        # ParsedFatwa to its source category.
        self._category_by_url: dict[str, str] = {}

    def _get_probe_valid(self, client: PoliteHttpClient, url: str) -> bool:
        """Lightweight GET probe for category id viability.

        The host blocks many HEAD requests, so we validate via GET and accept
        only responses that look like real listing pages:
        * HTTP 200
        * Body length > 500 chars (short pages are usually redirects/errors)
        """
        try:
            r = client.get(url, timeout=10)
        except Exception as e:
            logger.debug("Al-Ikhlas GET probe %s failed: %s", url, e)
            return False
        return r.status_code == 200 and len(r.text) > 500

    def iter_detail_urls(
        self,
        client: PoliteHttpClient,
        robots: RobotsCache,
        limit: int | None,
    ) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []

        # ``/articles/`` is mined below for category links but never walked
        # for fatwa anchors — its links use the legacy ``article.aspx`` shape
        # that ``parse_page`` cannot reliably extract.
        list_pages: list[str] = []

        # Discover extra category links from articles index. These are
        # explicitly linked from the site so we trust them without HEAD.
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

        # GET-filter the synthesized id range because HEAD is blocked here.
        kept = 0
        skipped = 0
        probes = 0
        for cid in DEFAULT_CATEGORY_IDS:
            cu = canonical_url(f"{BASE}/category.aspx?id={cid}&lang=1")
            if cu in list_pages:
                continue
            if not robots.can_fetch(cu):
                continue
            probes += 1
            if self._get_probe_valid(client, cu):
                list_pages.append(cu)
                kept += 1
            else:
                skipped += 1
            # Additional pacing to reduce connection resets during probe bursts.
            if probes % 10 == 0:
                time.sleep(2)
        logger.info(
            "Al-Ikhlas: GET probe kept=%d skipped=%d (range 1..%d)",
            kept,
            skipped,
            DEFAULT_CATEGORY_IDS[-1],
        )

        # Walk each listing root and exhaust its pagination before moving on.
        for lp in list_pages:
            if limit is not None and len(out) >= limit:
                break
            self._walk_listing(client, robots, lp, seen, out, limit)

        return out

    def _walk_listing(
        self,
        client: PoliteHttpClient,
        robots: RobotsCache,
        root: str,
        seen: set[str],
        out: list[str],
        limit: int | None,
    ) -> None:
        """Walk ``root`` and any next-page links, collecting article URLs.

        All article URLs harvested from this listing inherit the category
        name extracted from the first reachable page in the chain.
        """
        category_name: str | None = None
        visited: set[str] = set()
        queue: list[str] = [canonical_url(root)]

        while queue:
            if limit is not None and len(out) >= limit:
                return
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            if not robots.can_fetch(cur):
                continue
            try:
                r = client.get(cur)
                r.raise_for_status()
            except Exception as e:
                logger.warning("Al-Ikhlas list %s: %s", cur, e)
                continue
            soup = BeautifulSoup(r.text, "lxml")
            if category_name is None:
                category_name = _extract_category_name(soup)

            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                if not _DETAIL_HREF_RE.search(href):
                    continue
                full = canonical_url(urljoin(BASE, href))
                # Cross-category dedupe: an article linked from two
                # categories is collected once; the first category wins.
                if full in seen:
                    continue
                seen.add(full)
                out.append(full)
                if category_name:
                    self._category_by_url[full] = category_name
                if limit is not None and len(out) >= limit:
                    return

            for nxt in _find_next_page_urls(soup, cur):
                if nxt not in visited:
                    queue.append(nxt)

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        soup = BeautifulSoup(html, "lxml")

        # Breadcrumb sits inside ``nav`` on this template, so capture it
        # *before* the noise stripper drops nav/header/footer blocks.
        cat_el = soup.select_one(".breadcrumb a:last-of-type")
        breadcrumb_cat = normalize_text(cat_el.get_text()) if cat_el else ""

        _strip_noise(soup)

        # Locate the printable fatwa block; fall back to the full document
        # so legacy templates without the print container still parse.
        container: Tag | BeautifulSoup = soup
        for sel in _CONTAINER_SELECTORS:
            found = soup.select_one(sel)
            if found is not None:
                container = found
                break

        q_text, a_text = _extract_label_qa(container)

        # Legacy fallbacks: try previously-supported generic selectors so we
        # don't regress if a fatwa page uses an older template.
        if not q_text:
            q_text = _select_first_text(
                container,
                (".question", "#question", ".sawal", "[class*='question']"),
            )
        if not a_text:
            a_text = _select_first_text(
                container,
                (".answer", "#answer", ".jawab", "[class*='answer']"),
            )
        if not q_text:
            h = soup.select_one("h1, h2, h3")
            q_text = normalize_text(h.get_text()) if h else ""
        if not a_text:
            target = (
                container
                if isinstance(container, Tag)
                else (
                    soup.select_one("#main, main, .content, form, body") or soup.body
                )
            )
            a_text = html_to_clean_text(str(target)) if target is not None else ""

        q_text = _dedupe_lines(q_text)
        a_text = _dedupe_lines(a_text)

        canonical = canonical_url(url)
        category: str | None = None
        if _is_meaningful_category(breadcrumb_cat):
            category = breadcrumb_cat[:512]
        if not category:
            listing_cat = self._category_by_url.get(canonical)
            if _is_meaningful_category(listing_cat):
                category = (listing_cat or "")[:512]

        if (
            not q_text
            or not a_text
            or len(q_text.strip()) < 20
            or len(a_text.strip()) < 50
            or q_text == a_text
        ):
            logger.debug(
                "AlIkhlas parse failed: url=%s q=%d a=%d",
                url,
                len(q_text or ""),
                len(a_text or ""),
            )
            return None

        logger.debug(
            "AlIkhlas parsed OK: url=%s cat=%s q=%d a=%d",
            url,
            category,
            len(q_text),
            len(a_text),
        )
        return ParsedFatwa(
            question=q_text,
            answer=a_text,
            source=SOURCE_NAME,
            url=canonical,
            category=category,
            date=None,
        )


def _is_homepage_redirect(location: str, base: str = BASE) -> bool:
    """True if ``Location`` value points to the site root or a known landing."""
    if not location:
        return True
    loc = location.strip()
    if loc in ("/", base, base + "/"):
        return True
    low = loc.lower()
    if any(low.endswith(suffix) for suffix in _HOMEPAGE_PATH_SUFFIXES):
        return True
    return False


def _extract_category_name(soup: BeautifulSoup) -> str | None:
    """Best-effort category name from an ``h1`` or the ``<title>`` tag."""
    h1 = soup.select_one("h1")
    if h1:
        t = normalize_text(h1.get_text())
        if t and len(t) < 200:
            return t
    title_el = soup.select_one("title")
    if title_el:
        t = normalize_text(title_el.get_text())
        # Strip trailing site-name fragments common in ASP.NET title tags.
        for sep in (" - ", " | ", " :: "):
            if sep in t:
                t = t.split(sep, 1)[0].strip()
                break
        if t and len(t) < 200:
            return t
    return None


def _find_next_page_urls(soup: BeautifulSoup, current_url: str) -> list[str]:
    """Anchors that look like pagination links on a category page."""
    out: list[str] = []
    seen_local: set[str] = set()
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        text = (a.get_text() or "").strip().lower()
        text_match = any(hint in text for hint in _NEXT_TEXT_HINTS)
        qs_match = bool(_PAGE_QS_RE.search(href))
        if not (text_match or qs_match):
            continue
        full = canonical_url(urljoin(current_url, href))
        if full in seen_local:
            continue
        seen_local.add(full)
        out.append(full)
    return out


def _select_first_text(
    root: Tag | BeautifulSoup, selectors: tuple[str, ...]
) -> str:
    """Return normalized text for the first selector that matches non-empty content."""
    for sel in selectors:
        el = root.select_one(sel)
        if el is None:
            continue
        t = normalize_text(el.get_text())
        if t:
            return t
    return ""


def _strip_noise(soup: BeautifulSoup) -> None:
    """Remove sidebars, share widgets, print buttons, and chrome."""
    for tag in soup.select(_NOISE_SELECTOR):
        tag.decompose()


def _is_meaningful_category(text: str | None) -> bool:
    """True when ``text`` looks like a real fatwa category, not a root link."""
    if not text:
        return False
    t = text.strip()
    if len(t) < 4:
        return False
    if t.lower() in _GENERIC_CATEGORY_TERMS:
        return False
    return True


def _dedupe_lines(text: str) -> str:
    """Drop duplicate non-empty lines while preserving original order."""
    if not text:
        return ""
    seen: set[str] = set()
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return "\n".join(out)


def _extract_label_qa(container: Tag | BeautifulSoup) -> tuple[str, str]:
    """Extract (question, answer) using the ``UrduTextNafeesPara`` markup.

    The fatwa page wraps each section in ``<p class='UrduTextNafeesPara'>``
    with a leading ``<b class='text-danger'>سوال:</b>`` or
    ``<b class='text-danger'>جواب:</b>`` label. We decompose the bold label
    in-place, then read the remaining paragraph text. For the answer, all
    following ``<p>`` siblings are concatenated since longer fatawa span
    multiple paragraphs.
    """
    q_text = ""
    a_text = ""

    paragraphs = container.find_all("p", class_="UrduTextNafeesPara")
    for p in paragraphs:
        bold = p.find("b", class_="text-danger")
        if bold is None:
            continue
        label = bold.get_text() or ""

        if not q_text and _Q_LABEL in label:
            bold.decompose()
            q_text = normalize_text(p.get_text())
        elif not a_text and _A_LABEL in label:
            bold.decompose()
            answer_parts = [normalize_text(p.get_text())]
            for sibling in p.find_next_siblings("p"):
                t = normalize_text(sibling.get_text())
                if t and len(t) > 5:
                    answer_parts.append(t)
            a_text = "\n\n".join(part for part in answer_parts if part)

        if q_text and a_text:
            break

    return q_text, a_text
