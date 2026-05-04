"""Per-host robots.txt checks. Fetches via the polite HTTP client to avoid urllib 403."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

if TYPE_CHECKING:
    from mufti_scraper.http_client import PoliteHttpClient

logger = logging.getLogger(__name__)


class RobotsCache:
    """
    Cache RobotFileParser per netloc.
    If robots.txt cannot be fetched/parsed, crawling is allowed (per common practice).
    """

    def __init__(self, user_agent: str, client: PoliteHttpClient | None = None) -> None:
        self._user_agent = user_agent
        self._client = client
        self._cache: dict[str, RobotFileParser | None] = {}

    def _load(self, robots_url: str) -> RobotFileParser | None:
        rp = RobotFileParser()
        rp.set_url(robots_url)
        try:
            if self._client is not None:
                r = self._client.get(robots_url)
                if r.status_code == 404:
                    return None
                if not r.ok:
                    logger.warning(
                        "robots.txt HTTP %s for %s — allowing crawl",
                        r.status_code,
                        robots_url,
                    )
                    return None
                rp.parse(r.text.splitlines())
            else:
                rp.read()
        except Exception as e:
            logger.warning("Could not read robots.txt %s: %s — allowing crawl", robots_url, e)
            return None
        return rp

    def can_fetch(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        base = f"{parsed.scheme}://{parsed.netloc}"
        key = parsed.netloc.lower()
        if key not in self._cache:
            robots_url = f"{base}/robots.txt"
            self._cache[key] = self._load(robots_url)
        rp = self._cache[key]
        if rp is None:
            return True
        try:
            return rp.can_fetch(self._user_agent, url)
        except Exception:
            return True
