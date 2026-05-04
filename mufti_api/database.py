"""Async SQLAlchemy engine and session (SQLite via aiosqlite, PostgreSQL via asyncpg)."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mufti_scraper.db.models import Base

logger = logging.getLogger(__name__)

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def sync_to_async_database_url(sync_url: str) -> str:
    """Convert sync SQLAlchemy URL to async driver URL."""
    u = make_url(sync_url)
    if u.drivername == "sqlite":
        return str(u.set(drivername="sqlite+aiosqlite"))
    if u.drivername == "postgresql":
        return str(u.set(drivername="postgresql+asyncpg"))
    if u.drivername == "postgresql+psycopg2":
        return str(u.set(drivername="postgresql+asyncpg"))
    raise ValueError(
        f"Unsupported database URL driver for async API: {u.drivername}. "
        "Use sqlite:///... or postgresql://..."
    )


def init_engine(database_url: str):
    """Create global async engine and session factory."""
    global _engine, _session_factory
    async_url = sync_to_async_database_url(database_url)
    _engine = create_async_engine(async_url, echo=False)
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    logger.info("Async engine created for %s", async_url.split("@")[-1])


async def dispose_engine():
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database not initialized; call init_engine first.")
    return _session_factory


@asynccontextmanager
async def lifespan_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def create_tables_if_needed():
    """Ensure tables exist (same schema as scraper)."""
    if _engine is None:
        return
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
