"""
Guest/social auth, subscription checks, and AI trial management endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mufti_api.deps import get_db, verify_api_key
from mufti_api.schemas import (
    AITrialUseResponse,
    AdminSubscriptionUpsert,
    AuthGuestResponse,
    DashboardLoginRequest,
    GoogleDashboardAuthRequest,
    GuestRegister,
    SocialAuthRequest,
    SubscriptionOut,
    UserOut,
    UsersStatusPageResponse,
    UserStatusOut,
)
from mufti_scraper.db.models import AITrial, Subscription, User

router = APIRouter(tags=["auth"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_subscription_valid(sub: Subscription | None) -> bool:
    if sub is None:
        return False
    if sub.status != "active":
        return False
    if sub.expires_at is None:
        return False
    return sub.expires_at > _now()


def _to_subscription_out(sub: Subscription | None) -> SubscriptionOut | None:
    if sub is None:
        return None
    return SubscriptionOut(
        id=sub.id,
        plan=sub.plan,
        status=sub.status,
        started_at=sub.started_at,
        expires_at=sub.expires_at,
        is_valid=_is_subscription_valid(sub),
    )


async def _ensure_trial(db: AsyncSession, user_id: int) -> AITrial:
    """Return one AI trial row per user; tolerate legacy duplicate rows in DB."""
    stmt = (
        select(AITrial)
        .where(AITrial.user_id == user_id)
        .order_by(AITrial.id.desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        row = AITrial(user_id=user_id, used_count=0, max_count=3)
        db.add(row)
        await db.flush()
    return row


async def _latest_subscription(db: AsyncSession, user_id: int) -> Subscription | None:
    stmt = (
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc(), Subscription.id.desc())
    )
    return (await db.execute(stmt)).scalars().first()


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


def _build_user_status(user: User, sub: Subscription | None, trial: AITrial) -> UserStatusOut:
    is_valid = _is_subscription_valid(sub)
    remaining = max(0, int(trial.max_count) - int(trial.used_count))
    return UserStatusOut(
        user=UserOut.model_validate(user),
        subscription=_to_subscription_out(sub),
        ai_trials_remaining=remaining,
        can_save=is_valid,
        can_use_ai=(is_valid or remaining > 0),
    )


@router.post("/auth/guest", response_model=AuthGuestResponse, summary="Register/login guest user")
async def auth_guest(
    payload: GuestRegister,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthGuestResponse:
    row = (await db.execute(select(User).where(User.guest_id == payload.guest_id))).scalar_one_or_none()
    if row is None:
        row = User(
            guest_id=payload.guest_id.strip(),
            provider="guest",
            is_active=True,
            created_at=_now(),
            last_seen_at=_now(),
        )
        db.add(row)
        await db.flush()
        await _ensure_trial(db, row.id)
    else:
        row.last_seen_at = _now()
        await _ensure_trial(db, row.id)
    await db.commit()
    await db.refresh(row)
    return AuthGuestResponse(user=UserOut.model_validate(row), token=str(row.id))


@router.post("/auth/social", response_model=UserOut, summary="Register/login via social provider")
async def auth_social(
    payload: SocialAuthRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    # TODO: verify provider_token with Google/Apple SDK/server APIs.
    provider_uid = payload.provider_token.strip()
    if not provider_uid:
        raise HTTPException(status_code=400, detail="provider_token is required")

    row = (
        await db.execute(
            select(User).where(
                User.provider == payload.provider,
                User.provider_uid == provider_uid,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = User(
            provider=payload.provider,
            provider_uid=provider_uid,
            is_active=True,
            created_at=_now(),
            last_seen_at=_now(),
        )
        db.add(row)
        await db.flush()
        await _ensure_trial(db, row.id)

    # Optional guest -> social migration of trial usage.
    if payload.guest_id:
        guest = (await db.execute(select(User).where(User.guest_id == payload.guest_id))).scalar_one_or_none()
        if guest is not None and guest.id != row.id:
            guest_trial = await _ensure_trial(db, guest.id)
            social_trial = await _ensure_trial(db, row.id)
            social_trial.used_count = max(int(social_trial.used_count), int(guest_trial.used_count))
            guest.is_active = False

    row.last_seen_at = _now()
    await db.commit()
    await db.refresh(row)
    return UserOut.model_validate(row)


@router.post(
    "/auth/google-dashboard",
    response_model=AuthGuestResponse,
    summary="Dashboard login with Google token (placeholder)",
)
async def auth_google_dashboard(
    payload: GoogleDashboardAuthRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthGuestResponse:
    # TODO: Replace with real Google OAuth in production
    google_token = payload.google_token.strip()
    if not google_token:
        raise HTTPException(status_code=400, detail="google_token is required")

    # Phase 1 placeholder token format: email:<user@example.com>
    email = ""
    if google_token.startswith("email:"):
        email = google_token.split(":", 1)[1].strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="invalid_google_token")

    row = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    if row.role == "user" or row.role != payload.expected_role:
        raise HTTPException(status_code=403, detail="access_denied")

    row.dashboard_access = row.role in {"admin", "mufti"}
    row.last_seen_at = _now()
    await db.commit()
    await db.refresh(row)
    return AuthGuestResponse(user=UserOut.model_validate(row), token=str(row.id))


@router.post(
    "/auth/dashboard-login",
    response_model=AuthGuestResponse,
    summary="Development dashboard login (phase 1)",
)
async def auth_dashboard_login(
    payload: DashboardLoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthGuestResponse:
    # TODO: Replace with real Google OAuth in production
    email = payload.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email is required")

    row = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if row is None:
        row = User(
            email=email,
            name=(payload.name.strip() if payload.name else email.split("@")[0]),
            provider="google",
            is_active=True,
            role=payload.role,
            dashboard_access=True,
            created_at=_now(),
            last_seen_at=_now(),
        )
        db.add(row)
        await db.flush()
        await _ensure_trial(db, row.id)
    else:
        row.role = payload.role
        row.dashboard_access = True
        row.last_seen_at = _now()
        if payload.name and payload.name.strip():
            row.name = payload.name.strip()
        await _ensure_trial(db, row.id)
    await db.commit()
    await db.refresh(row)
    return AuthGuestResponse(user=UserOut.model_validate(row), token=str(row.id))


@router.get("/user/status", response_model=UserStatusOut, summary="Get user/subscription/trials status")
async def user_status(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
    x_guest_id: str | None = Header(None, alias="X-Guest-ID"),
) -> UserStatusOut:
    user = await _resolve_user(db, x_user_id, x_guest_id)
    sub = await _latest_subscription(db, user.id)
    trial = await _ensure_trial(db, user.id)
    await db.commit()
    return _build_user_status(user, sub, trial)


@router.get("/user/subscription", response_model=SubscriptionOut | None, summary="Get user subscription")
async def user_subscription(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> SubscriptionOut | None:
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-ID")
    user = await _resolve_user(db, x_user_id, None)
    sub = await _latest_subscription(db, user.id)
    return _to_subscription_out(sub)


@router.post("/user/ai-trial/use", response_model=AITrialUseResponse, summary="Consume one AI trial")
async def use_ai_trial(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
    x_guest_id: str | None = Header(None, alias="X-Guest-ID"),
) -> AITrialUseResponse:
    user = await _resolve_user(db, x_user_id, x_guest_id)
    sub = await _latest_subscription(db, user.id)
    trial = await _ensure_trial(db, user.id)

    if _is_subscription_valid(sub):
        remaining = max(0, int(trial.max_count) - int(trial.used_count))
        return AITrialUseResponse(trials_remaining=remaining, used=False)

    if int(trial.used_count) >= int(trial.max_count):
        return JSONResponse(
            status_code=403,
            content={"detail": "trial_exhausted", "trials_remaining": 0},
        )

    trial.used_count = int(trial.used_count) + 1
    await db.commit()
    remaining = max(0, int(trial.max_count) - int(trial.used_count))
    return AITrialUseResponse(trials_remaining=remaining, used=True)


@router.post(
    "/admin/subscriptions",
    response_model=SubscriptionOut,
    summary="Create/update subscription (admin)",
)
async def admin_upsert_subscription(
    payload: AdminSubscriptionUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
) -> SubscriptionOut:
    user = (await db.execute(select(User).where(User.id == payload.user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    row = Subscription(
        user_id=payload.user_id,
        plan=payload.plan,
        status=payload.status,
        started_at=_now(),
        expires_at=payload.expires_at,
        payment_provider=payload.payment_provider,
        payment_ref=payload.payment_ref,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _to_subscription_out(row)  # type: ignore[return-value]


@router.get(
    "/admin/users",
    response_model=UsersStatusPageResponse,
    summary="List users with subscription status (admin)",
)
async def admin_list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(verify_api_key)],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    provider: str | None = Query(None),
) -> UsersStatusPageResponse:
    conds = []
    if provider and provider.strip():
        conds.append(User.provider == provider.strip())

    count_stmt = select(func.count()).select_from(User)
    if conds:
        from sqlalchemy import and_

        count_stmt = count_stmt.where(and_(*conds))
    total = int((await db.execute(count_stmt)).scalar_one())

    stmt = select(User)
    if conds:
        from sqlalchemy import and_

        stmt = stmt.where(and_(*conds))
    stmt = stmt.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    users = (await db.execute(stmt)).scalars().all()

    items: list[UserStatusOut] = []
    for user in users:
        sub = await _latest_subscription(db, user.id)
        trial = await _ensure_trial(db, user.id)
        items.append(_build_user_status(user, sub, trial))

    await db.commit()
    return UsersStatusPageResponse(items=items, page=page, page_size=page_size, total=total)
