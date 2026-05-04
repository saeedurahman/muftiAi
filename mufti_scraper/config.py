"""Runtime configuration (env overrides supported)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class ScraperConfig:
    """Scraper behaviour and storage."""

    database_url: str = "sqlite:///fatawa.db"
    request_timeout_s: int = 45
    max_retries: int = 3
    retry_backoff_base_s: float = 2.0
    delay_seconds: float = 1.5
    delay_jitter_seconds: float = 0.75
    user_agent: str = (
        "MuftiEducationalScraper/0.1 (+https://example.local; educational indexing; "
        "respectful crawl)"
    )
    log_dir: str = "logs"
    log_file: str = "scraper.log"
    batch_commit_size: int = 50
    # Karachi discovery: max BFS depth from seed page
    karachi_max_depth: int = 3
    # Stop listing pagination after N consecutive pages with no new URLs
    pagination_stale_pages: int = 3

    @classmethod
    def from_env(cls) -> ScraperConfig:
        return cls(
            database_url=os.environ.get("MUFTI_DATABASE_URL", cls.database_url),
            request_timeout_s=int(os.environ.get("MUFTI_TIMEOUT", cls.request_timeout_s)),
            delay_seconds=float(os.environ.get("MUFTI_DELAY", cls.delay_seconds)),
            delay_jitter_seconds=float(
                os.environ.get("MUFTI_JITTER", cls.delay_jitter_seconds)
            ),
            user_agent=os.environ.get("MUFTI_USER_AGENT", cls.user_agent),
        )
