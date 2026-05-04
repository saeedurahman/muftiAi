"""FastAPI dependencies: DB session, optional API key, settings."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from mufti_api.config import Settings
from mufti_api.database import get_session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def verify_api_key(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> None:
    """If API_KEY env is set, require matching X-API-Key header."""
    settings: Settings = request.app.state.settings
    if settings.api_key is None:
        return
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing API key. Send header: X-API-Key",
        )
