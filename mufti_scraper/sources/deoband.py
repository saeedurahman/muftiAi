"""
Darul Ifta Deoband scraper: SeleniumBase UC Mode for Cloudflare, then requests.
Updated with robust Question/Answer extraction for the Deoband Urdu layout.
"""

from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from mufti_scraper.cleaning import canonical_url, normalize_text
from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.robots import RobotsCache
from mufti_scraper.sources.base import ParsedFatwa

logger = logging.getLogger(__name__)

try:
    from seleniumbase import SB
    HAS_SELENIUMBASE = True
except ImportError:
    HAS_SELENIUMBASE = False

DEOBAND_BASE = "https://darulifta-deoband.com"

DEOBAND_CATEGORIES = [
    "salah-prayer", "taharah", "sawm-fasting", "zakat", "hajj",
    "nikah-marriage", "talaq-divorce", "tijarah-business", "muamlat",
    "aqeedah-faith", "ilm-knowledge", "hadith-sunnah", "quran-tafseer",
    "tasawwuf", "food-drink", "inheritance", "oaths-vows", "misc", "general",
]

DEOBAND_LISTING = "https://darulifta-deoband.com/home/ur/questions/page/{page}"

_DETAIL_RE = re.compile(r"/home/ur/[\w-]+/\d+$", re.I)

def _get_cf_cookies() -> dict | None:
    """Bypasses Cloudflare using SeleniumBase UC Mode with retries."""
    if not HAS_SELENIUMBASE:
        logger.warning("Deoband: seleniumbase not installed.")
        return None
    
    for attempt in range(1, 4):
        try:
            logger.info(f"Deoband: CF bypass attempt {attempt}/3...")
            with SB(uc=True, headless=False, incognito=True) as sb:
                sb.open(f"{DEOBAND_BASE}/home/ur")
                sb.sleep(7)
                try:
                    sb.uc_gui_click_captcha()
                    sb.sleep(3)
                except:
                    pass

                if "Verify you are human" in sb.get_page_source():
                    continue

                cookies = {c["name"]: c["value"] for c in sb.get_cookies()}
                if "cf_clearance" in cookies:
                    logger.info("Deoband: CF bypass success!")
                    return cookies
        except Exception as exc:
            logger.error(f"Deoband browser error: {exc}")
        time.sleep(2)
    return None

def _make_session(cookies: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ur,en-US;q=0.9,en;q=0.8",
        "Referer": f"{DEOBAND_BASE}/home/ur",
    })
    for name, value in cookies.items():
        s.cookies.set(name, value, domain="darulifta-deoband.com")
    return s

def _sleep_jitter(base: float = 2.0):
    time.sleep(base + random.uniform(0.1, 1.0))

class DeobandSource:
    name = "deoband"
    has_custom_fetcher = True

    def __init__(self) -> None:
        self._cf_cookies: dict | None = None
        self._session: requests.Session | None = None

    def _refresh_session(self) -> bool:
        cookies = _get_cf_cookies()
        if not cookies: return False
        self._cf_cookies = cookies
        if self._session: self._session.close()
        self._session = _make_session(cookies)
        return True

    def fetch_page(self, url: str) -> str:
        if not self._session and not self._refresh_session():
            raise RuntimeError("Deoband: Could not establish session.")
        
        for attempt in range(2):
            r = self._session.get(url, timeout=15)
            if r.status_code == 403:
                if self._refresh_session(): continue
            r.raise_for_status()
            enc = r.encoding or r.apparent_encoding or "utf-8"
            return r.content.decode(enc, errors="replace")
        return ""

    def parse_page(self, html: str, url: str) -> ParsedFatwa | None:
        if not html: return None
        soup = BeautifulSoup(html, "lxml")
        
        # Cleanup UI noise
        for tag in soup.select("nav, header, footer, script, style, .sidebar, .share_fatwa"):
            tag.decompose()

        container = soup.select_one(".answer_detail")
        if not container: return None

        # 1. Category
        cat_el = container.select_one("p.category_name")
        category = normalize_text(cat_el.get_text()) if cat_el else ""

        # 2. Title
        title_el = container.select_one("p.Fatwa-peshaniE")
        title = normalize_text(title_el.get_text()) if title_el else ""

        # 3. Question (Robust: Handles nested <p> inside <h2>)
        q_text = ""
        q_header = container.find("h2", string=re.compile("سوال"))
        if q_header:
            inner_p = q_header.find("p")
            q_text = normalize_text(inner_p.get_text() if inner_p else q_header.get_text())
            q_text = q_text.replace("سوال:", "").strip()

        # 4. Answer (Starts after the Answer ID paragraph)
        ans_parts = []
        ans_start_node = container.select_one("p.fatwa_answer")
        if ans_start_node:
            for sibling in ans_start_node.find_next_siblings():
                txt = normalize_text(sibling.get_text())
                if not txt: continue
                # Stop when reaching the Fatwa signature/authority section
                if any(m in txt for m in ["واللہ تعالیٰ اعلم", "دارالافتاء", "Darul-Ifta"]):
                    break
                ans_parts.append(txt)
        
        a_text = "\n\n".join(ans_parts)

        return ParsedFatwa(
            url=url,
            title=title,
            category=category,
            question=q_text,
            answer=a_text
        )

    def iter_detail_urls(self, client: PoliteHttpClient, robots: RobotsCache, limit: int | None) -> list[str]:
        if not self._session and not self._refresh_session(): return []
        
        urls, seen = [], set()
        for page_num in range(1, 10000):
            if limit and len(urls) >= limit: break
            listing_url = DEOBAND_LISTING.format(page=page_num)
            if not robots.can_fetch(listing_url): break
            
            try:
                html = self.fetch_page(listing_url)
                soup = BeautifulSoup(html, "lxml")
                page_links = [
                    canonical_url(urljoin(listing_url, a["href"]))
                    for a in soup.find_all("a", href=True)
                    if _DETAIL_RE.search(a["href"])
                ]
                if not page_links: break
                
                for link in page_links:
                    if link not in seen:
                        seen.add(link)
                        urls.append(link)
                _sleep_jitter()
            except Exception as e:
                logger.error(f"Error on page {page_num}: {e}")
                break
        return urls[:limit] if limit else urls

    def close(self):
        if self._session: self._session.close()