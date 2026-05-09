"""
AI chat endpoints bound to a specific fatwa.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mufti_api.deps import get_db
from mufti_api.schemas import ChatMessageOut, ChatRequest, ChatResponse, ChatSessionOut
from mufti_scraper.db.models import ChatMessage, ChatSession, Fatwa, Subscription, User

router = APIRouter(tags=["chat"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_active_subscription(sub: Subscription | None) -> bool:
    if sub is None:
        return False
    if sub.status != "active":
        return False
    if sub.expires_at is None:
        return False
    return sub.expires_at > _now()


@router.post(
    "/fatwas/{fatwa_id}/chat",
    response_model=ChatResponse,
    summary="Ask question about a specific fatwa",
)
async def fatwa_chat(
    fatwa_id: int,
    body: ChatRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> ChatResponse:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="auth_required")
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
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        )
    ).scalars().first()
    if not _is_active_subscription(sub):
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "subscription_required",
                "message": "AI چیٹ کے لیے سبسکرپشن ضروری ہے",
                "subscription_required": True,
            },
        )

    fatwa = (await db.execute(select(Fatwa).where(Fatwa.id == fatwa_id))).scalar_one_or_none()
    if fatwa is None:
        raise HTTPException(status_code=404, detail="Fatwa not found")

    if body.session_id is not None:
        session = (await db.execute(select(ChatSession).where(ChatSession.id == body.session_id))).scalar_one_or_none()
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if int(session.user_id) != int(user.id) or int(session.fatwa_id) != int(fatwa_id):
            raise HTTPException(status_code=400, detail="Session does not match user/fatwa")
    else:
        session = ChatSession(
            user_id=user.id,
            fatwa_id=fatwa_id,
            created_at=_now(),
            last_message_at=_now(),
            is_active=True,
        )
        db.add(session)
        await db.flush()

    hist_rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session.id)
            .order_by(ChatMessage.created_at.desc())
            .limit(10)
        )
    ).scalars().all()
    hist_rows = list(reversed(hist_rows))

    system_prompt = f"""
آپ ایک اسلامی سکالر ہیں۔ آپ صرف درج ذیل فتوے کی بنیاد پر سوالات کے جوابات دیں گے۔

فتوے کا سوال: {fatwa.question}
فتوے کا جواب: {fatwa.answer}

اہم ہدایات:
- صرف اس فتوے سے متعلق سوالات کا جواب دیں
- اگر سوال اس فتوے سے باہر ہو تو کہیں: "یہ سوال اس فتوے کے دائرے سے باہر ہے"
- سادہ اردو میں جواب دیں
- 3-5 جملوں میں مکمل کریں
- کوئی نیا فتویٰ جاری نہ کریں
""".strip()

    messages = [{"role": m.role, "content": m.content} for m in hist_rows]
    messages.append({"role": "user", "content": body.message})

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="ai_unavailable")

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=system_prompt,
            messages=messages,
        )
        assistant_reply = "".join(
            getattr(block, "text", "") for block in getattr(response, "content", [])
        ).strip()
        if not assistant_reply:
            raise ValueError("Empty assistant response")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"ai_unavailable: {e}") from e

    # TODO Phase 2: Add rate limiting — max 20 messages per user per day
    user_msg = ChatMessage(
        session_id=session.id,
        role="user",
        content=body.message.strip(),
        created_at=_now(),
    )
    assistant_msg = ChatMessage(
        session_id=session.id,
        role="assistant",
        content=assistant_reply,
        created_at=_now(),
    )
    db.add(user_msg)
    db.add(assistant_msg)
    session.last_message_at = _now()
    await db.commit()
    await db.refresh(user_msg)
    await db.refresh(assistant_msg)

    return ChatResponse(
        session_id=session.id,
        user_message=ChatMessageOut.model_validate(user_msg),
        assistant_message=ChatMessageOut.model_validate(assistant_msg),
        subscription_required=False,
    )


@router.get(
    "/fatwas/{fatwa_id}/chat/{session_id}",
    response_model=ChatSessionOut,
    summary="Get full chat history for a session",
)
async def get_chat_session(
    fatwa_id: int,
    session_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> ChatSessionOut:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="auth_required")
    try:
        user_id = int(x_user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid X-User-ID") from e

    session = (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if int(session.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    if int(session.fatwa_id) != int(fatwa_id):
        raise HTTPException(status_code=400, detail="Session does not match fatwa")

    msgs = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session.id)
            .order_by(ChatMessage.created_at.asc())
        )
    ).scalars().all()
    return ChatSessionOut(
        session_id=session.id,
        fatwa_id=session.fatwa_id,
        messages=[ChatMessageOut.model_validate(m) for m in msgs],
    )


@router.get(
    "/user/chats",
    response_model=list[ChatSessionOut],
    summary="Get recent chat sessions for user",
)
async def get_user_chats(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_user_id: str | None = Header(None, alias="X-User-ID"),
) -> list[ChatSessionOut]:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="auth_required")
    try:
        user_id = int(x_user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid X-User-ID") from e

    sessions = (
        await db.execute(
            select(ChatSession)
            .where(ChatSession.user_id == user_id, ChatSession.is_active.is_(True))
            .order_by(ChatSession.last_message_at.desc())
            .limit(10)
        )
    ).scalars().all()

    out: list[ChatSessionOut] = []
    for s in sessions:
        msgs = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == s.id)
                .order_by(ChatMessage.created_at.asc())
                .limit(10)
            )
        ).scalars().all()
        out.append(
            ChatSessionOut(
                session_id=s.id,
                fatwa_id=s.fatwa_id,
                messages=[ChatMessageOut.model_validate(m) for m in msgs],
            )
        )
    return out
