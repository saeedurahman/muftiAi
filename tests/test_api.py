"""FastAPI tests: search, pagination, 404, API key, CORS."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from mufti_api.config import Settings
from mufti_api.main import create_app
from mufti_scraper.db.repository import FatwaRepository


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.fixture
def seeded_db_url(db_path: str) -> str:
    url = f"sqlite:///{db_path.replace(os.sep, '/')}"
    repo = FatwaRepository(url)
    with repo.session() as s:
        repo.upsert_fatwa(
            s,
            question="نماز کے اوقات",
            answer="تفصیلی جواب",
            source="TestSource",
            url="https://example.com/f/1",
            category="عبادات",
            date="2026-04-01",
        )
        repo.upsert_fatwa(
            s,
            question="زکوٰۃ کا حساب",
            answer="دوسرا جواب",
            source="TestSource",
            url="https://example.com/f/2",
            category="مالیات",
            date="2026-04-02",
        )
    repo.engine.dispose()
    return url


def test_search_pagination_and_keyword(seeded_db_url: str):
    settings = Settings(
        database_url=seeded_db_url,
        cors_origins=["http://test.local"],
        api_key=None,
        search_cache_ttl_s=60,
        search_cache_max_entries=32,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/search", params={"page": 1, "page_size": 1})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        assert len(data["items"]) == 1
        assert data["page"] == 1
        assert data["page_size"] == 1

        r2 = client.get("/search", params={"query": "نماز"})
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["total"] >= 1
        assert any("نماز" in (x.get("question") or "") for x in d2["items"])


def test_fatwa_by_id_404(seeded_db_url: str):
    settings = Settings(
        database_url=seeded_db_url,
        cors_origins=["*"],
        api_key=None,
        search_cache_ttl_s=60,
        search_cache_max_entries=32,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/fatwa/99999")
        assert r.status_code == 404

        r2 = client.get("/fatwa/1")
        assert r2.status_code == 200
        assert r2.json()["id"] == 1


def test_api_key_required(seeded_db_url: str):
    settings = Settings(
        database_url=seeded_db_url,
        cors_origins=["*"],
        api_key="secret-test-key",
        search_cache_ttl_s=60,
        search_cache_max_entries=32,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/search")
        assert r.status_code == 403

        ok = client.get("/search", headers={"X-API-Key": "secret-test-key"})
        assert ok.status_code == 200


def test_cors_options(seeded_db_url: str):
    settings = Settings(
        database_url=seeded_db_url,
        cors_origins=["http://flutter.test"],
        api_key=None,
        search_cache_ttl_s=60,
        search_cache_max_entries=32,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.options(
            "/search",
            headers={
                "Origin": "http://flutter.test",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code == 200
        assert "access-control-allow-origin" in {k.lower() for k in r.headers.keys()}


def test_invalid_page_size(seeded_db_url: str):
    settings = Settings(
        database_url=seeded_db_url,
        cors_origins=["*"],
        api_key=None,
        search_cache_ttl_s=60,
        search_cache_max_entries=32,
        max_page_size=100,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/search", params={"page_size": 500})
        assert r.status_code == 400
