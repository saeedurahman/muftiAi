"""
Fatwa REST API for Flutter / web clients.

Run locally::
    uvicorn mufti_api.main:app --reload --host 0.0.0.0 --port 8000

Or::
    mufti-api

Environment::
    MUFTI_DATABASE_URL   sqlite:///fatawa.db (default) or postgresql://...
    CORS_ORIGINS         * or comma-separated origins
    API_KEY              if set, require header X-API-Key
    SEARCH_CACHE_TTL_SECONDS   default 60
    SEARCH_CACHE_MAX_ENTRIES   default 256

Flutter example (package:http)::

    final uri = Uri.parse('http://10.0.2.2:8000/search')
        .replace(queryParameters: {'query': 'نماز', 'page': '1', 'page_size': '20'});
    final headers = <String, String>{'Accept': 'utf-8'};
    if (apiKey != null) headers['X-API-Key'] = apiKey;
    final res = await http.get(uri, headers: headers);
    final data = jsonDecode(utf8.decode(res.bodyBytes));
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from cachetools import TTLCache
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mufti_api.config import Settings
from mufti_api.database import create_tables_if_needed, dispose_engine, init_engine
from mufti_api.routers import fatwas


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_engine(resolved.database_url)
        await create_tables_if_needed()
        app.state.settings = resolved
        app.state.search_cache = TTLCache(
            maxsize=resolved.search_cache_max_entries,
            ttl=resolved.search_cache_ttl_s,
        )
        yield
        await dispose_engine()

    app = FastAPI(
        title="Mufti Fatwa API",
        version="0.1.0",
        description=__doc__ or "",
        lifespan=lifespan,
    )

    if resolved.cors_origins == ["*"]:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["GET", "OPTIONS"],
            allow_headers=["*"],
        )
    else:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "OPTIONS"],
            allow_headers=["*"],
        )

    app.include_router(fatwas.router)
    return app


app = create_app()


def run() -> None:
    """Console entry point: ``mufti-api``."""
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("mufti_api.main:app", host=host, port=port, reload=False)
