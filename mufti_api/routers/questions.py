"""
Ask Mufti question intake and admin management endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mufti_scraper.db.models import Question, SearchMiss

from mufti_api.deps import get_db, verify_api_key
from mufti_api.schemas import (
    QuestionAnswerUpdate,
    QuestionCreate,
    QuestionOut,
    SearchMissesPageResponse,
    SearchMissOut,
    SearchMissStatsResponse,
    SearchMissTopQuery,
    QuestionsPageResponse,
    QuestionStatusUpdate,
)

router = APIRouter(tags=["questions"])


@router.post(
    "/questions",
    response_model=QuestionOut,
    summary="Submit Ask Mufti question",
)
async def create_question(
    payload: QuestionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> QuestionOut:
    # TODO: add request-level rate limiting / anti-spam controls.
    row = Question(
        question_text=payload.question_text.strip(),
        contact_info=(payload.contact_info.strip() if payload.contact_info else None),
        language=payload.language.strip() or "ur",
        status="pending",
        priority="normal",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return QuestionOut.model_validate(row)


@router.get(
    "/admin/questions",
    response_model=QuestionsPageResponse,
    summary="List submitted questions (admin)",
)
async def list_admin_questions(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
) -> QuestionsPageResponse:
    conds = []
    if status and status.strip():
        conds.append(Question.status == status.strip())

    count_stmt = select(func.count()).select_from(Question)
    if conds:
        from sqlalchemy import and_

        count_stmt = count_stmt.where(and_(*conds))
    total = int((await db.execute(count_stmt)).scalar_one())

    stmt = select(Question)
    if conds:
        from sqlalchemy import and_

        stmt = stmt.where(and_(*conds))
    stmt = stmt.order_by(Question.submitted_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    return QuestionsPageResponse(
        items=[QuestionOut.model_validate(r) for r in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/admin/questions/{question_id}",
    response_model=QuestionOut,
    summary="Get one submitted question (admin)",
)
async def get_admin_question(
    question_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> QuestionOut:
    stmt = select(Question).where(Question.id == question_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Question not found")
    return QuestionOut.model_validate(row)


@router.patch(
    "/admin/questions/{question_id}/status",
    response_model=QuestionOut,
    summary="Update question status (admin)",
)
async def patch_question_status(
    question_id: int,
    payload: QuestionStatusUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> QuestionOut:
    stmt = select(Question).where(Question.id == question_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Question not found")

    row.status = payload.status
    row.admin_notes = payload.admin_notes
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return QuestionOut.model_validate(row)


@router.patch(
    "/admin/questions/{question_id}/answer",
    response_model=QuestionOut,
    summary="Set question answer text (admin)",
)
async def patch_question_answer(
    question_id: int,
    payload: QuestionAnswerUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> QuestionOut:
    stmt = select(Question).where(Question.id == question_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Question not found")

    row.answer_text = payload.answer_text
    row.status = "answered"
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return QuestionOut.model_validate(row)


@router.delete(
    "/admin/questions/{question_id}",
    summary="Reject question (soft delete, admin)",
)
async def reject_question(
    question_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> dict[str, str]:
    stmt = select(Question).where(Question.id == question_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Question not found")

    row.status = "rejected"
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "Question rejected"}


@router.get(
    "/admin/search-misses",
    response_model=SearchMissesPageResponse,
    summary="List logged search misses (admin)",
)
async def list_search_misses(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
    resolved: bool | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> SearchMissesPageResponse:
    conds = []
    if resolved is not None:
        conds.append(SearchMiss.resolved.is_(resolved))

    count_stmt = select(func.count()).select_from(SearchMiss)
    if conds:
        from sqlalchemy import and_

        count_stmt = count_stmt.where(and_(*conds))
    total = int((await db.execute(count_stmt)).scalar_one())

    stmt = select(SearchMiss)
    if conds:
        from sqlalchemy import and_

        stmt = stmt.where(and_(*conds))
    stmt = stmt.order_by(SearchMiss.searched_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    return SearchMissesPageResponse(
        items=[SearchMissOut.model_validate(r) for r in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.patch(
    "/admin/search-misses/{miss_id}/resolve",
    response_model=SearchMissOut,
    summary="Mark search miss resolved (admin)",
)
async def resolve_search_miss(
    miss_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> SearchMissOut:
    stmt = select(SearchMiss).where(SearchMiss.id == miss_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Search miss not found")
    row.resolved = True
    await db.commit()
    await db.refresh(row)
    return SearchMissOut.model_validate(row)


@router.get(
    "/admin/search-misses/stats",
    response_model=SearchMissStatsResponse,
    summary="Search misses overview (admin)",
)
async def search_miss_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> SearchMissStatsResponse:
    total_stmt = select(func.count()).select_from(SearchMiss)
    total_misses = int((await db.execute(total_stmt)).scalar_one())

    unresolved_stmt = select(func.count()).select_from(SearchMiss).where(
        SearchMiss.resolved.is_(False)
    )
    unresolved = int((await db.execute(unresolved_stmt)).scalar_one())

    top_stmt = (
        select(SearchMiss.query, func.count().label("count"))
        .group_by(SearchMiss.query)
        .order_by(func.count().desc())
        .limit(10)
    )
    top_rows = (await db.execute(top_stmt)).all()
    top_queries = [SearchMissTopQuery(query=str(r[0]), count=int(r[1])) for r in top_rows]

    return SearchMissStatsResponse(
        total_misses=total_misses,
        unresolved=unresolved,
        top_queries=top_queries,
    )
