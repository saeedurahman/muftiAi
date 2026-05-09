"""
Donation endpoints (Phase 1, no gateway verification yet).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mufti_api.deps import get_db, verify_api_key
from mufti_api.schemas import (
    AdminDonationsPageResponse,
    DonationCreate,
    DonationCreateResponse,
    DonationHistoryResponse,
    DonationOut,
    DonationStatsResponse,
    DonationVerifyResponse,
)
from mufti_scraper.db.models import Donation, Subscription, User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["donations"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _resolve_user(
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
    else:
        raise HTTPException(status_code=400, detail="Provide X-User-ID or X-Guest-ID")

    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _is_active_subscription(sub: Subscription | None) -> bool:
    if sub is None:
        return False
    if sub.status != "active":
        return False
    if sub.expires_at is None:
        return False
    return sub.expires_at > _now()


async def _latest_subscription(db: AsyncSession, user_id: int) -> Subscription | None:
    stmt = (
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc(), Subscription.id.desc())
    )
    return (await db.execute(stmt)).scalars().first()


async def _grant_ai_week_if_needed(db: AsyncSession, donation: Donation) -> None:
    if donation.ai_week_granted:
        return
    sub = await _latest_subscription(db, donation.user_id)
    if _is_active_subscription(sub):
        return
    now = _now()
    week_sub = Subscription(
        user_id=donation.user_id,
        plan="free_ai_week",
        status="active",
        started_at=now,
        expires_at=now + timedelta(days=7),
        payment_provider="donation",
        payment_ref=donation.payment_ref,
    )
    db.add(week_sub)
    donation.ai_week_granted = True


@router.post("/donations/create", response_model=DonationCreateResponse, summary="Create donation intent")
async def create_donation(
    payload: DonationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
    x_guest_id: str | None = Header(None, alias="X-Guest-ID"),
) -> DonationCreateResponse:
    if payload.currency == "PKR" and payload.amount < 50:
        raise HTTPException(status_code=400, detail="Minimum amount for PKR is 50")
    if payload.currency == "USD" and payload.amount < 0.50:
        raise HTTPException(status_code=400, detail="Minimum amount for USD is 0.50")

    user = await _resolve_user(db, x_user_id, x_guest_id)
    now = _now()
    row = Donation(
        user_id=user.id,
        amount=float(payload.amount),
        currency=payload.currency,
        payment_method=payload.payment_method,
        status="pending",
        ai_week_granted=False,
        donated_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return DonationCreateResponse(
        donation_id=row.id,
        amount=row.amount,
        currency=row.currency,
        status=row.status,
    )


@router.post(
    "/donations/{donation_id}/verify",
    response_model=DonationVerifyResponse,
    summary="Mark donation as success (Phase 1)",
)
async def verify_donation(
    donation_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> DonationVerifyResponse:
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-ID")
    user = await _resolve_user(db, x_user_id, None)

    row = (await db.execute(select(Donation).where(Donation.id == donation_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Donation not found")
    if row.user_id != user.id:
        raise HTTPException(status_code=403, detail="Donation does not belong to user")

    if row.status == "success":
        return DonationVerifyResponse(
            donation_id=row.id,
            status=row.status,
            ai_week_granted=bool(row.ai_week_granted),
        )

    row.status = "success"
    row.updated_at = _now()
    await _grant_ai_week_if_needed(db, row)
    await db.commit()
    await db.refresh(row)
    return DonationVerifyResponse(
        donation_id=row.id,
        status=row.status,
        ai_week_granted=bool(row.ai_week_granted),
    )


@router.get("/donations/history", response_model=DonationHistoryResponse, summary="Donation history")
async def donation_history(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> DonationHistoryResponse:
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-ID")
    user = await _resolve_user(db, x_user_id, None)

    stmt = select(Donation).where(Donation.user_id == user.id).order_by(Donation.donated_at.desc())
    rows = (await db.execute(stmt)).scalars().all()

    sum_stmt = select(func.coalesce(func.sum(Donation.amount), 0.0)).where(
        Donation.user_id == user.id,
        Donation.currency == "PKR",
        Donation.status == "success",
    )
    total_pkr = float((await db.execute(sum_stmt)).scalar_one())
    return DonationHistoryResponse(
        donations=[DonationOut.model_validate(r) for r in rows],
        total_donated_pkr=total_pkr,
    )


@router.post("/donations/webhook/stripe", summary="Stripe webhook placeholder")
async def stripe_webhook(request: Request) -> dict[str, str]:
    payload = await request.body()
    logger.info("Stripe webhook placeholder received (%d bytes)", len(payload))
    # TODO: Implement Stripe verification here
    return {"status": "ok"}


@router.post("/donations/webhook/jazzcash", summary="JazzCash webhook placeholder")
async def jazzcash_webhook(request: Request) -> dict[str, str]:
    payload = await request.body()
    logger.info("JazzCash webhook placeholder received (%d bytes)", len(payload))
    # TODO: Implement JazzCash verification here
    return {"status": "ok"}


@router.get(
    "/admin/donations",
    response_model=AdminDonationsPageResponse,
    summary="List donations (admin)",
)
async def admin_list_donations(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> AdminDonationsPageResponse:
    conds = []
    if status and status.strip():
        conds.append(Donation.status == status.strip())

    count_stmt = select(func.count()).select_from(Donation)
    if conds:
        from sqlalchemy import and_

        count_stmt = count_stmt.where(and_(*conds))
    total = int((await db.execute(count_stmt)).scalar_one())

    stmt = select(Donation)
    if conds:
        from sqlalchemy import and_

        stmt = stmt.where(and_(*conds))
    stmt = stmt.order_by(Donation.donated_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    return AdminDonationsPageResponse(
        items=[DonationOut.model_validate(r) for r in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/admin/donations/stats",
    response_model=DonationStatsResponse,
    summary="Donation statistics (admin)",
)
async def admin_donations_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> DonationStatsResponse:
    total_donations = int((await db.execute(select(func.count()).select_from(Donation))).scalar_one())
    successful = int(
        (await db.execute(select(func.count()).select_from(Donation).where(Donation.status == "success"))).scalar_one()
    )
    pending = int(
        (await db.execute(select(func.count()).select_from(Donation).where(Donation.status == "pending"))).scalar_one()
    )
    ai_weeks_granted = int(
        (await db.execute(select(func.count()).select_from(Donation).where(Donation.ai_week_granted.is_(True)))).scalar_one()
    )
    total_amount_pkr = float(
        (
            await db.execute(
                select(func.coalesce(func.sum(Donation.amount), 0.0)).where(
                    Donation.currency == "PKR",
                    Donation.status == "success",
                )
            )
        ).scalar_one()
    )
    return DonationStatsResponse(
        total_donations=total_donations,
        total_amount_pkr=total_amount_pkr,
        successful=successful,
        pending=pending,
        ai_weeks_granted=ai_weeks_granted,
    )
