"""Database session, upserts, and error logging."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from mufti_scraper.cleaning import content_hash
from mufti_scraper.db.models import Base, Fatwa, ScrapeError

logger = logging.getLogger(__name__)


class FatwaRepository:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(database_url, future=True)
        Base.metadata.create_all(self.engine)
        self._session = sessionmaker(self.engine, expire_on_commit=False, future=True)

    def new_session(self) -> Session:
        """Return a session; caller must commit/rollback/close (for long batch runs)."""
        return self._session()

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        s = self._session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def upsert_fatwa(
        self,
        session: Session,
        *,
        question: str,
        answer: str,
        source: str,
        url: str,
        category: str | None,
        date: str | None,
    ) -> str:
        """
        Insert fatwa if URL is new. Returns 'inserted', 'skipped', or 'updated'
        (SQLite: on_conflict_do_nothing for duplicate URL).
        """
        ch = content_hash(question, answer)
        now = datetime.now(timezone.utc)
        dialect = session.get_bind().dialect.name

        if dialect == "sqlite":
            stmt = sqlite_insert(Fatwa).values(
                question=question,
                answer=answer,
                source=source,
                url=url,
                category=category,
                date=date,
                scraped_at=now,
                content_hash=ch,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["url"])
            result = session.execute(stmt)
            if result.rowcount:
                return "inserted"
            return "skipped"

        # PostgreSQL / MySQL-style (generic fallback: check then insert)
        existing = session.scalar(select(Fatwa).where(Fatwa.url == url))
        if existing:
            return "skipped"
        session.add(
            Fatwa(
                question=question,
                answer=answer,
                source=source,
                url=url,
                category=category,
                date=date,
                scraped_at=now,
                content_hash=ch,
            )
        )
        return "inserted"

    def log_error(self, session: Session, url: str, source: str, message: str) -> None:
        session.add(
            ScrapeError(
                url=url,
                source=source,
                error=message[:8000],
            )
        )
        logger.warning("[%s] %s — %s", source, url, message[:500])
