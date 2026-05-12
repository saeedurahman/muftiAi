"""
Ask Mufti question intake and admin management endpoints.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

import pdfplumber
from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mufti_api.config import Settings
from mufti_scraper.cleaning import normalize_text
from mufti_scraper.db.models import Question, SearchMiss

from mufti_api.deps import get_db, verify_api_key
from mufti_api.schemas import (
    AnswerFromTextRequest,
    QuestionAnswerUpdate,
    QuestionCreate,
    QuestionOut,
    SearchMissesPageResponse,
    SearchMissOut,
    SearchMissStatsResponse,
    SearchMissTopQuery,
    QuestionsPageResponse,
    QuestionStatusUpdate,
    UploadAnswerResponse,
)

router = APIRouter(tags=["questions"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_MIN_ANSWER_CHARS = 50
_PREVIEW_CHARS = 200


def _dedupe_lines(text: str) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            result.append(line)
    return "\n".join(result)


def _urdu_char_count(s: str) -> int:
    return sum(1 for c in s if "\u0600" <= c <= "\u06ff")


def _mufti_write_access(question: Question, x_user_id: str) -> Literal["mufti"]:
    try:
        uid = int(x_user_id.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid X-User-ID") from e
    if question.assigned_mufti_id is None or question.assigned_mufti_id != uid:
        raise HTTPException(
            status_code=403,
            detail="Question is not assigned to this mufti",
        )
    return "mufti"


def _resolve_answer_writer(
    settings: Settings,
    question: Question,
    x_api_key: str | None,
    x_user_id: str | None,
) -> Literal["admin", "mufti"]:
    if settings.api_key is not None:
        if x_api_key == settings.api_key:
            return "admin"
        if x_user_id is not None:
            return _mufti_write_access(question, x_user_id)
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing API key. Send header: X-API-Key",
        )
    if x_user_id is not None:
        return _mufti_write_access(question, x_user_id)
    return "admin"


def _finalize_extracted_text(raw: str) -> str:
    cleaned = normalize_text(raw)
    return _dedupe_lines(cleaned)


def _extract_pdf_text(file_content: bytes) -> str:
    with pdfplumber.open(io.BytesIO(file_content)) as pdf:
        pages: list[str] = []
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return "\n\n".join(pages)


async def _get_question_or_404(
    db: AsyncSession, question_id: int,
) -> Question:
    stmt = select(Question).where(Question.id == question_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Question not found")
    return row


def _pdf_unreadable_response() -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": "pdf_not_readable",
            "message": (
                "PDF text could not be extracted. "
                "Please use a text-based PDF or TXT file."
            ),
        },
    )


async def _persist_answer(
    db: AsyncSession,
    row: Question,
    final_text: str,
) -> None:
    now = datetime.now(timezone.utc)
    row.answer_text = final_text
    row.status = "answered"
    row.answered_at = now
    row.updated_at = now
    await db.commit()
    await db.refresh(row)


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


@router.post(
    "/questions/{question_id}/upload-answer",
    response_model=UploadAnswerResponse,
    summary="Upload PDF or TXT answer (admin or assigned mufti)",
)
async def upload_question_answer(
    question_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(..., description="PDF or TXT, max 10MB"),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> UploadAnswerResponse:
    settings: Settings = request.app.state.settings
    filename = (file.filename or "").strip()
    suffix = Path(filename).suffix.lower()
    if suffix not in (".pdf", ".txt"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF or TXT files allowed",
        )

    raw_bytes = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail="File too large. Max 10MB",
        )

    row = await _get_question_or_404(db, question_id)
    _resolve_answer_writer(settings, row, x_api_key, x_user_id)

    if suffix == ".txt":
        extracted = raw_bytes.decode("utf-8", errors="ignore").strip()
    else:
        try:
            extracted = _extract_pdf_text(raw_bytes)
        except Exception:
            return _pdf_unreadable_response()
        extracted = extracted.strip()
        if not extracted:
            return _pdf_unreadable_response()
        if _urdu_char_count(extracted) < 50:
            return _pdf_unreadable_response()

    final_text = _finalize_extracted_text(extracted)
    if len(final_text) < _MIN_ANSWER_CHARS:
        raise HTTPException(
            status_code=422,
            detail="Extracted text too short",
        )

    await _persist_answer(db, row, final_text)
    preview = final_text[:_PREVIEW_CHARS]
    if len(final_text) > _PREVIEW_CHARS:
        preview = preview + "..."
    return UploadAnswerResponse(
        question_id=row.id,
        extracted_length=len(final_text),
        status=row.status,
        preview=preview,
        message="Answer extracted and saved successfully",
    )


@router.post(
    "/questions/{question_id}/answer-from-text",
    response_model=UploadAnswerResponse,
    summary="Paste answer text (admin or assigned mufti)",
)
async def answer_question_from_text(
    question_id: int,
    request: Request,
    payload: AnswerFromTextRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> UploadAnswerResponse:
    settings: Settings = request.app.state.settings
    row = await _get_question_or_404(db, question_id)
    _resolve_answer_writer(settings, row, x_api_key, x_user_id)

    final_text = _finalize_extracted_text(payload.answer_text)
    if len(final_text) < _MIN_ANSWER_CHARS:
        raise HTTPException(
            status_code=422,
            detail="Extracted text too short",
        )

    await _persist_answer(db, row, final_text)
    preview = final_text[:_PREVIEW_CHARS]
    if len(final_text) > _PREVIEW_CHARS:
        preview = preview + "..."
    return UploadAnswerResponse(
        question_id=row.id,
        extracted_length=len(final_text),
        status=row.status,
        preview=preview,
        message="Answer extracted and saved successfully",
    )


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
