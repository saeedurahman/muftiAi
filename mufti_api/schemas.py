"""Pydantic models for API responses (OpenAPI / Flutter clients)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_EXAMPLE_FATWA: dict[str, Any] = {
    "id": 1,
    "question": "نماز میں موبائل رکھنے کا کیا حکم ہے؟",
    "answer": "… مستند جواب کا متن …",
    "source": "Jamia Binoria (Darul Ifta)",
    "url": "https://www.banuri.edu.pk/readquestion/example-slug/01-01-2026",
    "category": "نماز",
    "date": "2026-01-01",
}


class FatwaOut(BaseModel):
    """Single fatwa record returned to clients (no internal scrape metadata)."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={"examples": [_EXAMPLE_FATWA]},
    )

    id: int
    question: str = ""
    answer: str = ""
    source: str
    url: str
    category: str | None = None
    date: str | None = None


class SearchResponse(BaseModel):
    """Paginated search / browse results."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "items": [_EXAMPLE_FATWA],
                    "page": 1,
                    "page_size": 20,
                    "total": 1,
                }
            ]
        }
    )

    items: list[FatwaOut] = Field(default_factory=list)
    page: int = Field(ge=1, description="1-based page index")
    page_size: int = Field(ge=1, le=100, description="Rows per page (max 100)")
    total: int = Field(ge=0, description="Total rows matching filters (all pages)")
