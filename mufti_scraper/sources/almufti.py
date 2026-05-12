"""
Al Mufti Online (Jamia Tur Rasheed) — WordPress-style URLs.

Homepage and category archives list posts: /YYYY/MM/DD/id/
Category pages paginate: /category/fatwa/{id}/page/N/
Selenium not required: HTML contains post links.
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

BASE = "https://almuftionline.com"
SOURCE_NAME = "Al Mufti Online (Jamia Tur Rasheed)"

# Date-based WordPress permalinks. The slug variant ``[^/]+`` also covers the
# older numeric-id style (``\d+``) so a single regex captures both shapes.
_POST_PATH_RE = re.compile(r"^/\d{4}/\d{2}/\d{2}/[^/]+/?$")
# Default WordPress post identifiers when pretty permalinks are off.
_QS_POST_KEYS: tuple[str, ...] = ("p", "page_id")

# Sawal/Jawab markers — matched explicitly so robust against layout drift.
# Question-section starts (most-specific first):
_Q_PREFIXES: tuple[str, ...] = ("سوال نمبر", "سوال:", "سوال")
# Answer-section starts that we look for *anywhere* in a paragraph:
_A_CONTAINS: tuple[str, ...] = ("الجواب حامداً", "الجواب")
# Answer-section starts that must be at the *start* of a paragraph:
_A_PREFIXES: tuple[str, ...] = ("جواب:", "جواب")
# End-of-answer markers — once seen, stop appending (mufti signature/footer
# typically follows).
_A_END_MARKERS: tuple[str, ...] = ("واللہ اعلم", "والله أعلم")

# Selectors and markers confirmed via browser inspection of
# almuftionline.com fatwa permalinks (April 2026).
_NOISE_SELECTOR = (
    "header, footer, nav, .sidebar, #sidebar, "
    ".social-share, .share-buttons, .post-navigation, "
    ".related-posts, .comments-area, script, style, noscript"
)

# Answer-section start markers in priority order. The most-specific
# (Bismillah-prefixed) form is checked first so we never split inside the
# leading invocation.
_ANSWER_MARKERS: tuple[str, ...] = (
    "اَلجَوَابْ بِاسْمِ مُلْہِمِ الصَّوَابْ",
    "اَلجَوَابُ",
    "الجواب",
    "جواب",
    "اَلجَوَابْ",
)

_QUESTION_PREFIX_RE = re.compile(r"^سوال\s*[:：]?\s*")

# Strict date+numeric-id post pattern used for collection-summary logging.
_FATWA_POST_RE = re.compile(r"/\d{4}/\d{2}/\d{2}/\d+")


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

        # Phase 1: WordPress XML sitemap. Pulls every post URL in one shot
        # without paying for archive pagination — typically dominates output.
        try:
            sm_urls = _discover_via_sitemap(client, robots)
        except Exception as e:
            logger.warning("Al Mufti sitemap discovery failed: %s", e)
            sm_urls = []
        sitemap_total = len(sm_urls)
        for u in sm_urls:
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
            if limit is not None and len(out) >= limit:
                logger.info(
                    "Al Mufti URL collection: sitemap=%d kept=%d (limit reached)",
                    sitemap_total,
                    len(out),
                )
                return out
        logger.info(
            "Al Mufti URL collection (sitemap phase): sitemap=%d kept=%d",
            sitemap_total,
            len(out),
        )

        # Phase 2: archive walks. Picks up anything missing from the sitemap
        # (recent posts not yet indexed, custom permalinks, etc).
        raw = self._discover_category_urls(client, robots)
        # Always include the homepage and the bare archive paths as seeds.
        raw.append(BASE + "/")
        raw.append(BASE + "/fatawa/")
        raw.append(BASE + "/category/fatwa/")
        if len(raw) <= 1:
            raw.insert(0, f"{BASE}/category/fatwa/19/")
        seeds = list(dict.fromkeys(canonical_url(s) for s in raw))

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
        # Mine category links from the homepage AND the two main archive
        # roots; each one tends to expose a different subset of categories.
        discovery_seeds = (
            BASE + "/",
            BASE + "/fatawa/",
            BASE + "/category/fatwa/",
        )
        cats: set[str] = set()
        for home in discovery_seeds:
            if not robots.can_fetch(home):
                continue
            try:
                r = client.get(home)
                r.raise_for_status()
            except Exception as e:
                logger.warning("Al Mufti seed %s failed: %s", home, e)
                continue
            soup = BeautifulSoup(r.text, "lxml")
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
        # WP archives sometimes have a few empty pages in a row between
        # valid ones; bumped from 3 to 8 to ride out those gaps.
        max_stale = 8
        while stale < max_stale:
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
                if _is_post_url(full):
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
        # Strip site chrome and tracking widgets *before* any extraction so
        # selectors below never see navigation / share / sidebar text.
        _strip_noise(soup)

        title_el = soup.select_one("h1.entry-title, article h1, h1")
        title = normalize_text(title_el.get_text()) if title_el else ""

        category = _category_from_table(soup) or _category_from_article(soup)

        # Mufti name is metadata for now; only used as a category fallback
        # when no topical category is available, so downstream search/filter
        # still has a meaningful tag for the post.
        if not category:
            mufti_name = _mufti_name_from_table(soup) or _extract_mufti_name(soup)
            if mufti_name:
                category = f"Mufti: {mufti_name}"[:512]

        date = _date_from_meta(soup, url)

        content_div = (
            soup.select_one(".entry-content.pagelayer-post-excerpt > div:first-of-type")
            or soup.select_one(".entry-content > div:first-of-type")
            or soup.select_one(".entry-content")
        )

        q_text, a_text = "", ""
        if content_div is not None:
            q_text, a_text = _split_question_answer(content_div)

        if q_text:
            q_text = _QUESTION_PREFIX_RE.sub("", q_text).strip()

        q_text = _dedupe_lines(q_text)
        a_text = _dedupe_lines(a_text)

        # Safety net: if the marker split was too thin, retry the legacy
        # paragraph-walk parser scoped to the same content container.
        if (
            (len(q_text) < 20 or len(a_text) < 50)
            and content_div is not None
        ):
            legacy_q, legacy_a = _split_sawal_jawab_wp(content_div)
            legacy_q = _dedupe_lines(_QUESTION_PREFIX_RE.sub("", legacy_q).strip())
            legacy_a = _dedupe_lines(legacy_a)
            if len(legacy_q) >= 20 and len(legacy_a) >= 50:
                q_text, a_text = legacy_q, legacy_a

        if not q_text and title:
            q_text = title

        if (
            not q_text
            or not a_text
            or len(q_text.strip()) < 20
            or len(a_text.strip()) < 50
            or q_text == a_text
        ):
            logger.debug(
                "AlMufti parse failed: url=%s q_len=%d a_len=%d",
                url,
                len(q_text or ""),
                len(a_text or ""),
            )
            return None

        logger.debug(
            "AlMufti parsed OK: url=%s category=%s q_len=%d a_len=%d",
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


def _strip_noise(soup: BeautifulSoup) -> None:
    """Remove site chrome and widgets before any text extraction."""
    for tag in soup.select(_NOISE_SELECTOR):
        tag.decompose()


def _category_from_article(soup: BeautifulSoup) -> str | None:
    for a in soup.select('a[rel="category tag"], .cat-links a, a[href*="/category/fatwa/"]'):
        t = normalize_text(a.get_text())
        if t:
            return t[:512]
    return None


def _category_from_table(soup: BeautifulSoup) -> str | None:
    """Read category / sub-category from the leading wp-block-table figure.

    The header table on almuftionline posts has the shape::

        | post-id | category | sub-category |

    We combine category and sub-category with an em-dash when both are
    present and distinct, otherwise return whichever exists.
    """
    cat_el = soup.select_one(
        "figure.wp-block-table:first-of-type tr td:nth-child(2)"
    )
    sub_el = soup.select_one(
        "figure.wp-block-table:first-of-type tr td:nth-child(3)"
    )
    cat = normalize_text(cat_el.get_text()) if cat_el else ""
    sub = normalize_text(sub_el.get_text()) if sub_el else ""

    if cat and sub and sub != cat:
        combined = f"{cat} — {sub}"
        return combined[:512]
    if cat:
        return cat[:512]
    if sub:
        return sub[:512]
    return None


def _mufti_name_from_table(soup: BeautifulSoup) -> str | None:
    """Read the answering mufti from the trailing wp-block-table figure."""
    el = soup.select_one(
        "figure.wp-block-table:last-of-type tr:nth-child(1) td:nth-child(2)"
    )
    if el is None:
        return None
    name = normalize_text(el.get_text())
    return name[:200] if name else None


def _split_question_answer(content_div: Tag) -> tuple[str, str]:
    """Split fatwa content into ``(question, answer)`` using known markers.

    The fatwa post stores question and answer in a single block separated
    by an Urdu/Arabic answer marker (e.g. ``الجواب``). We try the most
    specific marker first so we never split inside the leading invocation,
    and fall back to a 500-character heuristic if no marker is present.
    """
    full_text = normalize_text(content_div.get_text(separator="\n"))
    if not full_text:
        return "", ""

    for marker in _ANSWER_MARKERS:
        if marker in full_text:
            head, tail = full_text.split(marker, 1)
            q_text = normalize_text(head)
            a_text = normalize_text(marker + tail)
            return q_text, a_text

    return normalize_text(full_text[:500]), normalize_text(full_text[500:])


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


def _date_from_meta(soup: BeautifulSoup, url: str) -> str | None:
    """Return ISO-day date for the post.

    Order of preference:
    1. ``<meta property='article:published_time'>`` (most reliable on WP).
    2. ``<time datetime='...'>`` element if present.
    3. Date segment embedded in the permalink path.
    """
    meta = soup.select_one("meta[property='article:published_time']")
    if meta is not None:
        content = meta.get("content") or ""
        if isinstance(content, str) and len(content) >= 10:
            return content[:10]

    t = soup.select_one("time[datetime]")
    if t and t.get("datetime"):
        dt = t["datetime"]
        if isinstance(dt, str) and len(dt) >= 10:
            return dt[:10]

    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _split_sawal_jawab_wp(root: Tag) -> tuple[str, str]:
    """Split a WordPress fatwa post into question/answer text.

    Recognized markers (Urdu / Arabic):
    * Question starts: ``سوال نمبر``, ``سوال:``, ``سوال`` (prefix or exact match).
    * Answer starts: ``الجواب حامداً``, ``الجواب`` (anywhere in the paragraph)
      and ``جواب:``, ``جواب`` (paragraph prefix).
    * Answer end: ``واللہ اعلم`` / ``والله أعلم`` — once seen, stop appending
      so we don't bleed mufti signature / footer into the answer body.
    """
    q_parts: list[str] = []
    a_parts: list[str] = []
    mode: str | None = None
    answer_closed = False

    for el in root.find_all(["h2", "h3", "h4", "h5", "p", "div", "li", "td"]):
        text = normalize_text(el.get_text())
        if not text:
            continue

        if _is_q_marker(text):
            mode = "q"
            answer_closed = False
            if len(text) > 10:
                q_parts.append(text)
            continue

        if _is_a_marker(text):
            mode = "a"
            answer_closed = False
            if len(text) > 20:
                a_parts.append(text)
                if _is_a_end(text):
                    answer_closed = True
            continue

        if mode == "q":
            q_parts.append(text)
        elif mode == "a" and not answer_closed:
            a_parts.append(text)
            if _is_a_end(text):
                answer_closed = True

    return normalize_text("\n".join(q_parts)), normalize_text("\n".join(a_parts))


def _is_q_marker(text: str) -> bool:
    if text == "سوال":
        return True
    return any(text.startswith(p) for p in _Q_PREFIXES)


def _is_a_marker(text: str) -> bool:
    if any(needle in text for needle in _A_CONTAINS):
        return True
    if text == "جواب":
        return True
    return any(text.startswith(p) for p in _A_PREFIXES)


def _is_a_end(text: str) -> bool:
    return any(marker in text for marker in _A_END_MARKERS)


def _is_post_url(url: str) -> bool:
    """True if ``url`` matches a known Al Mufti post pattern.

    Recognized shapes:
    * Date-based permalinks ``/YYYY/MM/DD/<slug-or-id>/`` (slug variant
      includes the older numeric-id form as a strict subset).
    * Default WP query strings ``/?p=<id>`` and ``/?page_id=<id>``.
    """
    p = urlparse(url)
    if _POST_PATH_RE.match(p.path or ""):
        return True
    if not p.query:
        return False
    qs = parse_qs(p.query)
    for key in _QS_POST_KEYS:
        v = qs.get(key)
        if v and v[0] and v[0].isdigit():
            return True
    return False


def _discover_via_sitemap(
    client: PoliteHttpClient, robots: RobotsCache
) -> list[str]:
    """Pull post URLs from ``/sitemap.xml`` or ``/sitemap_index.xml``.

    A sitemap *index* (containing further ``<loc>`` entries that point to
    sub-sitemaps) is followed one level. URLs are filtered through
    :func:`_is_post_url` so we never emit category or page URLs here.
    """
    candidates = (f"{BASE}/sitemap.xml", f"{BASE}/sitemap_index.xml")
    out: list[str] = []
    seen: set[str] = set()
    for sm_url in candidates:
        if not robots.can_fetch(sm_url):
            continue
        try:
            r = client.get(sm_url)
            if r.status_code == 404:
                continue
            r.raise_for_status()
        except Exception as e:
            logger.info("Al Mufti sitemap %s failed: %s", sm_url, e)
            continue

        locs = _parse_sitemap_locs(r.text)
        sub_maps = [u for u in locs if u.lower().endswith(".xml")]
        page_urls = [u for u in locs if not u.lower().endswith(".xml")]

        for sm in sub_maps:
            if not robots.can_fetch(sm):
                continue
            try:
                rr = client.get(sm)
                rr.raise_for_status()
            except Exception as e:
                logger.warning("Al Mufti sub-sitemap %s: %s", sm, e)
                continue
            page_urls.extend(_parse_sitemap_locs(rr.text))

        for u in page_urls:
            full = canonical_url(u)
            if full in seen:
                continue
            seen.add(full)
            if _is_post_url(full):
                out.append(full)

    raw = len(seen)
    strict = sum(
        1
        for u in out
        if _FATWA_POST_RE.search(urlparse(u).path or "")
    )
    logger.info(
        "Al Mufti sitemap: %d urls discovered, %d kept as posts (%d filtered, "
        "%d match strict /YYYY/MM/DD/<id> pattern)",
        raw,
        len(out),
        raw - len(out),
        strict,
    )
    return out


def _parse_sitemap_locs(xml_text: str) -> list[str]:
    """Return text of every ``<loc>`` element in a sitemap XML document."""
    soup = BeautifulSoup(xml_text, "xml")
    return [
        normalize_text(loc.get_text())
        for loc in soup.find_all("loc")
        if loc.get_text()
    ]


def _extract_mufti_name(soup: BeautifulSoup) -> str | None:
    """Best-effort mufti / author name extraction from common WP themes."""
    el = soup.select_one(
        ".mufti-name, .author, .written-by, [class*='mufti']"
    )
    if el is None:
        return None
    t = normalize_text(el.get_text())
    if not t:
        return None
    return t[:200]
