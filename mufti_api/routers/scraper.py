"""
Admin scraper management endpoints (background execution + in-memory status).
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from mufti_api.deps import verify_api_key
from mufti_scraper.cli import run_scraper
from mufti_scraper.config import ScraperConfig
from mufti_scraper.sources.registry import all_source_names

router = APIRouter(tags=["scraper"])


class ScrapeStartRequest(BaseModel):
    sources: list[str] = Field(default_factory=lambda: all_source_names())
    limit: int = Field(100, ge=1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_scraper_task(app_state: Any, task_id: str, sources: list[str], limit: int) -> None:
    """Runs scraper in background; updates in-memory status/history safely."""
    status = app_state.scrape_status
    history = app_state.scrape_history

    try:
        total_progress = {"inserted": 0, "skipped": 0, "failed": 0, "current_source": ""}
        for src in sources:
            status["progress"]["current_source"] = src
            total_progress["current_source"] = src
            config = ScraperConfig.from_env()
            stats = await asyncio.to_thread(run_scraper, config, [src], limit, False)
            total_progress["inserted"] += int(stats.get("inserted", 0))
            total_progress["skipped"] += int(stats.get("skipped", 0))
            total_progress["failed"] += int(stats.get("failed", 0))
            status["progress"] = deepcopy(total_progress)

        status["progress"]["current_source"] = ""
        status["is_running"] = False
        status["completed_at"] = _now_iso()
        status["error"] = None
    except Exception as e:
        status["is_running"] = False
        status["completed_at"] = _now_iso()
        status["error"] = str(e)
        status["progress"]["current_source"] = ""
    finally:
        # Keep last 10 runs only.
        history.append(deepcopy(status))
        if len(history) > 10:
            del history[:-10]


@router.post("/admin/scrape", summary="Start scraper in background (admin)")
async def start_scrape(
    body: ScrapeStartRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    _: Annotated[None, Depends(verify_api_key)],
) -> dict[str, str]:
    request_state = request.app.state
    lock: asyncio.Lock = request_state.scrape_lock
    async with lock:
        status = request_state.scrape_status
        if status.get("is_running"):
            raise HTTPException(status_code=409, detail="Scraper already running")

        task_id = str(uuid4())
        status.update(
            {
                "is_running": True,
                "task_id": task_id,
                "started_at": _now_iso(),
                "sources": body.sources,
                "limit": body.limit,
                "progress": {
                    "inserted": 0,
                    "skipped": 0,
                    "failed": 0,
                    "current_source": "",
                },
                "completed_at": None,
                "error": None,
            }
        )

        background_tasks.add_task(
            _run_scraper_task,
            request_state,
            task_id,
            body.sources,
            body.limit,
        )

    return {"task_id": task_id, "message": "Scraping started"}


@router.get("/admin/scrape/status", summary="Current scraper status (admin)")
async def scrape_status(
    request: Request,
    _: Annotated[None, Depends(verify_api_key)],
) -> dict[str, Any]:
    request_state = request.app.state
    return deepcopy(request_state.scrape_status)


@router.get("/admin/scrape/history", summary="Last 10 scraper runs (admin)")
async def scrape_history(
    request: Request,
    _: Annotated[None, Depends(verify_api_key)],
) -> list[dict[str, Any]]:
    request_state = request.app.state
    return deepcopy(request_state.scrape_history[-10:])
