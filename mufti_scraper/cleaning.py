"""HTML stripping, plain text extraction, and Unicode normalization."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from html import unescape
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

# Zero-width and bidi marks that often appear in scraped Urdu/Arabic
_ZW_RE = re.compile(
    r"[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)
_WS_RE = re.compile(r"[ \t\xa0\u00a0]+")
_NL_RUN_RE = re.compile(r"\n{3,}")


def canonical_url(url: str) -> str:
    """Normalize URL for deduplication: no fragment, lowercase host, trim path slash."""
    p = urlparse(url.strip())
    if not p.scheme or not p.netloc:
        return url.strip()
    netloc = p.netloc.lower()
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    # Drop default query noise if desired — keep query for ASP.NET pages
    clean = urlunparse((p.scheme.lower(), netloc, path, "", p.query, ""))
    return clean


def content_hash(question: str, answer: str) -> str:
    payload = f"{question}\n{answer}".encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def strip_html_to_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    for sel in ["nav", "footer", "header"]:
        for t in soup.find_all(sel):
            t.decompose()
    for t in soup.find_all(
        class_=re.compile(
            r"(ad-|ads|advert|cookie|banner|social|share|comment)",
            re.I,
        )
    ):
        t.decompose()
    for t in soup.find_all(id=re.compile(r"(ad|ads|cookie|banner|nav)", re.I)):
        t.decompose()
    return soup


def soup_to_text(soup: BeautifulSoup, separator: str = "\n\n") -> str:
    text = soup.get_text(separator=separator)
    return normalize_text(text)


def html_to_clean_text(html: str) -> str:
    soup = strip_html_to_soup(html)
    return soup_to_text(soup)


def extract_readable_text(html: str) -> str:
    """Prefer trafilatura main content when available; fallback to BeautifulSoup."""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
        )
        if extracted and len(extracted.strip()) > 120:
            return normalize_text(extracted)
    except Exception:
        pass
    return html_to_clean_text(html)


def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = unescape(s)
    s = unicodedata.normalize("NFC", s)
    s = _ZW_RE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in s.split("\n"):
        line = _WS_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    s = "\n".join(lines)
    s = _NL_RUN_RE.sub("\n\n", s)
    return s.strip()


def strip_url_query_keys(url: str, keys: set[str]) -> str:
    """Remove specific query keys (e.g. utm_*) for canonicalization."""
    p = urlparse(url)
    if not p.query:
        return url
    q = parse_qs(p.query, keep_blank_values=True)
    for k in keys:
        q.pop(k, None)
    new_query = urlencode({k: v[0] if len(v) == 1 else v for k, v in q.items()})
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, ""))
