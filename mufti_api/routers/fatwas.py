"""
Fatwa browse/search and detail endpoints.

Search uses parameterized LIKE patterns (escape % and _) to reduce injection risk.
For Urdu/Arabic, matching is substring-based; FTS5/pgvector can be added later.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from datetime import datetime, timezone
from typing import Annotated

from cachetools import TTLCache
from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mufti_scraper.db.models import (
    AITrial,
    Fatwa,
    FatwaSummary,
    FatwaTranslation,
    RelatedFatwaCache,
    SearchMiss,
    Subscription,
    User,
)

from mufti_api.database import get_session_factory
from mufti_api.deps import get_db, verify_api_key
from mufti_api.schemas import (
    CategoryCount,
    FatwaOut,
    RelatedFatwaItem,
    RelatedFatwasOut,
    SearchResponse,
    SummaryOut,
    SourceCount,
    StatsResponse,
    StatsSourceCount,
    TranslationOut,
    TranslationRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["fatwas"])


class SummaryRequest(BaseModel):
    language: str = "ur"


_STOP_WORDS = {
    "کیا",
    "کیسے",
    "کب",
    "کہاں",
    "ہے",
    "ہیں",
    "کا",
    "کی",
    "کے",
}


def _escape_like(pattern: str) -> str:
    return pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_filters(
    source: list[str] | None,
    category: str | None,
) -> list:
    conds = []
    if source:
        sources = [s.strip() for s in source if s and s.strip()]
        if sources:
            conds.append(Fatwa.source.in_(sources))
    if category and category.strip():
        conds.append(Fatwa.category == category.strip())
    return conds


def _base_select(conds: list) -> Select:
    stmt = select(Fatwa)
    if conds:
        from sqlalchemy import and_

        stmt = stmt.where(and_(*conds))
    return stmt


def _query_like_condition(query: str):
    esc = _escape_like(query.strip())
    pat = f"%{esc}%"
    return or_(
        Fatwa.question.like(pat, escape="\\"),
        Fatwa.answer.like(pat, escape="\\"),
    )


async def _log_search_miss_safe(
    *,
    query: str,
    source_filter: str | None,
    category_filter: str | None,
    user_agent: str | None,
) -> None:
    try:
        factory = get_session_factory()
        async with factory() as s:
            row = SearchMiss(
                query=query,
                results_count=0,
                source_filter=source_filter,
                category_filter=category_filter,
                searched_at=datetime.now(timezone.utc),
                user_agent=user_agent,
                resolved=False,
            )
            s.add(row)
            await s.commit()
    except Exception as e:
        logger.warning("Search miss logging failed: %s", e)


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Search or list fatwas",
    description=(
        "Keyword search across question and answer (substring match). "
        "Omit `query` to browse with pagination. "
        "Optional filters: `source` (repeatable), `category` (exact match). "
        "v1 is SQL LIKE; ranking/semantic search can be added via FTS or embeddings."
    ),
)
async def search_fatwas(
    request: Request,
    background_tasks: BackgroundTasks,
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
    source: list[str] | None = Query(
        default=None,
        description="Filter by source name (repeat query param, e.g. ?source=A&source=B)",
    ),
    category: str | None = Query(None, description="Filter by exact category"),
) -> SearchResponse:
    settings = request.app.state.settings
    size = limit if limit is not None else page_size
    if size > settings.max_page_size:
        raise HTTPException(
            status_code=400,
            detail=f"page_size/limit must be <= {settings.max_page_size}",
        )

    conds = _search_filters(source, category)
    use_cache = bool(query and query.strip())
    cache: TTLCache | None = getattr(request.app.state, "search_cache", None)
    source_key = tuple(sorted(s.strip() for s in source if s and s.strip())) if source else ()
    cache_key = (query or "", page, size, source_key, category or "")
    if use_cache and cache is not None and cache_key in cache:
        return cache[cache_key]

    snippets_by_id: dict[int, str] = {}
    rows: list[Fatwa] = []
    total = 0
    offset = (page - 1) * size

    query_text = query.strip() if query and query.strip() else None

    if query_text:
        # FTS5 first; fallback to LIKE if FTS path fails.
        async def _like_fallback() -> tuple[int, list[Fatwa]]:
            like_conds = [*conds, _query_like_condition(query_text)]
            like_count_stmt = select(func.count()).select_from(Fatwa)
            if like_conds:
                from sqlalchemy import and_

                like_count_stmt = like_count_stmt.where(and_(*like_conds))
            like_total = int((await db.execute(like_count_stmt)).scalar_one())
            like_list_stmt = _base_select(like_conds).order_by(Fatwa.id.desc()).offset(offset).limit(size)
            like_rows = (await db.execute(like_list_stmt)).scalars().all()
            return like_total, list(like_rows)

        try:
            fts_ids = (
                select(text("rowid"))
                .select_from(text("fatwas_fts"))
                .where(text("fatwas_fts MATCH :q"))
                .params(q=query_text)
            )
            fts_conds = [Fatwa.id.in_(fts_ids), *conds]

            count_stmt = select(func.count()).select_from(Fatwa)
            if fts_conds:
                from sqlalchemy import and_

                count_stmt = count_stmt.where(and_(*fts_conds))
            total = int((await db.execute(count_stmt)).scalar_one())

            list_stmt = _base_select(fts_conds).order_by(Fatwa.id.desc()).offset(offset).limit(size)
            result = await db.execute(list_stmt)
            rows = result.scalars().all()

            for r in rows:
                try:
                    s = (
                        await db.execute(
                            text(
                                """
                                SELECT snippet(fatwas_fts, 0, '<b>', '</b>', '...', 10) AS snip
                                FROM fatwas_fts
                                WHERE fatwas_fts MATCH :q AND rowid = :rid
                                LIMIT 1
                                """
                            ),
                            {"q": query_text, "rid": int(r.id)},
                        )
                    ).first()
                    if s and s[0]:
                        snippets_by_id[int(r.id)] = str(s[0])
                except Exception:
                    # Snippet must not break search flow.
                    pass
            if total == 0:
                total, rows = await _like_fallback()
        except Exception as e:
            logger.warning("FTS search failed; falling back to LIKE: %s", e)
            total, rows = await _like_fallback()
    else:
        count_stmt = select(func.count()).select_from(Fatwa)
        if conds:
            from sqlalchemy import and_

            count_stmt = count_stmt.where(and_(*conds))
        total = int((await db.execute(count_stmt)).scalar_one())
        list_stmt = _base_select(conds).order_by(Fatwa.id.desc()).offset(offset).limit(size)
        result = await db.execute(list_stmt)
        rows = result.scalars().all()

    out = SearchResponse(
        items=[
            FatwaOut(
                **{
                    **FatwaOut.model_validate(r).model_dump(),
                    "snippet": snippets_by_id.get(int(r.id)),
                }
            )
            for r in rows
        ],
        page=page,
        page_size=size,
        total=int(total),
    )
    if int(total) == 0 and query and query.strip():
        source_filter = ",".join(source_key) if source_key else None
        user_agent = request.headers.get("user-agent")
        background_tasks.add_task(
            _log_search_miss_safe,
            query=query.strip(),
            source_filter=source_filter,
            category_filter=category.strip() if category and category.strip() else None,
            user_agent=user_agent,
        )
    if use_cache and cache is not None:
        cache[cache_key] = out
    return out


def _is_active_subscription(sub: Subscription | None) -> bool:
    if sub is None:
        return False
    if sub.status != "active":
        return False
    if sub.expires_at is None:
        return False
    return sub.expires_at > datetime.now(timezone.utc)


async def _resolve_user_from_headers(
    db: AsyncSession,
    x_user_id: str | None,
    x_guest_id: str | None,
) -> User:
    user: User | None = None
    if x_user_id:
        try:
            uid = int(x_user_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Invalid X-User-ID") from e
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    elif x_guest_id:
        user = (await db.execute(select(User).where(User.guest_id == x_guest_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _extract_keywords(text: str) -> list[str]:
    words = [w.strip(".,:;!?()[]{}\"'") for w in (text or "").split()]
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        if len(w) <= 3:
            continue
        if w in _STOP_WORDS:
            continue
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= 3:
            break
    return out


@router.get(
    "/fatwas/{fatwa_id}/related",
    response_model=RelatedFatwasOut,
    summary="Get related fatwas",
)
async def get_related_fatwas(
    fatwa_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(5, ge=1, le=10),
) -> RelatedFatwasOut:
    source_fatwa = (await db.execute(select(Fatwa).where(Fatwa.id == fatwa_id))).scalar_one_or_none()
    if source_fatwa is None:
        raise HTTPException(status_code=404, detail="Fatwa not found")

    now = datetime.now(timezone.utc)
    cached_rows = (
        await db.execute(
            select(RelatedFatwaCache.related_fatwa_id, RelatedFatwaCache.score)
            .where(
                RelatedFatwaCache.fatwa_id == fatwa_id,
                RelatedFatwaCache.cached_until > now,
            )
            .order_by(RelatedFatwaCache.score.desc())
            .limit(limit)
        )
    ).all()
    if cached_rows:
        id_score = {int(r[0]): float(r[1]) for r in cached_rows}
        related_ids = list(id_score.keys())
        related_rows = (
            await db.execute(select(Fatwa).where(Fatwa.id.in_(related_ids)))
        ).scalars().all()
        by_id = {int(r.id): r for r in related_rows}
        items: list[RelatedFatwaItem] = []
        for rid in related_ids:
            row = by_id.get(rid)
            if row is None:
                continue
            items.append(
                RelatedFatwaItem(
                    id=int(row.id),
                    question=(row.question or "")[:150],
                    category=row.category,
                    source=row.source,
                    score=id_score[rid],
                )
            )
        return RelatedFatwasOut(fatwa_id=fatwa_id, related=items[:limit], source="cached")

    found: dict[int, tuple[Fatwa, float]] = {}

    # A) Same category first.
    if source_fatwa.category:
        same_cat = (
            await db.execute(
                select(Fatwa)
                .where(
                    Fatwa.id != fatwa_id,
                    Fatwa.category.is_not(None),
                    Fatwa.category == source_fatwa.category,
                )
                .order_by(func.random())
                .limit(10)
            )
        ).scalars().all()
        for r in same_cat:
            found[int(r.id)] = (r, 0.8)

    # B) Keyword-based fill.
    keywords = _extract_keywords(source_fatwa.question or "")
    for kw in keywords:
        if len(found) >= limit:
            break
        kw_rows = (
            await db.execute(
                select(Fatwa)
                .where(
                    Fatwa.id != fatwa_id,
                    Fatwa.id.not_in(list(found.keys()) or [-1]),
                    or_(
                        Fatwa.question.like(f"%{_escape_like(kw)}%", escape="\\"),
                        Fatwa.answer.like(f"%{_escape_like(kw)}%", escape="\\"),
                    ),
                )
                .limit(5)
            )
        ).scalars().all()
        for r in kw_rows:
            found[int(r.id)] = (r, 0.5)

    ranked = sorted(found.values(), key=lambda x: x[1], reverse=True)[:limit]
    items = [
        RelatedFatwaItem(
            id=int(row.id),
            question=(row.question or "")[:150],
            category=row.category,
            source=row.source,
            score=score,
        )
        for row, score in ranked
    ]

    # Save cache for 7 days; failure should not affect response.
    try:
        cached_until = now + timedelta(days=7)
        for row, score in ranked:
            db.add(
                RelatedFatwaCache(
                    fatwa_id=fatwa_id,
                    related_fatwa_id=int(row.id),
                    score=float(score),
                    created_at=now,
                    cached_until=cached_until,
                )
            )
        await db.commit()
    except Exception as e:
        logger.warning("Related fatwa cache save failed for %s: %s", fatwa_id, e)
        await db.rollback()

    return RelatedFatwasOut(fatwa_id=fatwa_id, related=items, source="fresh")


@router.post(
    "/fatwas/{fatwa_id}/translate",
    response_model=TranslationOut,
    summary="Translate fatwa (subscription required)",
)
async def translate_fatwa(
    fatwa_id: int,
    payload: TranslationRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> TranslationOut:
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-ID")
    try:
        user_id = int(x_user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid X-User-ID") from e

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    sub = (
        await db.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        )
    ).scalars().first()
    if not _is_active_subscription(sub):
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "subscription_required",
                "message": "Translation requires active subscription",
            },
        )

    fatwa = (await db.execute(select(Fatwa).where(Fatwa.id == fatwa_id))).scalar_one_or_none()
    if fatwa is None:
        raise HTTPException(status_code=404, detail="Fatwa not found")

    now = datetime.now(timezone.utc)
    cached = (
        await db.execute(
            select(FatwaTranslation).where(
                FatwaTranslation.fatwa_id == fatwa_id,
                FatwaTranslation.language == payload.target_language,
                FatwaTranslation.cached_until > now,
            )
        )
    ).scalar_one_or_none()
    if cached is not None:
        try:
            parsed = json.loads(cached.translated_content)
            return TranslationOut(
                fatwa_id=fatwa_id,
                language=payload.target_language,
                original_question=fatwa.question or "",
                original_answer=fatwa.answer or "",
                translated_question=str(parsed.get("question", "")),
                translated_answer=str(parsed.get("answer", "")),
                source="cached",
                cached_until=cached.cached_until,
            )
        except Exception:
            pass

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail={
                "detail": "translation_unavailable",
                "message": "Translation service not configured",
            },
        )

    try:
        import anthropic
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail={
                "detail": "translation_unavailable",
                "message": "Translation service not configured",
            },
        ) from e

    lang_name = "English" if payload.target_language == "en" else "Arabic"
    prompt = f"""
Translate the following Islamic fatwa from Urdu to {lang_name}.
Maintain the formal Islamic scholarly tone.
Preserve Arabic terms (like 'Sunnah', 'Hadith', 'Fiqh') as-is.
Do not translate source names or references.

Question (Urdu): {fatwa.question}
Answer (Urdu): {fatwa.answer}

Return ONLY a JSON object:
{{"question": "translated question", "answer": "translated answer"}}
""".strip()

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(getattr(block, "text", "") for block in getattr(message, "content", []))
        parsed = json.loads(raw)
        tq = str(parsed.get("question", "")).strip()
        ta = str(parsed.get("answer", "")).strip()
        if not tq and not ta:
            raise ValueError("Empty translation result")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail={"detail": "translation_failed", "message": str(e)},
        ) from e

    cached_until = now + timedelta(days=30)
    try:
        row = FatwaTranslation(
            fatwa_id=fatwa_id,
            language=payload.target_language,
            translated_content=json.dumps({"question": tq, "answer": ta}, ensure_ascii=False),
            created_at=now,
            cached_until=cached_until,
        )
        db.add(row)
        await db.commit()
    except Exception as e:
        logger.warning("Failed to cache translation for fatwa %s: %s", fatwa_id, e)
        await db.rollback()

    return TranslationOut(
        fatwa_id=fatwa_id,
        language=payload.target_language,
        original_question=fatwa.question or "",
        original_answer=fatwa.answer or "",
        translated_question=tq,
        translated_answer=ta,
        source="fresh",
        cached_until=cached_until,
    )


@router.get(
    "/stats",
    response_model=StatsResponse,
    summary="Overall database statistics",
)
async def get_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> StatsResponse:
    total_stmt = select(func.count()).select_from(Fatwa)
    total_fatwas = int((await db.execute(total_stmt)).scalar_one())

    sources_stmt = (
        select(Fatwa.source, func.count().label("count"))
        .group_by(Fatwa.source)
        .order_by(func.count().desc())
    )
    source_rows = (await db.execute(sources_stmt)).all()
    sources = [StatsSourceCount(name=str(row[0]), count=int(row[1])) for row in source_rows]

    categories_stmt = select(func.count(func.distinct(Fatwa.category))).where(
        Fatwa.category.is_not(None),
        Fatwa.category != "",
    )
    total_categories = int((await db.execute(categories_stmt)).scalar_one())

    return StatsResponse(
        total_fatwas=total_fatwas,
        sources=sources,
        total_sources=len(sources),
        total_categories=total_categories,
    )


@router.get(
    "/sources",
    response_model=list[SourceCount],
    summary="List all sources with counts",
)
async def get_sources(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> list[SourceCount]:
    stmt = (
        select(Fatwa.source, func.count().label("count"))
        .group_by(Fatwa.source)
        .order_by(func.count().desc())
    )
    rows = (await db.execute(stmt)).all()
    return [SourceCount(source=str(row[0]), count=int(row[1])) for row in rows]


@router.get(
    "/categories",
    response_model=list[CategoryCount],
    summary="List categories with counts",
)
async def get_categories(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
    limit: int = Query(20, ge=1),
) -> list[CategoryCount]:
    stmt = (
        select(Fatwa.category, func.count().label("count"))
        .where(Fatwa.category.is_not(None), Fatwa.category != "")
        .group_by(Fatwa.category)
        .order_by(func.count().desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [CategoryCount(category=str(row[0]), count=int(row[1])) for row in rows]


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


@router.post(
    "/fatwas/{fatwa_id}/summary",
    response_model=SummaryOut,
    summary="AI powered simple fatwa summary",
)
async def summarize_fatwa(
    fatwa_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    payload: SummaryRequest = Body(default=SummaryRequest()),
    x_user_id: str | None = Header(None, alias="X-User-ID"),
    x_guest_id: str | None = Header(None, alias="X-Guest-ID"),
) -> SummaryOut:
    language = (payload.language or "ur").strip().lower()
    if language not in {"ur", "en", "ar"}:
        raise HTTPException(status_code=400, detail="language must be ur, en, or ar")

    if not x_user_id and not x_guest_id:
        raise HTTPException(status_code=401, detail="auth_required")

    user = await _resolve_user_from_headers(db, x_user_id, x_guest_id)
    sub = (
        await db.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        )
    ).scalars().first()
    paid = _is_active_subscription(sub)

    trial = (await db.execute(select(AITrial).where(AITrial.user_id == user.id))).scalar_one_or_none()
    if trial is None:
        trial = AITrial(user_id=user.id, used_count=0, max_count=3)
        db.add(trial)
        await db.flush()

    if not paid and int(trial.used_count) >= int(trial.max_count):
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "trial_exhausted",
                "message": "آپ کے مفت ٹرائل ختم ہوگئے۔ سبسکرپشن لیں۔",
                "trials_remaining": 0,
            },
        )

    now = datetime.now(timezone.utc)
    cached = (
        await db.execute(
            select(FatwaSummary).where(
                FatwaSummary.fatwa_id == fatwa_id,
                FatwaSummary.language == language,
                FatwaSummary.cached_until > now,
            )
        )
    ).scalar_one_or_none()

    fatwa = (await db.execute(select(Fatwa).where(Fatwa.id == fatwa_id))).scalar_one_or_none()
    if fatwa is None:
        raise HTTPException(status_code=404, detail="Fatwa not found")

    trials_remaining = None if paid else max(0, int(trial.max_count) - int(trial.used_count))
    if cached is not None:
        return SummaryOut(
            fatwa_id=fatwa_id,
            language=language,
            original_question=(fatwa.question or "")[:200],
            summary=cached.summary_text,
            source="cached",
            cached_until=cached.cached_until,
            trials_remaining=trials_remaining,
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="ai_unavailable")

    try:
        import anthropic
    except Exception as e:
        raise HTTPException(status_code=503, detail="ai_unavailable") from e

    if language == "ur":
        prompt = f"""
آپ ایک اسلامی سکالر ہیں۔ درج ذیل فتوے کو آسان اردو میں سمجھائیں۔

اصل سوال: {fatwa.question}
اصل جواب: {fatwa.answer}

ہدایات:
- بالکل سادہ اردو استعمال کریں جو عام آدمی سمجھ سکے
- 3-4 جملوں میں مکمل کریں
- اصل حکم واضح کریں
- عربی اصطلاحات کی جگہ اردو الفاظ استعمال کریں
- صرف خلاصہ لکھیں، کوئی اضافی بات نہیں

صرف خلاصہ لکھیں:
""".strip()
    elif language == "en":
        prompt = f"""
You are an Islamic scholar. Summarize this Urdu fatwa in simple English.

Question (Urdu): {fatwa.question}
Answer (Urdu): {fatwa.answer}

Rules:
- Use simple English anyone can understand
- 3-4 sentences maximum
- State the ruling clearly
- Keep Islamic terms like Sunnah, Halal, Haram as-is
- Only write the summary, nothing else

Write summary only:
""".strip()
    else:
        prompt = f"""
أنت عالم إسلامي. لخص هذه الفتوى الأردية باللغة العربية البسيطة.

السؤال: {fatwa.question}
الجواب: {fatwa.answer}

التعليمات:
- استخدم لغة عربية بسيطة
- 3-4 جمل كحد أقصى
- وضح الحكم بوضوح
- اكتب الملخص فقط

اكتب الملخص فقط:
""".strip()

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        summary_text = "".join(
            getattr(block, "text", "") for block in getattr(message, "content", [])
        ).strip()
        if not summary_text:
            raise ValueError("Empty summary")
    except Exception as e:
        logger.error("AI summary generation failed for fatwa %s: %s", fatwa_id, e)
        raise HTTPException(status_code=503, detail="ai_unavailable") from e

    if not paid:
        trial.used_count = int(trial.used_count) + 1
    trials_remaining = None if paid else max(0, int(trial.max_count) - int(trial.used_count))

    cached_until = now + timedelta(days=30)
    try:
        row = (
            await db.execute(
                select(FatwaSummary).where(
                    FatwaSummary.fatwa_id == fatwa_id,
                    FatwaSummary.language == language,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = FatwaSummary(
                fatwa_id=fatwa_id,
                language=language,
                summary_text=summary_text,
                created_at=now,
                cached_until=cached_until,
            )
            db.add(row)
        else:
            row.summary_text = summary_text
            row.cached_until = cached_until
            row.created_at = now
        await db.commit()
    except Exception as e:
        logger.warning("Fatwa summary cache save failed for %s: %s", fatwa_id, e)
        await db.rollback()

    return SummaryOut(
        fatwa_id=fatwa_id,
        language=language,
        original_question=(fatwa.question or "")[:200],
        summary=summary_text,
        source="fresh",
        cached_until=cached_until,
        trials_remaining=trials_remaining,
    )
