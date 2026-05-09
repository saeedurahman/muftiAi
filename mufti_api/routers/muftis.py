"""
Mufti management and mufti portal endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mufti_api.deps import get_db, verify_api_key
from mufti_api.schemas import (
    MuftiCreate,
    MuftiDetailResponse,
    MuftiEarningsResponse,
    MuftiMeResponse,
    MuftiOut,
    MuftiPaymentOut,
    MuftiStatsOut,
    MuftiUpdate,
    PaymentMarkPaidRequest,
    PaymentMonthRequest,
    PaymentRateUpdate,
    QuestionAssignRequest,
    QuestionOut,
    QuestionsPageResponse,
    UserOut,
)
from mufti_scraper.db.models import Mufti, MuftiPayment, Question, User

router = APIRouter(tags=["muftis"])


class PaymentsPageResponse(BaseModel):
    items: list[MuftiPaymentOut]
    page: int
    page_size: int
    total: int


class MuftiAnswerRequest(BaseModel):
    answer_text: str


class SeedAdminRequest(BaseModel):
    email: str
    name: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM") from e
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


async def _get_user_by_id(db: AsyncSession, user_id: int) -> User:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def _get_mufti_by_id(db: AsyncSession, mufti_id: int) -> Mufti:
    mufti = (await db.execute(select(Mufti).where(Mufti.id == mufti_id))).scalar_one_or_none()
    if mufti is None:
        raise HTTPException(status_code=404, detail="Mufti not found")
    return mufti


async def _get_mufti_by_user_id(db: AsyncSession, user_id: int) -> Mufti:
    mufti = (await db.execute(select(Mufti).where(Mufti.user_id == user_id))).scalar_one_or_none()
    if mufti is None:
        raise HTTPException(status_code=404, detail="Mufti profile not found")
    return mufti


async def _resolve_mufti_from_header(db: AsyncSession, x_user_id: str | None) -> tuple[User, Mufti]:
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-ID")
    try:
        user_id = int(x_user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid X-User-ID") from e
    user = await _get_user_by_id(db, user_id)
    if user.role != "mufti":
        raise HTTPException(status_code=403, detail="Forbidden")
    mufti = await _get_mufti_by_user_id(db, user.id)
    return user, mufti


def _to_mufti_out(mufti: Mufti, user: User) -> MuftiOut:
    return MuftiOut(
        id=mufti.id,
        user_id=mufti.user_id,
        display_name=mufti.display_name,
        specialization=mufti.specialization,
        per_question_rate=float(mufti.per_question_rate or 0.0),
        is_available=bool(mufti.is_available),
        total_questions_answered=int(mufti.total_questions_answered or 0),
        total_earned=float(mufti.total_earned or 0.0),
        joined_at=mufti.joined_at,
        bio=mufti.bio,
        user=UserOut.model_validate(user),
    )


async def _build_mufti_stats(db: AsyncSession, mufti: Mufti) -> MuftiStatsOut:
    now = _now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    answered_statuses = ["answered", "published"]

    this_month_questions = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Question)
                .where(
                    Question.assigned_mufti_id == mufti.id,
                    Question.status.in_(answered_statuses),
                    Question.answered_at.is_not(None),
                    Question.answered_at >= month_start,
                    Question.answered_at < now,
                )
            )
        ).scalar_one()
    )
    answered_questions = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Question)
                .where(
                    Question.assigned_mufti_id == mufti.id,
                    Question.status.in_(answered_statuses),
                )
            )
        ).scalar_one()
    )
    pending_questions = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Question)
                .where(
                    Question.assigned_mufti_id == mufti.id,
                    Question.status.not_in(answered_statuses),
                )
            )
        ).scalar_one()
    )

    response_rows = (
        await db.execute(
            select(Question.assigned_at, Question.answered_at).where(
                Question.assigned_mufti_id == mufti.id,
                Question.assigned_at.is_not(None),
                Question.answered_at.is_not(None),
                Question.status.in_(answered_statuses),
            )
        )
    ).all()
    avg_response_time_hours = None
    if response_rows:
        deltas = [
            (answered_at - assigned_at).total_seconds() / 3600.0
            for assigned_at, answered_at in response_rows
            if assigned_at is not None and answered_at is not None and answered_at >= assigned_at
        ]
        if deltas:
            avg_response_time_hours = float(sum(deltas) / len(deltas))

    return MuftiStatsOut(
        mufti_id=mufti.id,
        display_name=mufti.display_name,
        this_month_questions=this_month_questions,
        this_month_earned=float(this_month_questions * float(mufti.per_question_rate or 0.0)),
        pending_questions=pending_questions,
        answered_questions=answered_questions,
        avg_response_time_hours=avg_response_time_hours,
    )


@router.post("/admin/muftis", response_model=MuftiOut, summary="Create mufti profile")
async def create_mufti(
    payload: MuftiCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> MuftiOut:
    user = await _get_user_by_id(db, payload.user_id)
    existing = (await db.execute(select(Mufti).where(Mufti.user_id == user.id))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Mufti profile already exists for this user")

    user.role = "mufti"
    user.dashboard_access = True
    row = Mufti(
        user_id=user.id,
        display_name=payload.display_name.strip(),
        specialization=(payload.specialization.strip() if payload.specialization else None),
        per_question_rate=float(payload.per_question_rate),
        bio=(payload.bio.strip() if payload.bio else None),
        is_available=True,
        total_questions_answered=0,
        total_earned=0.0,
        joined_at=_now(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await db.refresh(user)
    return _to_mufti_out(row, user)


@router.get("/admin/muftis", response_model=list[MuftiOut], summary="List muftis")
async def list_muftis(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
    is_available: bool | None = Query(None),
) -> list[MuftiOut]:
    stmt = select(Mufti).order_by(Mufti.joined_at.desc(), Mufti.id.desc())
    if is_available is not None:
        stmt = stmt.where(Mufti.is_available.is_(is_available))
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return []
    user_ids = [int(m.user_id) for m in rows]
    users = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
    user_map = {int(u.id): u for u in users}
    return [_to_mufti_out(m, user_map[m.user_id]) for m in rows if m.user_id in user_map]


@router.get("/admin/muftis/{mufti_id}", response_model=MuftiDetailResponse, summary="Mufti detail")
async def get_mufti_detail(
    mufti_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> MuftiDetailResponse:
    mufti = await _get_mufti_by_id(db, mufti_id)
    user = await _get_user_by_id(db, mufti.user_id)
    stats = await _build_mufti_stats(db, mufti)
    return MuftiDetailResponse(mufti=_to_mufti_out(mufti, user), stats=stats)


@router.patch("/admin/muftis/{mufti_id}", response_model=MuftiOut, summary="Update mufti profile")
async def patch_mufti(
    mufti_id: int,
    payload: MuftiUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> MuftiOut:
    mufti = await _get_mufti_by_id(db, mufti_id)
    user = await _get_user_by_id(db, mufti.user_id)

    if payload.display_name is not None:
        mufti.display_name = payload.display_name.strip()
    if payload.specialization is not None:
        mufti.specialization = payload.specialization.strip() or None
    if payload.per_question_rate is not None:
        mufti.per_question_rate = float(payload.per_question_rate)
    if payload.is_available is not None:
        mufti.is_available = bool(payload.is_available)
    if payload.bio is not None:
        mufti.bio = payload.bio.strip() or None

    await db.commit()
    await db.refresh(mufti)
    return _to_mufti_out(mufti, user)


@router.post(
    "/admin/questions/{question_id}/assign",
    response_model=QuestionOut,
    summary="Assign question to mufti",
)
async def assign_question_to_mufti(
    question_id: int,
    payload: QuestionAssignRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> QuestionOut:
    question = (await db.execute(select(Question).where(Question.id == question_id))).scalar_one_or_none()
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    mufti = await _get_mufti_by_id(db, payload.mufti_id)

    question.assigned_mufti_id = mufti.id
    question.assigned_to = mufti.display_name
    question.assigned_at = _now()
    question.status = "reviewing"
    question.updated_at = _now()
    await db.commit()
    await db.refresh(question)
    return QuestionOut.model_validate(question)


@router.get(
    "/admin/muftis/{mufti_id}/questions",
    response_model=QuestionsPageResponse,
    summary="Questions assigned to mufti",
)
async def list_mufti_questions_admin(
    mufti_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
) -> QuestionsPageResponse:
    await _get_mufti_by_id(db, mufti_id)
    conds = [Question.assigned_mufti_id == mufti_id]
    if status and status.strip():
        conds.append(Question.status == status.strip())
    where_clause = and_(*conds)

    total = int((await db.execute(select(func.count()).select_from(Question).where(where_clause))).scalar_one())
    rows = (
        await db.execute(
            select(Question)
            .where(where_clause)
            .order_by(Question.submitted_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return QuestionsPageResponse(
        items=[QuestionOut.model_validate(r) for r in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/admin/muftis/{mufti_id}/stats", response_model=MuftiStatsOut, summary="Mufti stats")
async def mufti_stats_admin(
    mufti_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> MuftiStatsOut:
    mufti = await _get_mufti_by_id(db, mufti_id)
    return await _build_mufti_stats(db, mufti)


@router.get("/admin/payments", response_model=PaymentsPageResponse, summary="List mufti payments")
async def list_payments_admin(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
    mufti_id: int | None = Query(None),
    month: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaymentsPageResponse:
    conds = []
    if mufti_id is not None:
        conds.append(MuftiPayment.mufti_id == mufti_id)
    if month and month.strip():
        conds.append(MuftiPayment.month == month.strip())
    if status and status.strip():
        conds.append(MuftiPayment.status == status.strip())

    count_stmt = select(func.count()).select_from(MuftiPayment)
    list_stmt = select(MuftiPayment)
    if conds:
        where_clause = and_(*conds)
        count_stmt = count_stmt.where(where_clause)
        list_stmt = list_stmt.where(where_clause)

    total = int((await db.execute(count_stmt)).scalar_one())
    rows = (
        await db.execute(
            list_stmt
            .order_by(MuftiPayment.created_at.desc(), MuftiPayment.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return PaymentsPageResponse(
        items=[MuftiPaymentOut.model_validate(r) for r in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.post(
    "/admin/payments/calculate/{mufti_id}",
    response_model=MuftiPaymentOut,
    summary="Calculate monthly payment for mufti",
)
async def calculate_payment(
    mufti_id: int,
    payload: PaymentMonthRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> MuftiPaymentOut:
    mufti = await _get_mufti_by_id(db, mufti_id)
    month = payload.month.strip()
    start, end = _month_bounds(month)

    answered_statuses = ["answered", "published"]
    count_stmt = select(func.count()).select_from(Question).where(
        Question.assigned_mufti_id == mufti.id,
        Question.status.in_(answered_statuses),
        Question.answered_at.is_not(None),
        Question.answered_at >= start,
        Question.answered_at < end,
        Question.payment_status == "unpaid",
    )
    questions_count = int((await db.execute(count_stmt)).scalar_one())
    total_amount = float(questions_count * float(mufti.per_question_rate))

    row = (
        await db.execute(
            select(MuftiPayment).where(
                MuftiPayment.mufti_id == mufti.id,
                MuftiPayment.month == month,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = MuftiPayment(
            mufti_id=mufti.id,
            user_id=mufti.user_id,
            month=month,
            questions_answered=questions_count,
            per_question_rate=float(mufti.per_question_rate),
            total_amount=total_amount,
            status="pending",
            created_at=_now(),
        )
        db.add(row)
    else:
        row.questions_answered = questions_count
        row.per_question_rate = float(mufti.per_question_rate)
        row.total_amount = total_amount
        if row.status != "paid":
            row.status = "pending"

    await db.commit()
    await db.refresh(row)
    return MuftiPaymentOut.model_validate(row)


@router.patch(
    "/admin/payments/{payment_id}/mark-paid",
    response_model=MuftiPaymentOut,
    summary="Mark payment as paid",
)
async def mark_payment_paid(
    payment_id: int,
    payload: PaymentMarkPaidRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> MuftiPaymentOut:
    payment = (await db.execute(select(MuftiPayment).where(MuftiPayment.id == payment_id))).scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    mufti = await _get_mufti_by_id(db, payment.mufti_id)

    start, end = _month_bounds(payment.month)
    answered_statuses = ["answered", "published"]
    related_questions = (
        await db.execute(
            select(Question).where(
                Question.assigned_mufti_id == payment.mufti_id,
                Question.status.in_(answered_statuses),
                Question.answered_at.is_not(None),
                Question.answered_at >= start,
                Question.answered_at < end,
                Question.payment_status == "unpaid",
            )
        )
    ).scalars().all()
    for q in related_questions:
        q.payment_status = "paid"
        q.payment_amount = float(mufti.per_question_rate)
        q.updated_at = _now()

    payment.status = "paid"
    payment.paid_at = _now()
    payment.payment_ref = payload.payment_ref.strip() if payload.payment_ref else None
    mufti.total_earned = float(mufti.total_earned or 0.0) + float(payment.total_amount or 0.0)

    await db.commit()
    await db.refresh(payment)
    return MuftiPaymentOut.model_validate(payment)


@router.patch("/admin/muftis/{mufti_id}/rate", response_model=MuftiOut, summary="Update mufti rate")
async def update_mufti_rate(
    mufti_id: int,
    payload: PaymentRateUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> MuftiOut:
    mufti = await _get_mufti_by_id(db, mufti_id)
    user = await _get_user_by_id(db, mufti.user_id)
    mufti.per_question_rate = float(payload.per_question_rate)
    await db.commit()
    await db.refresh(mufti)
    return _to_mufti_out(mufti, user)


@router.get("/mufti/me", response_model=MuftiMeResponse, summary="Get own mufti profile")
async def mufti_me(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> MuftiMeResponse:
    user, mufti = await _resolve_mufti_from_header(db, x_user_id)
    stats = await _build_mufti_stats(db, mufti)
    return MuftiMeResponse(mufti=_to_mufti_out(mufti, user), stats=stats)


@router.get("/mufti/questions", response_model=QuestionsPageResponse, summary="List own assigned questions")
async def mufti_questions(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
) -> QuestionsPageResponse:
    _, mufti = await _resolve_mufti_from_header(db, x_user_id)
    conds = [Question.assigned_mufti_id == mufti.id]
    if status and status.strip():
        conds.append(Question.status == status.strip())
    where_clause = and_(*conds)

    total = int((await db.execute(select(func.count()).select_from(Question).where(where_clause))).scalar_one())
    rows = (
        await db.execute(
            select(Question)
            .where(where_clause)
            .order_by(Question.submitted_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return QuestionsPageResponse(
        items=[QuestionOut.model_validate(r) for r in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.patch(
    "/mufti/questions/{question_id}/answer",
    response_model=QuestionOut,
    summary="Submit answer for assigned question",
)
async def mufti_answer_question(
    question_id: int,
    payload: MuftiAnswerRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> QuestionOut:
    _, mufti = await _resolve_mufti_from_header(db, x_user_id)
    question = (await db.execute(select(Question).where(Question.id == question_id))).scalar_one_or_none()
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    if int(question.assigned_mufti_id or 0) != int(mufti.id):
        raise HTTPException(status_code=403, detail="Forbidden")

    question.answer_text = payload.answer_text.strip()
    question.status = "answered"
    question.answered_at = _now()
    question.payment_amount = float(mufti.per_question_rate)
    question.payment_status = "unpaid"
    question.updated_at = _now()
    mufti.total_questions_answered = int(mufti.total_questions_answered or 0) + 1
    await db.commit()
    await db.refresh(question)
    return QuestionOut.model_validate(question)


@router.get("/mufti/earnings", response_model=MuftiEarningsResponse, summary="Get own earnings")
async def mufti_earnings(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> MuftiEarningsResponse:
    _, mufti = await _resolve_mufti_from_header(db, x_user_id)
    stats = await _build_mufti_stats(db, mufti)
    payments = (
        await db.execute(
            select(MuftiPayment)
            .where(MuftiPayment.mufti_id == mufti.id)
            .order_by(MuftiPayment.created_at.desc(), MuftiPayment.id.desc())
        )
    ).scalars().all()
    return MuftiEarningsResponse(
        this_month_questions=stats.this_month_questions,
        this_month_earned=stats.this_month_earned,
        total_earned=float(mufti.total_earned or 0.0),
        payment_history=[MuftiPaymentOut.model_validate(p) for p in payments],
    )


@router.post("/admin/seed-admin", response_model=UserOut, summary="Seed initial admin user")
async def seed_admin(
    payload: SeedAdminRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> UserOut:
    admin_count = int(
        (await db.execute(select(func.count()).select_from(User).where(User.role == "admin"))).scalar_one()
    )
    if admin_count > 0:
        raise HTTPException(status_code=403, detail="Admin already exists")

    email = payload.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email is required")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    row = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if row is None:
        row = User(
            email=email,
            name=name,
            provider="google",
            is_active=True,
            role="admin",
            dashboard_access=True,
            created_at=_now(),
            last_seen_at=_now(),
        )
        db.add(row)
    else:
        row.role = "admin"
        row.dashboard_access = True
        row.name = name
        row.is_active = True
        row.last_seen_at = _now()

    await db.commit()
    await db.refresh(row)
    return UserOut.model_validate(row)
