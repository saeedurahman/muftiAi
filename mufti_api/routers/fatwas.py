"""
Fatwa browse/search and detail endpoints.

Search uses parameterized LIKE patterns (escape % and _) to reduce injection risk.
For Urdu/Arabic, matching is substring-based; FTS5/pgvector can be added later.
"""

from __future__ import annotations

import logging
from typing import Annotated

from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mufti_scraper.db.models import Fatwa

from mufti_api.deps import get_db, verify_api_key
from mufti_api.schemas import FatwaOut, SearchResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["fatwas"])


def _escape_like(pattern: str) -> str:
    return pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_filters(
    query: str | None,
    source: str | None,
    category: str | None,
) -> list:
    conds = []
    if query and query.strip():
        raw = query.strip()
        esc = _escape_like(raw)
        pat = f"%{esc}%"
        conds.append(
            or_(
                Fatwa.question.like(pat, escape="\\"),
                Fatwa.answer.like(pat, escape="\\"),
            )
        )
    if source and source.strip():
        conds.append(Fatwa.source == source.strip())
    if category and category.strip():
        conds.append(Fatwa.category == category.strip())
    return conds


def _base_select(conds: list) -> Select:
    stmt = select(Fatwa)
    if conds:
        from sqlalchemy import and_

        stmt = stmt.where(and_(*conds))
    return stmt


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Search or list fatwas",
    description=(
        "Keyword search across question and answer (substring match). "
        "Omit `query` to browse with pagination. "
        "Optional filters: `source`, `category` (exact match). "
        "v1 is SQL LIKE; ranking/semantic search can be added via FTS or embeddings."
    ),
)
async def search_fatwas(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
    query: str | None = Query(None, description="Search text (empty = browse all)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, description="Rows per page"),
    limit: int | None = Query(
        None,
        ge=1,
        description="If set, overrides page_size (same meaning as page_size)",
    ),
    source: str | None = Query(None, description="Filter by exact source name"),
    category: str | None = Query(None, description="Filter by exact category"),
) -> SearchResponse:
    settings = request.app.state.settings
    size = limit if limit is not None else page_size
    if size > settings.max_page_size:
        raise HTTPException(
            status_code=400,
            detail=f"page_size/limit must be <= {settings.max_page_size}",
        )

    conds = _search_filters(query, source, category)
    use_cache = bool(query and query.strip())
    cache: TTLCache | None = getattr(request.app.state, "search_cache", None)
    cache_key = (query or "", page, size, source or "", category or "")
    if use_cache and cache is not None and cache_key in cache:
        return cache[cache_key]

    count_stmt = select(func.count()).select_from(Fatwa)
    if conds:
        from sqlalchemy import and_

        count_stmt = count_stmt.where(and_(*conds))
    total = (await db.execute(count_stmt)).scalar_one()

    list_stmt = _base_select(conds).order_by(Fatwa.id.desc())
    offset = (page - 1) * size
    list_stmt = list_stmt.offset(offset).limit(size)
    result = await db.execute(list_stmt)
    rows = result.scalars().all()

    out = SearchResponse(
        items=[FatwaOut.model_validate(r) for r in rows],
        page=page,
        page_size=size,
        total=int(total),
    )
    if use_cache and cache is not None:
        cache[cache_key] = out
    return out


@router.get(
    "/fatwa/{fatwa_id}",
    response_model=FatwaOut,
    summary="Get one fatwa by id",
)
async def get_fatwa(
    fatwa_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> FatwaOut:
    stmt = select(Fatwa).where(Fatwa.id == fatwa_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Fatwa not found")
    return FatwaOut.model_validate(row)
