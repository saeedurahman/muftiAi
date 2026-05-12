"""
Jamia Darul Uloom Karachi scraper for darulifta.info.

The old darululoomkarachi.edu.pk source returns 403, so this source targets:

* Listing: https://darulifta.info/d/darululoomkarachi?page={N}
* Detail:  https://darulifta.info/d/darululoomkarachi/fatwa/{short_id}/{slug}

Detail pages embed the answer as a PDF.js iframe; text answers are extracted
from the linked PDF with pdfplumber.
"""

from __future__ import annotations

import io
import logging
import re
import time
import urllib.parse
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from mufti_scraper.cleaning import canonical_url, normalize_text
from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.robots import RobotsCache
from mufti_scraper.sources.base import ParsedFatwa

logger = logging.getLogger(__name__)

# pdfplumber uses pdfminer internally, which can emit extremely noisy DEBUG
# traces under the scraper's global logging config.
for _noisy in (
    "pdfminer",
    "pdfminer.psparser",
    "pdfminer.pdfinterp",
    "pdfminer.cmapdb",
    "pdfplumber",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

BASE_URL = "https://darulifta.info"
LISTING_BASE = "https://darulifta.info/d/darululoomkarachi"
SOURCE_NAME = "Jamia Darul Uloom Karachi"
MAX_PAGES = 42
PER_PAGE = 25

_FATWA_LINK_RE = re.compile(r"/d/darululoomkarachi/fatwa/")
_PDF_FILE_RE = re.compile(r"[?&]file=([^&]+)")
_DATE_RE = re.compile(r"\d{2}[-/]\d{2}[-/]\d{4}|\d{4}[-/]\d{2}[-/]\d{2}")
_FATWA_NUMBER_RE = re.compile(r"فتویٰ\s*نمبر[:\s]+([0-9/]+)")
_ARABIC_SCRIPT_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")

_NOISE_SELECTOR = (
    "header, footer, nav, script, style, noscript, "
    "._m_widget, div.grid.grid-cols-1.md\\:grid-cols-2"
)


class KarachiSource:
    name = "karachi"

    def __init__(self, max_depth: int = 3) -> None:
        # Kept for registry compatibility. The darulifta.info source uses a
        # fixed paginated listing instead of depth-based crawling.
        _ = max_depth
        self._client: PoliteHttpClient | None = None

    def iter_detail_urls(
        self,
        client: PoliteHttpClient,
        robots: RobotsCache,
        limit: int | None,
    ) -> list[str]:
        """Collect detail URLs from the confirmed darulifta.info listing."""
        self._client = client
        seen: set[str] = set()
        out: list[str] = []
        max_urls = limit or 99999

        for page_num in range(1, MAX_PAGES + 1):
            if len(out) >= max_urls:
                break

            listing_url = (
                LISTING_BASE
                if page_num == 1
                else f"{LISTING_BASE}?page={page_num}"
            )
            if not robots.can_fetch(listing_url):
                logger.warning("Karachi robots blocked: %s", listing_url)
                break

            try:
                r = client.get(listing_url)
                r.raise_for_status()
            except Exception as e:
                logger.warning("Karachi listing page=%d failed: %s", page_num, e)
                break

            soup = BeautifulSoup(r.text, "lxml")
            links: list[str] = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not _FATWA_LINK_RE.search(href):
                    continue
                canon = canonical_url(urljoin(BASE_URL, href))
                if canon in seen:
                    continue
                seen.add(canon)
                links.append(canon)

            if not links:
                logger.info(
                    "Karachi: page=%d returned 0 links - stopping", page_num
                )
                break

            for url in links:
                if len(out) >= max_urls:
                    break
                out.append(url)

            logger.info(
                "Karachi: page=%d links=%d total=%d",
                page_num,
                len(links),
                len(out),
            )

            if page_num < MAX_PAGES and len(out) < max_urls:
                time.sleep(1.5)

        return out

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        soup = BeautifulSoup(html, "lxml")
        _strip_noise(soup)

        q_text = _extract_question(soup)
        pdf_url = self._get_pdf_url(soup, BASE_URL)

        a_text = ""
        if pdf_url:
            logger.debug("Karachi: fetching PDF %s", pdf_url)
            a_text = self._extract_pdf_text(pdf_url, self._client)
        else:
            logger.warning("Karachi: no PDF found for %s", url)

        category = _extract_category(soup)
        date_text = _extract_date(soup)
        fatwa_num = _extract_fatwa_number(soup)

        q_text = _dedupe_lines(q_text)
        a_text = _dedupe_lines(a_text)

        if len(q_text.strip()) < 10:
            logger.debug(
                "Karachi: question too short url=%s q=%d",
                url,
                len(q_text),
            )
            return None

        if len(a_text.strip()) < 30:
            if pdf_url and not a_text:
                logger.info(
                    "Karachi: PDF has no extractable text (likely scanned image) url=%s pdf=%s",
                    url,
                    pdf_url,
                )
            else:
                logger.debug(
                    "Karachi: answer too short url=%s a=%d pdf=%s",
                    url,
                    len(a_text),
                    pdf_url,
                )
            return None

        if not _looks_readable_urdu(a_text):
            logger.info(
                "Karachi: PDF text is not readable Urdu; skipping url=%s pdf=%s chars=%d",
                url,
                pdf_url or "<none>",
                len(a_text),
            )
            return None

        logger.debug(
            "Karachi parsed OK: url=%s cat=%s q=%d a=%d fatwa=%s",
            url,
            category,
            len(q_text),
            len(a_text),
            fatwa_num,
        )

        return ParsedFatwa(
            question=q_text,
            answer=a_text,
            source=SOURCE_NAME,
            url=canonical_url(url),
            category=category,
            date=date_text or None,
        )

    def _extract_pdf_text(
        self, pdf_url: str, client: PoliteHttpClient | None
    ) -> str:
        """Download PDF and extract text using pdfplumber."""
        try:
            import pdfplumber  # type: ignore
        except ImportError:
            logger.error("pdfplumber not installed. Run: pip install pdfplumber")
            return ""

        try:
            if client is not None:
                r = client.get(pdf_url)
            else:
                import requests  # type: ignore

                r = requests.get(pdf_url, timeout=30)
            r.raise_for_status()

            with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                text_parts: list[str] = []
                for page in pdf.pages:
                    text = page.extract_text()
                    if text and text.strip():
                        text_parts.append(text.strip())

            return normalize_text("\n\n".join(text_parts))
        except Exception as e:
            logger.warning(
                "Karachi PDF extract failed url=%s error=%s",
                pdf_url,
                e,
            )
            return ""

    def _get_pdf_url(self, soup: BeautifulSoup, base_url: str) -> str:
        """Extract PDF URL from iframe or direct link."""
        iframe = soup.select_one("iframe[src*='viewer.html']")
        if iframe is not None:
            iframe_src = iframe.get("src", "")
            match = _PDF_FILE_RE.search(iframe_src)
            if match:
                pdf_url = urllib.parse.unquote(match.group(1))
                if pdf_url.startswith("http"):
                    return pdf_url

        pdf_link = soup.select_one("a[href*='.pdf']")
        if pdf_link is not None:
            href = pdf_link.get("href", "")
            if href:
                return urljoin(base_url, href)

        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "")
            if "pdf" not in src.lower() and "files.darulifta" not in src:
                continue
            match = _PDF_FILE_RE.search(src)
            if match:
                pdf_url = urllib.parse.unquote(match.group(1))
                if pdf_url:
                    return urljoin(base_url, pdf_url)

        return ""


def _strip_noise(soup: BeautifulSoup) -> None:
    for tag in soup.select(_NOISE_SELECTOR):
        tag.decompose()


def _extract_question(soup: BeautifulSoup) -> str:
    # User-confirmed selector. Some live pages currently render the same
    # classes on a div, so keep that as the first fallback without changing
    # the public behavior.
    q_el = soup.select_one("h1.text-lg.font-bold") or soup.select_one("h1")
    if q_el is None:
        q_el = soup.select_one("div.text-lg.font-bold.leading-relaxed")
    if q_el is None:
        q_el = soup.select_one("main div.text-lg.font-bold")
    q_text = normalize_text(q_el.get_text()) if q_el else ""

    if len(q_text) < 10:
        h4 = soup.select_one("h4")
        if h4 is not None:
            q_text = normalize_text(h4.get_text())

    if len(q_text) < 10:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og is not None:
            q_text = normalize_text(og.get("content") or "")

    return q_text


def _extract_category(soup: BeautifulSoup) -> str:
    section = soup.find("meta", attrs={"property": "article:section"})
    if section is not None:
        category = normalize_text(section.get("content") or "")
        if category:
            return category[:512]

    for a in soup.select("main a[href*='/d/']"):
        text = normalize_text(a.get_text())
        if text and len(text) > 3 and "دار" not in text:
            return text[:512]

    return "karachi"


def _extract_date(soup: BeautifulSoup) -> str:
    pub = soup.find("meta", attrs={"property": "article:published_time"})
    if pub is not None:
        content = (pub.get("content") or "").strip()
        if content:
            return content[:32]

    for span in soup.select("main span, main div.flex span"):
        text = span.get_text(strip=True)
        if _DATE_RE.search(text):
            return text.strip()

    return ""


def _extract_fatwa_number(soup: BeautifulSoup) -> str:
    match = _FATWA_NUMBER_RE.search(soup.get_text(" ", strip=True))
    return match.group(1).strip() if match else ""


def _dedupe_lines(text: str) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            result.append(line)
    return "\n".join(result)


def _looks_readable_urdu(text: str) -> bool:
    """Reject pdfplumber glyph dumps before they reach the database.

    Many Karachi PDFs are scanned images or use fonts without a usable Unicode
    map. pdfplumber can still emit long strings, but they look like
    ``J.,,..--`` / ``yl~I`` rather than Urdu. A valid answer should contain a
    meaningful amount of Arabic-script text and not be dominated by Latin
    glyph fragments.
    """
    compact = text.strip()
    if len(compact) < 30:
        return False

    arabic_chars = len(_ARABIC_SCRIPT_RE.findall(compact))
    latin_chars = len(_LATIN_RE.findall(compact))
    alpha_chars = arabic_chars + latin_chars
    if arabic_chars < 40:
        return False
    if alpha_chars and arabic_chars / alpha_chars < 0.45:
        return False
    if latin_chars > arabic_chars:
        return False

    return True
