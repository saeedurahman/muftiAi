"""HTTP session with retries, per-domain rate limiting, and encoding-safe text."""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from mufti_scraper.config import ScraperConfig

logger = logging.getLogger(__name__)


class PoliteHttpClient:
    """Session wrapper for polite scraping.

    Provides:
    * urllib3 ``Retry`` for transient HTTP errors.
    * Per-domain rate limiting — consecutive requests to the same netloc
      are spaced by at least ``delay_seconds + uniform(0, jitter)``,
      while cross-domain requests fire immediately.
    * Helpers for the common shapes scrapers actually need:
      :meth:`get`, :meth:`head`, :meth:`get_text`, :meth:`get_json`.
    """

    def __init__(self, config: ScraperConfig) -> None:
        self._config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ur,ar,en-US;q=0.9,en;q=0.8",
            }
        )
        retry = Retry(
            total=config.max_retries,
            connect=config.max_retries,
            read=config.max_retries,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            backoff_factor=config.retry_backoff_base_s,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Per-domain throttling state. Keyed by lowercased netloc; value is
        # the ``time.monotonic()`` of the most recent dispatched request.
        self._last_request_at: dict[str, float] = {}
        self._domain_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Throttling
    # ------------------------------------------------------------------

    def sleep_polite(self) -> None:
        """Unconditional polite sleep: ``base + uniform(0, jitter)`` seconds.

        Retained for backward compatibility with code that bypasses
        :meth:`get` and drives ``self.session`` directly. New code should
        prefer :meth:`get`, :meth:`head`, :meth:`get_text`, or
        :meth:`get_json` — they honor per-domain rate limiting automatically.
        """
        base = self._config.delay_seconds
        jitter = random.uniform(0, self._config.delay_jitter_seconds)
        time.sleep(base + jitter)

    def _wait_for_domain(self, url: str) -> None:
        """Sleep just long enough to keep consecutive requests to the same
        netloc at least ``delay_seconds + uniform(0, jitter)`` apart.

        Cross-domain requests proceed immediately when nothing recent is
        recorded for the target host, dramatically cutting idle time when
        scrapers interleave between sites.
        """
        netloc = urlparse(url).netloc.lower()
        if not netloc:
            # Defensive: relative or schemeless URL — fall back to the
            # blanket polite sleep so we don't accidentally hammer.
            self.sleep_polite()
            return

        min_gap = self._config.delay_seconds + random.uniform(
            0, self._config.delay_jitter_seconds
        )
        # Hold the lock through the sleep so concurrent callers serialize
        # rather than all firing at once after observing the same `last`.
        with self._domain_lock:
            last = self._last_request_at.get(netloc)
            now = time.monotonic()
            if last is not None and (now - last) < min_gap:
                time.sleep(min_gap - (now - last))
            self._last_request_at[netloc] = time.monotonic()

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """Polite GET. Public signature unchanged for backward compatibility."""
        self._wait_for_domain(url)
        kwargs.setdefault("timeout", self._config.request_timeout_s)
        logger.debug("GET %s", url)
        return self.session.get(url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> requests.Response:
        """Polite HEAD for lightweight existence / status probes.

        Defaults to ``allow_redirects=False`` so callers can distinguish a
        real ``200`` from a redirect to a homepage / error page; pass
        ``allow_redirects=True`` explicitly to override.
        """
        self._wait_for_domain(url)
        kwargs.setdefault("timeout", self._config.request_timeout_s)
        kwargs.setdefault("allow_redirects", False)
        logger.debug("HEAD %s", url)
        return self.session.head(url, **kwargs)

    def get_text(self, url: str, **kwargs: Any) -> str:
        """Fetch ``url`` and return the body decoded as a ``str``.

        Encoding strategy (first that yields a usable result wins):

        1. ``charset-normalizer`` byte detection — Urdu / Arabic sites
           routinely send the wrong ``Content-Type`` charset, so we ignore
           the header and look at the bytes first.
        2. ``chardet`` byte detection (used only when ``charset-normalizer``
           isn't importable).
        3. ``response.encoding`` (HTTP / meta charset).
        4. ``response.apparent_encoding`` (requests' built-in detector).
        5. UTF-8 with ``errors="replace"`` as a final safety net.

        Raises ``requests.HTTPError`` on a non-2xx response, matching the
        ``get(...).raise_for_status()`` pattern callers already use.
        """
        r = self.get(url, **kwargs)
        r.raise_for_status()
        return _decode_response_text(r)

    def get_json(self, url: str, **kwargs: Any) -> Any | None:
        """Fetch ``url`` and parse JSON when the server confirms it.

        Returns the parsed JSON value if ``Content-Type`` contains
        ``application/json``; otherwise returns ``None`` (so callers can
        fall through to a different discovery path without raising).
        Non-2xx responses raise ``requests.HTTPError`` consistent with
        :meth:`get_text`.
        """
        r = self.get(url, **kwargs)
        r.raise_for_status()
        ct = (r.headers.get("Content-Type") or "").lower()
        if "application/json" not in ct:
            return None
        try:
            return r.json()
        except ValueError as e:
            logger.warning("JSON decode failed for %s: %s", url, e)
            return None


def _decode_response_text(r: requests.Response) -> str:
    """Best-effort bytes→str decoding for an HTTP response.

    Tries byte-level detection libraries first because servers commonly
    misreport encoding in headers, then falls back to header-driven
    encodings, and finally to UTF-8 with replacement so a worst-case
    response still yields a usable string.
    """
    body = r.content
    if not body:
        return ""

    # 1. charset-normalizer (pulled in by modern ``requests`` and so usually
    # available transitively; soft-import keeps things working if not).
    try:
        from charset_normalizer import from_bytes  # type: ignore

        result = from_bytes(body).best()
        if result is not None and result.encoding:
            try:
                return body.decode(result.encoding, errors="replace")
            except (LookupError, UnicodeDecodeError):
                pass
    except ImportError:
        pass

    # 2. chardet — optional, separate package; only used when
    # ``charset-normalizer`` is unavailable.
    try:
        import chardet  # type: ignore

        detected = chardet.detect(body) or {}
        enc = detected.get("encoding")
        if enc:
            try:
                return body.decode(enc, errors="replace")
            except (LookupError, UnicodeDecodeError):
                pass
    except ImportError:
        pass

    # 3. Headers & requests' apparent_encoding.
    encoding = r.encoding or r.apparent_encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")
