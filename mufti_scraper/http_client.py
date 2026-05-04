"""HTTP session with retries, timeouts, and polite delays."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from mufti_scraper.config import ScraperConfig

logger = logging.getLogger(__name__)


class PoliteHttpClient:
    """Session wrapper: retries for transient errors, delay between calls."""

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

    def sleep_polite(self) -> None:
        base = self._config.delay_seconds
        jitter = random.uniform(0, self._config.delay_jitter_seconds)
        time.sleep(base + jitter)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.sleep_polite()
        kwargs.setdefault("timeout", self._config.request_timeout_s)
        logger.debug("GET %s", url)
        return self.session.get(url, **kwargs)
