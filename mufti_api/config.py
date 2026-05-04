"""API settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    """Runtime configuration."""

    database_url: str
    cors_origins: list[str]
    api_key: str | None
    search_cache_ttl_s: int
    search_cache_max_entries: int
    max_page_size: int = 100
    default_page_size: int = 20

    @classmethod
    def load(cls) -> Settings:
        db = os.environ.get("MUFTI_DATABASE_URL", "sqlite:///fatawa.db")
        cors_raw = os.environ.get("CORS_ORIGINS", "*")
        if cors_raw.strip() == "*":
            origins = ["*"]
        else:
            origins = [o.strip() for o in cors_raw.split(",") if o.strip()]
        api_key = os.environ.get("API_KEY", "").strip() or None
        ttl = int(os.environ.get("SEARCH_CACHE_TTL_SECONDS", "60"))
        max_entries = int(os.environ.get("SEARCH_CACHE_MAX_ENTRIES", "256"))
        return cls(
            database_url=db,
            cors_origins=origins,
            api_key=api_key,
            search_cache_ttl_s=ttl,
            search_cache_max_entries=max_entries,
        )
