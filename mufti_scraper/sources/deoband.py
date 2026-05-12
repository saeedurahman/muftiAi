"""
Darul Ifta Deoband — https://darulifta-deoband.com

Cloudflare requires ``cf_clearance`` (and the site expects ``ci_session``).
Cookies are loaded from ``deoband_cookies.json`` at the project root.

If listing/fetch sees 403 or a CF challenge page, SeleniumBase UC Mode runs
(at most once per run) to refresh cookies and writes them back to the JSON
file — same pattern as manual DevTools paste, automated when possible.

URL discovery:
    * **Mega menu** (full archive): ``ul.sub_level_menu li a`` → ``/home/qa_ur/{slug}/…``
      paginated with ``/page/N`` and numeric segments (distinct from chronological
      ``/home/ur/questions``).
    * Seeds: ``/home/ur``, homepage, ``/home/en/questions``
    * Pagination for legacy ``/home/ur/{cat}/page/{n}`` + questions hub

Parsing:
    * Multiple container probes, light vs aggressive stripping (``.widget`` kept;
      it often wraps fatwa markup)
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from mufti_scraper.cleaning import canonical_url, normalize_text
from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.robots import RobotsCache
from mufti_scraper.sources.base import ParsedFatwa

logger = logging.getLogger(__name__)

DEOBAND_BASE = "https://darulifta-deoband.com"
DEOBAND_HOST = "darulifta-deoband.com"
HOME_UR = f"{DEOBAND_BASE}/home/ur"
SOURCE_NAME = "Darul Ifta Deoband"

_COOKIE_FILE = Path(__file__).resolve().parent.parent.parent / "deoband_cookies.json"


def _url_host_variants(url: str) -> list[str]:
    """Try apex + ``www`` — listing HTML can differ slightly between hosts."""
    u = canonical_url(url)
    p = urlparse(u)
    h = (p.netloc or "").lower()
    if h.startswith("www."):
        h = h[4:]
    if h != DEOBAND_HOST:
        return [u]
    apex = canonical_url(urlunparse((p.scheme, DEOBAND_HOST, p.path, "", p.query, "")))
    www_u = canonical_url(urlunparse((p.scheme, f"www.{DEOBAND_HOST}", p.path, "", p.query, "")))
    return list(dict.fromkeys([apex, www_u]))


DEOBAND_CATEGORIES: tuple[str, ...] = (
    "namaz-prayer",
    "zakat-charity",
    "sawm-fasting",
    "nikah-marriage",
    "talaq-divorce",
    "hajj",
    "taharah",
    "muamlat",
    "aqeedah",
    "hadith",
    "quran",
    "misc",
    "salah-prayer",
    "tijarah",
)

# Hub paths that look like fatwas but are listing pages
_NON_FATWA_SLUGS: frozenset[str] = frozenset(
    {
        "questions",
        "search",
        "page",
        "contact",
        "about",
    }
)

# Internal reference lines in the fatwa body
_FATWA_REF_RE = re.compile(r"^Fatwa\s*:\s*\S", re.I)
_QUES_PREFIX_RE = re.compile(r"^\s*سوال\s*[:：۔]?\s*")
_QUES_NUM_SPLIT_RE = re.compile(r"\s*سوال\s*نمبر\s*[:：]?")

# Hard stops for answer body walking (/footer). Do not treat “Darul Ifta …” alone
# as a stop inside long paragraphs — that appears in nav and breaks extraction.
_SIGNATURE_STOP_LINES: tuple[str, ...] = ("واللہ تعالیٰ اعلم", "واللہ اعلم")

# Loose markers retained for coarse splitter / breadcrumbs only (not substring-kill).
_JAWAB_LINE_MARKERS: tuple[str, ...] = (
    "جواب نمبر",
    "الجواب",
    "جواب:",
    "Detailed Answer",
    "Answer:",
)

# Patterns embedded in markup (CodeIgniter `index.php/...`).
_RE_DEOBAND_DETAIL_ABS = re.compile(
    r"https?://(?:www\.)?darulifta-deoband\.com"
    r"(/(?:index\.php/)?home/(?:ur|en)/[\w.-]+/\d+)\b/?",
    re.I,
)
_RE_DEOBAND_DETAIL_REL = re.compile(
    r"(['\"])(/(?:index\.php/)?home/(?:ur|en)/[\w.-]+/\d+)\b/?(?=\1|[\s>#?]|$)",
    re.I,
)

_ANSWER_BODY_TAGS = frozenset(
    {"p", "div", "span", "section", "blockquote", "ul", "li", "article"},
)


def _strip_listing_noise(soup: BeautifulSoup, *, aggressive: bool) -> None:
    """Drop chrome; keep plausible fatwa wrappers (``.widget`` can hold fatwa markup)."""
    base = (
        "header, footer, nav, script, style, noscript, aside, iframe, svg"
    )
    for tag in soup.select(base):
        tag.decompose()
    if aggressive:
        for tag in soup.select(".sidebar, .social-share"):
            tag.decompose()


_MAX_CATEGORY_PAGES = 200
_MAX_QUESTIONS_HUB_PAGES = 200
_MAX_QA_UR_CATEGORY_PAGES = 900
_LIST_SLEEP_S = 1.5


def _path_segments_normalized(path: str) -> list[str]:
    raw = path or "/"
    if raw != "/" and raw.endswith("/"):
        raw = raw.rstrip("/") or "/"
    for _ in range(10):
        n = re.sub(r"^/index\.php(?=/)", "", raw, flags=re.I)
        if n == raw:
            break
        raw = (n.rstrip("/") or "/") or "/"
    return [seg for seg in raw.strip("/").split("/") if seg]


def _canonical_fatwa_detail_url(raw: str) -> str | None:
    """Normalise homepage / CI ``index.php`` permalinks to canonical detail URL."""
    if not raw or raw.strip().startswith("#"):
        return None
    joined = canonical_url(urljoin(DEOBAND_BASE, raw.strip()))
    pu = urlparse(joined)
    host = pu.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "darulifta-deoband.com":
        return None
    segs = _path_segments_normalized(pu.path or "/")
    if len(segs) != 4 or segs[0] != "home" or segs[1] not in {"ur", "en"}:
        return None
    slug, rid = segs[2], segs[3]
    if slug in _NON_FATWA_SLUGS or not rid.isdigit():
        return None
    out_path = f"/home/{segs[1]}/{slug}/{rid}"
    return canonical_url(
        urlunparse(("https", "darulifta-deoband.com", out_path, "", "", "")),
    )


def _is_detail_fatwa_url(url: str) -> bool:
    return _canonical_fatwa_detail_url(url) is not None


def _inject_indexphp(url: str) -> str:
    """Some CI themes resolve listings only under ``/index.php/home/...``."""
    pu = urlparse(url)
    if "index.php" in (pu.path or "").lower():
        return canonical_url(url)
    new_path = "/index.php" + (pu.path or "")
    return canonical_url(urlunparse((pu.scheme, pu.netloc, new_path, "", pu.query, "")))


def _expand_listing_url_variants(urls: list[str]) -> list[str]:
    """Each path: clean + ``index.php`` mirror (dedup)."""
    out: list[str] = []
    seen_k: set[str] = set()
    for raw in urls:
        base_u = canonical_url(urljoin(DEOBAND_BASE, raw)) if "://" not in raw else canonical_url(raw)
        for cand in (base_u, _inject_indexphp(base_u)):
            pu = urlparse(cand)
            key = pu.path.lower() + "?" + pu.query.lower()
            if key in seen_k:
                continue
            seen_k.add(key)
            out.append(cand)
    return out


def _load_cookies() -> dict | None:
    """Load Deoband cookies from ``deoband_cookies.json``."""
    if not _COOKIE_FILE.exists():
        logger.warning(
            "Deoband: cookie file not found at %s\n"
            "  Create deoband_cookies.json with cf_clearance and ci_session.\n"
            "  Chrome DevTools → Application → Cookies for %s",
            _COOKIE_FILE,
            DEOBAND_BASE,
        )
        return None
    try:
        with open(_COOKIE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("Deoband: cookie file invalid JSON: %s", e)
        return None
    except OSError as e:
        logger.error("Deoband: read cookie file: %s", e)
        return None

    for key in ("cf_clearance", "ci_session"):
        if key not in data or not data[key]:
            logger.warning("Deoband: missing/empty '%s' in cookie file", key)
            return None

    logger.info("Deoband: cookies loaded from %s", _COOKIE_FILE)
    return data


def _save_cookies(cookies: dict) -> None:
    """Write cookie dict back to disk (Unicode, indented). Errors are swallowed."""
    try:
        with open(_COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=4)
        logger.info("Deoband: cookies saved to %s", _COOKIE_FILE)
    except OSError as e:
        logger.warning("Deoband: could not save cookies: %s", e)


def _make_deoband_session(cookies: dict) -> requests.Session:
    """``requests.Session`` with domain cookies and realistic headers."""
    s = requests.Session()
    ua = cookies.get(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36",
    )
    s.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ur,en-US;q=0.9,en;q=0.8",
        "Referer": HOME_UR,
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    for name in ("cf_clearance", "ci_session"):
        if name in cookies and cookies[name]:
            s.cookies.set(name, str(cookies[name]), domain=".darulifta-deoband.com")
    return s


def _is_cf_challenge(html: str) -> bool:
    if not html:
        return True
    head = html[:5000]
    return any(
        p in head
        for p in (
            "Verify you are human",
            "Just a moment",
            "cf-browser-verification",
            "needs to review the security of your connection",
        )
    )


def _refresh_cookies_via_browser(
    *,
    reuse_ua: str | None,
    attempts: int = 3,
) -> dict | None:
    """SeleniumBase UC: open site, harvest ``cf_clearance`` + ``ci_session``."""
    try:
        from seleniumbase import SB  # type: ignore
    except ImportError:
        logger.warning(
            "Deoband: seleniumbase not installed — cannot auto-refresh cookies. "
            "pip install seleniumbase  OR  paste cookies into deoband_cookies.json",
        )
        return None

    for attempt in range(1, attempts + 1):
        logger.info(
            "Deoband: UC browser cookie refresh (%d/%d)...",
            attempt,
            attempts,
        )
        try:
            with SB(uc=True, headless=False, incognito=True, locale_code="en") as sb:
                if reuse_ua:
                    try:
                        sb.driver.execute_cdp_cmd(
                            "Network.setUserAgentOverride",
                            {"userAgent": reuse_ua},
                        )
                    except Exception:
                        pass
                sb.open(HOME_UR)
                sb.sleep(8)
                try:
                    sb.uc_gui_click_captcha()
                    sb.sleep(3)
                except Exception:
                    pass

                if _is_cf_challenge(sb.get_page_source()):
                    logger.warning(
                        "Deoband: challenge still visible after UC (attempt %d)",
                        attempt,
                    )
                    continue

                jar = {c["name"]: c["value"] for c in sb.get_cookies()}
                if "cf_clearance" not in jar:
                    logger.warning(
                        "Deoband: no cf_clearance in browser (have %s)",
                        list(jar)[:12],
                    )
                    continue

                ua_nav = reuse_ua or (
                    sb.execute_script("return navigator.userAgent;") or ""
                )
                merged = dict(_load_cookies() or {})
                merged.update({
                    "cf_clearance": jar["cf_clearance"],
                    "ci_session": jar.get("ci_session") or merged.get("ci_session", ""),
                    "User-Agent": ua_nav.strip()
                    or merged.get(
                        "User-Agent",
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36",
                    ),
                })
                if not merged.get("ci_session"):
                    logger.warning(
                        "Deoband: ci_session missing after UC — "
                        "try logging in via browser once",
                    )
                    continue

                logger.info("Deoband: fresh Cloudflare cookies obtained")
                return merged
        except Exception as e:
            logger.warning("Deoband: UC refresh error: %s", e)
            time.sleep(2)

    logger.error("Deoband: all UC cookie refresh attempts failed")
    return None


def _dedupe_lines(text: str) -> str:
    seen: set[str] = set()
    out_lines: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            out_lines.append(line)
    return "\n".join(out_lines)


def _category_page_urls(cat: str, page_num: int) -> list[str]:
    """Prefer ``/page/N``; fall back to bare category URL for page 1."""
    base = f"{DEOBAND_BASE}/home/ur/{cat}"
    urls: list[str] = []
    if page_num <= 1:
        urls.extend([base, f"{base}/", f"{base}?page=1", f"{base}?paged=1"])
    urls.extend(
        [
            f"{base}/page/{page_num}",
            f"{base}/page/{page_num}/",
            f"{base}?page={page_num}",
            f"{base}?paged={page_num}",
        ]
    )
    return urls


def _mega_menu_qa_slug_url_pairs_ordered(html: str) -> list[tuple[str, str]]:
    """``(slug, canonical_menu_url)`` in DOM order — one row per slug (first link wins).

    The mega menu trailing segment differs per category (not always ``…/slug/1``).
    Opening the wrong ``/{slug}/1`` often repeats the global listing ⇒ ``new=0``.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[str, str]] = []
    seen_slug: set[str] = set()
    for a in soup.select(
        "ul.sub_level_menu li a[href], "
        "nav ul.sub_level_menu li a[href], "
        ".sub_level_menu li a[href]",
    ):
        href = (a.get("href") or "").strip()
        if not href or "qa_ur" not in href:
            continue
        full = canonical_url(urljoin(DEOBAND_BASE, href))
        segs = _path_segments_normalized(urlparse(full).path)
        if len(segs) < 3 or segs[0] != "home" or segs[1] != "qa_ur":
            continue
        slug = segs[2]
        if not slug or slug in _NON_FATWA_SLUGS or slug in seen_slug:
            continue
        seen_slug.add(slug)
        out.append((slug, full))
    return out


def _qa_ur_ordered_listing_candidates(
    slug: str,
    page_num: int,
    *,
    menu_page1_canon: str | None,
) -> list[str]:
    """Fetch order: exact mega-menu page-1 URL first, then synthetic CI variants."""
    ordered: list[str] = []
    seenu: set[str] = set()

    def _add(u: str) -> None:
        c = canonical_url(urljoin(DEOBAND_BASE, u)) if "://" not in u else canonical_url(u)
        if c not in seenu:
            seenu.add(c)
            ordered.append(c)

    if page_num == 1 and menu_page1_canon:
        _add(menu_page1_canon)

    for u in _qa_ur_category_pagination_urls(slug, page_num):
        if (
            page_num == 1
            and menu_page1_canon
            and canonical_url(u) == canonical_url(menu_page1_canon)
        ):
            continue
        _add(u)
    return ordered


def _qa_ur_category_pagination_urls(slug: str, page_num: int) -> list[str]:
    """Synthetic listing URL variants under ``/home/qa_ur/{slug}/…``."""
    base = f"{DEOBAND_BASE}/home/qa_ur/{slug}"
    urls: list[str] = []
    if page_num <= 1:
        urls.extend(
            [
                f"{base}/1",
                f"{base}/1/",
                base,
                f"{base}/",
                f"{base}?page=1",
                f"{base}?paged=1",
            ],
        )
    urls.extend(
        [
            f"{base}/page/{page_num}",
            f"{base}/page/{page_num}/",
            f"{base}/{page_num}",
            f"{base}/{page_num}/",
            f"{base}?page={page_num}",
            f"{base}?paged={page_num}",
        ],
    )
    return urls


def _questions_hub_page_urls(lang: str, page_num: int) -> list[str]:
    """``/home/{lang}/questions`` listing variants (CodeIgniter-style)."""
    base = f"{DEOBAND_BASE}/home/{lang}/questions"
    urls: list[str] = []
    if page_num <= 1:
        urls.extend([base, f"{base}/", f"{base}?page=1", f"{base}?paged=1"])
    urls.extend(
        [
            f"{base}/page/{page_num}",
            f"{base}/page/{page_num}/",
            f"{base}?page={page_num}",
            f"{base}?paged={page_num}",
        ]
    )
    return urls


def _slugs_from_listing_html(html: str, seen_extra: set[str]) -> None:
    """Infer category slugs from any fatwa permalinks in ``html``."""
    soup = BeautifulSoup(html, "lxml")
    for a in soup.select("a[href*='home'], a[href*='index.php']"):
        cu = _canonical_fatwa_detail_url(a.get("href") or "")
        if cu is None:
            continue
        parts = _path_segments_normalized(urlparse(cu).path)
        if len(parts) != 4:
            continue
        slug = parts[2]
        if slug and slug not in _NON_FATWA_SLUGS:
            seen_extra.add(slug)
    for a in soup.select("a[href*='qa_ur']"):
        full = canonical_url(urljoin(DEOBAND_BASE, (a.get("href") or "").strip()))
        segs = _path_segments_normalized(urlparse(full).path)
        if len(segs) >= 3 and segs[0] == "home" and segs[1] == "qa_ur":
            slug = segs[2]
            if slug and slug not in _NON_FATWA_SLUGS:
                seen_extra.add(slug)
    for m in _RE_DEOBAND_DETAIL_ABS.finditer(html):
        cu = _canonical_fatwa_detail_url(m.group(0))
        if cu is None:
            continue
        slug = _path_segments_normalized(urlparse(cu).path)[2]
        if slug and slug not in _NON_FATWA_SLUGS:
            seen_extra.add(slug)


class DeobandSource:
    name = "deoband"
    has_custom_fetcher = True

    def __init__(self) -> None:
        self._session: requests.Session | None = None
        self._cookies: dict | None = None
        self._refresh_attempted = False

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None

    # --- session / refresh -------------------------------------------------

    def _try_browser_refresh(self) -> dict | None:
        if self._refresh_attempted:
            return None
        self._refresh_attempted = True
        ua = (self._cookies or {}).get("User-Agent") if self._cookies else None
        fresh = _refresh_cookies_via_browser(reuse_ua=ua)
        if fresh:
            _save_cookies(fresh)
            return fresh
        return None

    def _ensure_session(self) -> bool:
        if self._session is not None:
            return True
        cookies = _load_cookies()
        if not cookies:
            cookies = self._try_browser_refresh()
        if not cookies:
            return False
        self._cookies = cookies
        self._session = _make_deoband_session(cookies)
        return True

    def _rebuild_session(self, cookies: dict) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
        self._cookies = cookies
        self._session = _make_deoband_session(cookies)

    def _refresh_session(self) -> bool:
        """One-shot UC refresh then rebuild; returns False if unavailable."""
        prior_ua = (self._cookies or {}).get("User-Agent") if self._cookies else None
        if self._refresh_attempted:
            fresh = None
        else:
            self._refresh_attempted = True
            fresh = _refresh_cookies_via_browser(reuse_ua=prior_ua)
            if fresh:
                _save_cookies(fresh)
        if fresh:
            self._rebuild_session(fresh)
            return True
        return False

    def _decode_response(self, r: requests.Response) -> str:
        enc = r.encoding or r.apparent_encoding or "utf-8"
        return r.content.decode(enc, errors="replace")

    # --- fetch_page (CLI) ---------------------------------------------------

    def fetch_page(self, url: str) -> str:
        if not self._ensure_session():
            return ""
        assert self._session is not None

        for round_i in range(2):
            for fetch_u in _url_host_variants(url):
                try:
                    r = self._session.get(fetch_u, timeout=15)
                except requests.RequestException as e:
                    logger.warning(
                        "Deoband: fetch error url=%s: %s", fetch_u, e,
                    )
                    continue

                if r.status_code == 404:
                    continue

                if r.status_code == 403:
                    logger.warning("Deoband: 403 fetch url=%s", fetch_u)
                    continue

                if r.status_code != 200:
                    logger.warning(
                        "Deoband: status=%s url=%s",
                        r.status_code,
                        fetch_u,
                    )
                    continue

                html = self._decode_response(r)
                if _is_cf_challenge(html):
                    logger.warning(
                        "Deoband: CF challenge in fetch url=%s", fetch_u,
                    )
                    continue

                return html

            # All host variants exhausted (or stale CF on every mirror)
            if round_i == 0 and self._refresh_session():
                continue

        return ""

    # --- discovery ---------------------------------------------------------

    def _get_discovery_html(self, robots: RobotsCache, listing_url: str) -> str | None:
        if not any(robots.can_fetch(u) for u in _url_host_variants(listing_url)):
            return None
        assert self._session is not None

        hdr_variants = (None, {"Referer": HOME_UR + "/"})

        for round_i in range(2):
            for extra in hdr_variants:
                for fetch_url in _url_host_variants(listing_url):
                    if not robots.can_fetch(fetch_url):
                        continue
                    try:
                        r = self._session.get(
                            fetch_url,
                            timeout=15,
                            headers=extra,
                        )
                    except requests.RequestException as e:
                        logger.debug("Deoband: GET %s err %s", fetch_url, e)
                        continue

                    if r.status_code == 404:
                        return None
                    if r.status_code != 200:
                        continue

                    html = self._decode_response(r)
                    if _is_cf_challenge(html):
                        continue
                    return html

            if round_i == 0 and self._refresh_session():
                continue

        logger.debug(
            "Deoband: blocked discovery url=%s",
            listing_url,
        )
        return None

    def iter_detail_urls(
        self,
        client: PoliteHttpClient,
        robots: RobotsCache,
        limit: int | None,
    ) -> list[str]:
        del client

        if not self._ensure_session():
            logger.warning(
                "Deoband: no cookies — create %s or allow UC refresh.",
                _COOKIE_FILE.name,
            )
            return []

        cap = limit if limit and limit > 0 else 99999
        out: list[str] = []
        seen: set[str] = set()

        dynamic_slugs: set[str] = set()

        def harvest(html: str) -> list[str]:
            """Pull detail URLs from anchors + regex (CI ``index.php`` permalinks, JSON, etc.)."""
            soup = BeautifulSoup(html, "lxml")
            anchors = soup.select(
                "ul.questions_list li > a[href], "
                "ul.questions_list li a[href], "
                ".questions_list li a[href], "
                ".questions_list a[href], "
                "ul[class*='question'] li > a[href], "
                "a[href*='/home/'], "
                "a[href*='index.php'], "
                "article a[href*='/home/ur/'][href]",
            )

            found_set: list[str] = []

            def _take(cu_raw: str) -> None:
                cu_final = _canonical_fatwa_detail_url(cu_raw)
                if cu_final is None or cu_final in seen:
                    return
                seen.add(cu_final)
                found_set.append(cu_final)

            for a in anchors:
                _take(a.get("href") or "")
            for m in _RE_DEOBAND_DETAIL_ABS.finditer(html):
                _take(m.group(0))
            for m in _RE_DEOBAND_DETAIL_REL.finditer(html):
                _take(m.group(2))
            return found_set

        def harvest_and_slugs(html: str) -> list[str]:
            """Collect detail URLs and remember category slugs for later pagination."""
            _slugs_from_listing_html(html, dynamic_slugs)
            return harvest(html)

        # Phase 0a — Mega-menu archive hub: scrape ``/``, ``/home/ur`` for
        # ``ul.sub_level_menu`` → ``/home/qa_ur/{slug}/…`` (full corpus counters).
        # ``(slug, menu_page1_url)`` — preserve each menu ``href`` (trailing id varies).
        qa_slug_seeds: list[tuple[str, str]] = []
        qa_slug_have: set[str] = set()
        for early_seed in (f"{DEOBAND_BASE}/", HOME_UR):
            if len(out) >= cap:
                break
            blob = self._get_discovery_html(robots, early_seed.rstrip("/"))
            if not blob:
                continue
            prev = len(out)
            for u in harvest_and_slugs(blob):
                if len(out) >= cap:
                    break
                out.append(u)
            if len(out) > prev:
                logger.info(
                    "Deoband: early seed %s → +%d (total=%d)",
                    early_seed.rstrip("/"),
                    len(out) - prev,
                    len(out),
                )
            for slug, menu_u in _mega_menu_qa_slug_url_pairs_ordered(blob):
                if slug in qa_slug_have:
                    continue
                qa_slug_have.add(slug)
                qa_slug_seeds.append((slug, menu_u))

        logger.info(
            "Deoband: mega-menu qa_ur category slugs=%d",
            len(qa_slug_seeds),
        )

        # Phase 0b — Paginate ``/home/qa_ur/{slug}/…`` using exact menu URL for page 1
        for slug, menu_canon in qa_slug_seeds:
            if len(out) >= cap:
                break
            empty_streak = 0
            for page_num in range(1, _MAX_QA_UR_CATEGORY_PAGES + 1):
                if len(out) >= cap:
                    break
                html_val: str | None = None
                used_url: str | None = None
                for listing_url in _expand_listing_url_variants(
                    _qa_ur_ordered_listing_candidates(
                        slug,
                        page_num,
                        menu_page1_canon=menu_canon if page_num == 1 else None,
                    ),
                ):
                    if not robots.can_fetch(listing_url):
                        continue
                    html_val = self._get_discovery_html(robots, listing_url)
                    if html_val is not None:
                        used_url = listing_url
                        break
                if html_val is None:
                    break

                if page_num == 1:
                    soup_ln = BeautifulSoup(html_val, "lxml")
                    tot_el = soup_ln.select_one("h5.total_res_ques")
                    if tot_el is not None:
                        logger.info(
                            "Deoband: qa_ur cat=%s banner=%s",
                            slug,
                            normalize_text(tot_el.get_text())[:160],
                        )

                prev = len(out)
                for u in harvest_and_slugs(html_val):
                    if len(out) >= cap:
                        break
                    out.append(u)
                new_ct = len(out) - prev

                logger.info(
                    "Deoband: qa_ur slug=%s page=%d via=%s new=%d total=%d",
                    slug,
                    page_num,
                    used_url,
                    new_ct,
                    len(out),
                )

                if new_ct == 0:
                    empty_streak += 1
                    if empty_streak >= 3:
                        break
                else:
                    empty_streak = 0
                time.sleep(_LIST_SLEEP_S)

        # Phase 1 — Remaining hubs (``/home/ur/`` slash, EN questions — not Phase 0a)
        for seed in (
            f"{DEOBAND_BASE}/home/ur/",
            f"{DEOBAND_BASE}/home/en/questions",
        ):
            if len(out) >= cap:
                break
            blob = self._get_discovery_html(robots, seed.rstrip("/"))
            if not blob:
                continue
            prev = len(out)
            for u in harvest_and_slugs(blob):
                if len(out) >= cap:
                    break
                out.append(u)
            if len(out) > prev:
                logger.info(
                    "Deoband: seed %s → +%d (total=%d)",
                    seed.rstrip("/"),
                    len(out) - prev,
                    len(out),
                )

        # Phase 1b — ``/home/ur/questions`` pagination (main fatwa index)
        q_empty = 0
        for page_num in range(1, _MAX_QUESTIONS_HUB_PAGES + 1):
            if len(out) >= cap:
                break
            html_val: str | None = None
            used_url: str | None = None
            for listing_url in _expand_listing_url_variants(
                _questions_hub_page_urls("ur", page_num),
            ):
                if not robots.can_fetch(listing_url):
                    continue
                html_val = self._get_discovery_html(robots, listing_url)
                if html_val is not None:
                    used_url = listing_url
                    break
            if html_val is None:
                break
            prev = len(out)
            for u in harvest_and_slugs(html_val):
                if len(out) >= cap:
                    break
                out.append(u)
            new_ct = len(out) - prev
            logger.info(
                "Deoband: questions hub page=%d via=%s new=%d total=%d",
                page_num,
                used_url,
                new_ct,
                len(out),
            )
            if new_ct == 0:
                q_empty += 1
                if q_empty >= 3:
                    break
            else:
                q_empty = 0
            time.sleep(_LIST_SLEEP_S)

        # Phase 2 — static categories + anything we saw linked from hubs
        category_order = list(dict.fromkeys((*DEOBAND_CATEGORIES, *sorted(dynamic_slugs))))

        for cat in category_order:
            if len(out) >= cap:
                break
            empty_streak = 0

            for page_num in range(1, _MAX_CATEGORY_PAGES + 1):
                if len(out) >= cap:
                    break

                html_val: str | None = None
                used_url: str | None = None
                for listing_url in _expand_listing_url_variants(
                    _category_page_urls(cat, page_num),
                ):
                    if not robots.can_fetch(listing_url):
                        continue
                    html_val = self._get_discovery_html(robots, listing_url)
                    if html_val is not None:
                        used_url = listing_url
                        break

                if html_val is None:
                    break

                fresh = harvest(html_val)
                new_ct = sum(1 for u in fresh if u not in out)
                for u in fresh:
                    if len(out) >= cap:
                        break
                    if u not in out:
                        out.append(u)

                logger.info(
                    "Deoband: cat=%s page=%s via=%s new_links=%s total=%d",
                    cat,
                    page_num,
                    used_url,
                    new_ct,
                    len(out),
                )

                if new_ct == 0:
                    empty_streak += 1
                    if empty_streak >= 2:
                        break
                else:
                    empty_streak = 0

                time.sleep(_LIST_SLEEP_S)

        return out[:cap]

    # --- parse -------------------------------------------------------------

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        if not html or len(html) < 100:
            return None

        if _is_cf_challenge(html):
            logger.warning(
                "Deoband: CF page in parser — cookies stale url=%s",
                url,
            )
            return None

        for aggressive in (False, True):
            soup = BeautifulSoup(html, "lxml")
            _strip_listing_noise(soup, aggressive=aggressive)

            meta_q = _question_from_meta(soup)
            segs = _path_segments_normalized(urlparse(url).path)
            url_cat = segs[-2] if len(segs) >= 2 else "deoband"

            for container in _iter_fatwa_containers(soup):
                category = _extract_category(container, url)
                if not category:
                    category = url_cat

                q_text = _extract_question(container)
                if len(q_text.strip()) < 10 and meta_q:
                    q_text = meta_q
                a_text = _extract_answer(container)

                pf = _finalize_fatwa(
                    q_text,
                    a_text,
                    category,
                    url,
                    pass_name="primary",
                )
                if pf is not None:
                    return pf

                cq, ca = _coarse_split_qa(container)
                pf2 = _finalize_fatwa(
                    cq,
                    ca,
                    category,
                    url,
                    pass_name="coarse",
                )
                if pf2 is not None:
                    return pf2

        logger.debug("Deoband: parse exhausted fallbacks url=%s", url)
        return None


def _finalize_fatwa(
    q_text: str,
    a_text: str,
    category: str,
    url: str,
    *,
    pass_name: str,
) -> ParsedFatwa | None:
    q_text = _dedupe_lines(q_text)
    a_text = _dedupe_lines(a_text)

    if len(q_text.strip()) < 10:
        logger.debug(
            "Deoband: question too short (%s) url=%s len=%d",
            pass_name,
            url,
            len(q_text),
        )
        return None
    if len(a_text.strip()) < 30:
        logger.debug(
            "Deoband: answer too short (%s) url=%s len=%d",
            pass_name,
            url,
            len(a_text),
        )
        return None

    logger.debug(
        "Deoband parsed (%s): url=%s cat=%s q=%d a=%d",
        pass_name,
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
        category=category or None,
        date=None,
    )


def _iter_fatwa_containers(soup: BeautifulSoup):
    selectors = (
        "ul.answer_detail li",
        ".answer_detail",
        ".fata_detail",
        "article",
        ".main_content",
        "main",
        "#content",
        ".content",
        ".container .row",
        "body",
    )
    yielded: list = []
    for sel in selectors:
        if sel == "body":
            b = soup.body
            nodes = [b] if b is not None else []
        else:
            nodes = soup.select(sel)
        for node in nodes:
            if node is not None and node not in yielded:
                yielded.append(node)
                yield node


def _question_from_meta(soup: BeautifulSoup) -> str:
    # Description / title snippets help when headings are malformed
    meta = soup.find("meta", attrs={"name": "description"})
    raw = ""
    if meta:
        raw = (meta.get("content") or "").strip()
        if raw and len(raw) < 2000:
            raw = normalize_text(raw)
    if len(raw.strip()) < 12:
        ti = soup.find("title")
        if ti is not None:
            tt = normalize_text(ti.get_text()).strip()
            if 12 <= len(tt) <= 900:
                raw = tt
    return raw if len(raw.strip()) >= 12 else ""


def _extract_category(container: BeautifulSoup, url: str) -> str:
    cat_el = container.select_one(".cate_sec, .category_name")
    if cat_el:
        raw = normalize_text(cat_el.get_text())
        raw = _QUES_NUM_SPLIT_RE.split(raw, maxsplit=1)[0].strip()
        raw = re.sub(r"\s*>>\s*", " — ", raw)
        if raw:
            return raw[:512]

    segs = _path_segments_normalized(urlparse(url).path)
    return segs[-2] if len(segs) >= 2 else "deoband"


def _extract_question(container: BeautifulSoup) -> str:
    q_block = container.select_one(
        "div.question, div.fatwa_question, .question-text",
    )
    if q_block is not None:
        raw = normalize_text(q_block.get_text(separator="\n"))
        raw = _QUES_PREFIX_RE.sub("", raw).strip()
        if len(raw) >= 10:
            return raw

    h2s = container.find_all("h2")

    # Live site: `<h2>… سوال …<p>QUESTION</p></h2>` — question sits *inside* the h2.
    for hn in h2s:
        combo_raw = hn.get_text(" ", strip=False)
        combo = normalize_text(combo_raw)
        if "جواب" in combo:
            continue
        if (
            "سوال" not in combo
            and not re.search(r"\bquestion\b", combo, re.I)
        ):
            continue
        inner_p = hn.find("p", recursive=False) or hn.find("p")
        if inner_p is not None:
            c = inner_p.get("class") or []
            cls_list = [c] if isinstance(c, str) else list(c)
            if "Fatwa-peshaniE" in cls_list:
                continue
            t = normalize_text(inner_p.get_text())
            if len(t) >= 10:
                return _QUES_PREFIX_RE.sub("", t)

        sibling_p = hn.find_next_sibling("p")
        if sibling_p is not None:
            t = normalize_text(sibling_p.get_text())
            if len(t) >= 10:
                return _QUES_PREFIX_RE.sub("", t)

        for sib in hn.find_next_siblings():
            if sib.name not in ("p", "div", "section"):
                continue
            t = normalize_text(sib.get_text())
            if len(t) >= 10:
                return _QUES_PREFIX_RE.sub("", t)

    # Second `<h2>` is usually السؤال body when عنوان is first.
    if len(h2s) >= 2:
        cand = h2s[1]
        if "جواب" not in normalize_text(cand.get_text()):
            inner_p = cand.find("p", recursive=False) or cand.find("p")
            if inner_p is not None:
                c = inner_p.get("class") or []
                cls_list = [c] if isinstance(c, str) else list(c)
                if "Fatwa-peshaniE" not in cls_list:
                    t = normalize_text(inner_p.get_text())
                    if len(t) >= 10:
                        return _QUES_PREFIX_RE.sub("", t)

            p_after = cand.find_next_sibling("p")
            if p_after is not None:
                t = normalize_text(p_after.get_text())
                if len(t) >= 10:
                    return _QUES_PREFIX_RE.sub("", t)

    for hn in container.find_all("h3"):
        combo = normalize_text(hn.get_text())
        if "جواب" in combo:
            continue
        if "سوال" not in combo and not re.search(
            r"\bquestion\b",
            combo,
            re.I,
        ):
            continue
        inner_p = hn.find("p", recursive=False) or hn.find("p")
        if inner_p is not None:
            t = normalize_text(inner_p.get_text())
            if len(t) >= 10:
                return _QUES_PREFIX_RE.sub("", t)

    for hn in h2s:
        lab = normalize_text(hn.get_text())
        if "عنوان" not in lab and not re.search(r"\btitle\b", lab, re.I):
            continue
        sibling_p = hn.find_next_sibling("p")
        if sibling_p is not None:
            t = normalize_text(sibling_p.get_text())
            if len(t) >= 15:
                return _QUES_PREFIX_RE.sub("", t)

    quesid = container.select_one("p.quesid")
    if quesid is not None:
        for p in quesid.find_next_siblings("p"):
            t = normalize_text(p.get_text())
            if _QUES_NUM_SPLIT_RE.search(t) or t.startswith("سوال نمبر"):
                continue
            if len(t) >= 15 and "جواب" not in t:
                return _QUES_PREFIX_RE.sub("", t)

    title_el = container.select_one("p.Fatwa-peshaniE")
    if title_el is not None:
        t = normalize_text(title_el.get_text())
        if len(t) >= 10:
            return _QUES_PREFIX_RE.sub("", t)

    for p in container.find_all("p"):
        t = normalize_text(p.get_text())
        if "سوال" in t and len(t) > 25:
            return _QUES_PREFIX_RE.sub("", t)

    return ""


def _trim_hard_signature(text: str) -> tuple[str, bool]:
    """Cut at first ``واللہ …`` footer if present — ``(fragment, found_stop)``."""
    for stop in _SIGNATURE_STOP_LINES:
        idx = text.find(stop)
        if idx != -1:
            return normalize_text(text[:idx]).strip(), True
    return normalize_text(text).strip(), False


def _append_answer_chunk(parts: list[str], text: str) -> bool:
    if not text or len(text) <= 5:
        return False
    if _FATWA_REF_RE.match(text.strip()):
        return False
    if text in ("بسم الله الرحمن الرحيم", "بسم اللہ الرحمن الرحیم"):
        return False
    parts.append(text)
    return True


def _walk_answer_from_anchor(anchor) -> str:
    parts: list[str] = []
    for sib in anchor.find_next_siblings():
        if sib.name not in _ANSWER_BODY_TAGS:
            continue
        raw = normalize_text(sib.get_text())
        chunk, stopped = _trim_hard_signature(raw)
        if not chunk:
            if stopped:
                break
            continue
        _append_answer_chunk(parts, chunk)
        if stopped:
            break
    return "\n\n".join(parts).strip()


def _gather_answer_after_fatwa_line(li) -> str:
    """Confirmed layout: paragraphs after `<p>Fatwa: …</p>` carry the juridical answer."""
    parts: list[str] = []
    past_id = False
    for pr in li.find_all("p"):
        tnorm = normalize_text(pr.get_text())
        if _FATWA_REF_RE.match(tnorm.strip()):
            past_id = True
            continue
        if not past_id:
            continue
        chunk, stopped = _trim_hard_signature(tnorm)
        if not chunk:
            if stopped:
                break
            continue
        _append_answer_chunk(parts, chunk)
        if stopped:
            break
    return "\n\n".join(parts).strip()


def _extract_answer(container: BeautifulSoup) -> str:
    li_focus = None
    if getattr(container, "name", "") == "li":
        li_focus = container
    else:
        li_focus = (
            container.select_one("ul.answer_detail > li")
            or container.select_one("ul.answer_detail li")
        )

    if li_focus is not None:
        boxed = _gather_answer_after_fatwa_line(li_focus)
        if len(boxed.strip()) >= 30:
            return boxed

    anchor = (
        container.select_one("p.fatwa_answer")
        or container.select_one("div.fatwa_answer")
        or container.select_one(".fatwa_answer")
    )
    if anchor is not None:
        joined = _walk_answer_from_anchor(anchor)
        if len(joined) >= 30:
            return joined

    jawab_h2s = [
        h
        for h in container.find_all("h2")
        if "جواب" in h.get_text()
        or re.search(r"\banswer\b", h.get_text(), re.I)
    ]
    for h2 in reversed(jawab_h2s):
        joined = _walk_answer_from_anchor(h2)
        if len(joined) >= 30:
            return joined

    h2_list = container.find_all("h2")
    if h2_list:
        joined = _walk_answer_from_anchor(h2_list[-1])
        if len(joined) >= 30:
            return joined

    full_text = normalize_text(container.get_text(separator="\n"))
    for marker in _JAWAB_LINE_MARKERS:
        if marker not in full_text:
            continue
        tail = full_text.split(marker, 1)[1].strip()
        lines: list[str] = []
        for ln in tail.splitlines():
            stripped = ln.strip()
            if not stripped:
                continue
            if _FATWA_REF_RE.match(stripped):
                continue
            if "تاریخ اشاعت" in stripped and len(stripped) < 80:
                continue
            trimmed = stripped
            hit_end = False
            for stop in _SIGNATURE_STOP_LINES:
                if stop in trimmed:
                    trimmed = trimmed.split(stop, 1)[0].strip()
                    hit_end = True
                    break
            if trimmed:
                lines.append(trimmed)
            if hit_end:
                break
        joined = "\n".join(lines).strip()
        if len(joined) >= 30:
            return joined

    return ""


def _coarse_split_qa(container: BeautifulSoup) -> tuple[str, str]:
    """Split visible text at the earliest substantial جواب marker."""
    root = (
        container.select_one("main, #content, article, .main_content")
        or container
    )
    text = normalize_text(root.get_text("\n"))
    cut = len(text)
    for marker in _JAWAB_LINE_MARKERS:
        pos = text.find(marker)
        if pos != -1 and 20 < pos < cut:
            cut = pos

    if cut < len(text) - 25:
        head = text[:cut].strip()
        tail = text[cut:].strip()
        if len(head) >= 10 and len(tail) >= 30:
            return head, tail
    return "", ""
