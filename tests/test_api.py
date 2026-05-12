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

        r3 = client.get(
            "/search",
            params=[("source", "TestSource"), ("source", "MissingSource")],
        )
        assert r3.status_code == 200
        d3 = r3.json()
        assert d3["total"] == 2


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


def test_stats_sources_categories_endpoints(seeded_db_url: str):
    settings = Settings(
        database_url=seeded_db_url,
        cors_origins=["*"],
        api_key=None,
        search_cache_ttl_s=60,
        search_cache_max_entries=32,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        stats = client.get("/stats")
        assert stats.status_code == 200
        d = stats.json()
        assert d["total_fatwas"] == 2
        assert d["total_sources"] == 1
        assert d["total_categories"] == 2
        assert d["sources"][0]["name"] == "TestSource"
        assert d["sources"][0]["count"] == 2

        sources = client.get("/sources")
        assert sources.status_code == 200
        ds = sources.json()
        assert len(ds) == 1
        assert ds[0]["source"] == "TestSource"
        assert ds[0]["count"] == 2

        categories = client.get("/categories", params={"limit": 1})
        assert categories.status_code == 200
        dc = categories.json()
        assert len(dc) == 1
        assert dc[0]["count"] == 1


def test_questions_crud_flow(seeded_db_url: str):
    settings = Settings(
        database_url=seeded_db_url,
        cors_origins=["*"],
        api_key=None,
        search_cache_ttl_s=60,
        search_cache_max_entries=32,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/questions",
            json={
                "question_text": "نماز کے بعد دعا کے آداب کیا ہیں؟ براہ کرم تفصیل سے رہنمائی فرمائیں۔",
                "contact_info": "user@example.com",
                "language": "ur",
            },
        )
        assert created.status_code == 200
        row = created.json()
        qid = row["id"]
        assert row["status"] == "pending"
        assert row["priority"] == "normal"

        listing = client.get("/admin/questions", params={"status": "pending"})
        assert listing.status_code == 200
        ld = listing.json()
        assert ld["total"] >= 1
        assert any(item["id"] == qid for item in ld["items"])

        detail = client.get(f"/admin/questions/{qid}")
        assert detail.status_code == 200
        assert detail.json()["id"] == qid

        st = client.patch(
            f"/admin/questions/{qid}/status",
            json={"status": "reviewing", "admin_notes": "Assigned to mufti"},
        )
        assert st.status_code == 200
        assert st.json()["status"] == "reviewing"

        ans = client.patch(
            f"/admin/questions/{qid}/answer",
            json={"answer_text": "بعد از نماز دعا کرنا جائز ہے۔"},
        )
        assert ans.status_code == 200
        assert ans.json()["status"] == "answered"
        assert ans.json()["answer_text"] == "بعد از نماز دعا کرنا جائز ہے۔"

        rej = client.delete(f"/admin/questions/{qid}")
        assert rej.status_code == 200
        assert rej.json()["message"] == "Question rejected"


def test_question_upload_answer_and_answer_from_text(seeded_db_url: str):
    settings = Settings(
        database_url=seeded_db_url,
        cors_origins=["*"],
        api_key=None,
        search_cache_ttl_s=60,
        search_cache_max_entries=32,
    )
    app = create_app(settings)
    long_txt = (
        "بسم اللہ الرحمن الرحیم۔ یہ ایک طویل جواب ہے جو پچاس حروف سے زیادہ ہونا چاہیے۔ "
        * 2
    )
    with TestClient(app) as client:
        created = client.post(
            "/questions",
            json={
                "question_text": "نماز کے بعد دعا کے آداب کیا ہیں؟ براہ کرم تفصیل سے رہنمائی فرمائیں۔",
                "contact_info": None,
                "language": "ur",
            },
        )
        assert created.status_code == 200
        qid = created.json()["id"]

        up = client.post(
            f"/questions/{qid}/upload-answer",
            files={"file": ("answer.txt", long_txt.encode("utf-8"), "text/plain")},
        )
        assert up.status_code == 200
        ud = up.json()
        assert ud["question_id"] == qid
        assert ud["status"] == "answered"
        assert ud["extracted_length"] >= 50
        assert "message" in ud

        bad = client.post(
            f"/questions/{qid}/upload-answer",
            files={"file": ("x.doc", b"hi", "application/msword")},
        )
        assert bad.status_code == 400
        assert bad.json()["detail"] == "Only PDF or TXT files allowed"

        created2 = client.post(
            "/questions",
            json={
                "question_text": "زکوٰۃ کے مسائل میں کیا فرق ہے؟ تفصیل سے وضاحت فرمائیں۔",
                "contact_info": None,
                "language": "ur",
            },
        )
        qid2 = created2.json()["id"]
        paste = client.post(
            f"/questions/{qid2}/answer-from-text",
            json={"answer_text": long_txt},
        )
        assert paste.status_code == 200
        assert paste.json()["question_id"] == qid2


def test_search_miss_logging_and_admin_endpoints(seeded_db_url: str):
    settings = Settings(
        database_url=seeded_db_url,
        cors_origins=["*"],
        api_key=None,
        search_cache_ttl_s=60,
        search_cache_max_entries=32,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        # This should log a miss in background (total == 0).
        r = client.get("/search", params={"query": "this-query-should-not-match-anything"})
        assert r.status_code == 200
        assert r.json()["total"] == 0

        misses = client.get("/admin/search-misses")
        assert misses.status_code == 200
        md = misses.json()
        assert md["total"] >= 1
        first = md["items"][0]
        miss_id = first["id"]
        assert first["resolved"] is False

        stats = client.get("/admin/search-misses/stats")
        assert stats.status_code == 200
        sd = stats.json()
        assert sd["total_misses"] >= 1
        assert sd["unresolved"] >= 1

        resolved = client.patch(f"/admin/search-misses/{miss_id}/resolve")
        assert resolved.status_code == 200
        assert resolved.json()["resolved"] is True

        unresolved_list = client.get("/admin/search-misses", params={"resolved": False})
        assert unresolved_list.status_code == 200
