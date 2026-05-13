"""Insert a ``fatwas`` row for search (shared by manual entry and submissions)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from mufti_scraper.db.models import Fatwa


async def insert_searchable_fatwa(
    db: AsyncSession,
    *,
    question: str,
    answer: str,
    category: str | None,
    source: str,
    url: str,
) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = Fatwa(
        question=question.strip(),
        answer=answer.strip(),
        source=(source or "")[:255],
        url=(url or "")[:2048],
        category=(category.strip()[:512] if category and category.strip() else None),
        date=today,
    )
    db.add(row)
    await db.flush()
    return int(row.id)
