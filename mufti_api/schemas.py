"""Pydantic models for API responses (OpenAPI / Flutter clients)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal

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
    snippet: str | None = None


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


class SourceCount(BaseModel):
    source: str
    count: int = Field(ge=0)


class CategoryCount(BaseModel):
    category: str
    count: int = Field(ge=0)


class StatsSourceCount(BaseModel):
    name: str
    count: int = Field(ge=0)


class StatsResponse(BaseModel):
    total_fatwas: int = Field(ge=0)
    sources: list[StatsSourceCount] = Field(default_factory=list)
    total_sources: int = Field(ge=0)
    total_categories: int = Field(ge=0)


QuestionStatus = Literal["pending", "reviewing", "answered", "published", "rejected"]
QuestionPriority = Literal["normal", "high", "urgent"]
UserRole = Literal["user", "admin", "mufti"]
ManualFatwaStatus = Literal["draft", "published"]


class ManualFatwaCreate(BaseModel):
    question: str = Field(min_length=20, max_length=50_000)
    answer: str = Field(min_length=50, max_length=500_000)
    category: str | None = Field(default=None, max_length=200)
    source_name: str | None = Field(default=None, max_length=200)
    pdf_url: str | None = Field(default=None, max_length=500)
    status: ManualFatwaStatus = "published"


class ManualFatwaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question: str
    answer: str
    category: str | None = None
    source_name: str
    added_by_user_id: int
    added_by_role: Literal["admin", "mufti"]
    pdf_url: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime
    fatwa_id: int | None = None


class ManualFatwasPageResponse(BaseModel):
    items: list[ManualFatwaOut] = Field(default_factory=list)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class CategoryDistinctItem(BaseModel):
    category: str


class QuestionCreate(BaseModel):
    question_text: str = Field(min_length=20, max_length=2000)
    contact_info: str | None = None
    language: str = "ur"


class QuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question_text: str
    contact_info: str | None = None
    language: str
    status: QuestionStatus
    priority: QuestionPriority
    assigned_to: str | None = None
    assigned_mufti_id: int | None = None
    assigned_at: datetime | None = None
    answer_text: str | None = None
    answered_at: datetime | None = None
    payment_amount: float | None = None
    payment_status: Literal["unpaid", "paid"] = "unpaid"
    fatwa_id: int | None = None
    submitted_at: datetime
    updated_at: datetime


class QuestionAnswerWithCategoryRequest(BaseModel):
    answer_text: str = Field(min_length=50, max_length=500_000)
    category: str | None = Field(default=None, max_length=512)
    publish_as_fatwa: bool = True


class QuestionAnswerWithCategoryResponse(BaseModel):
    question: QuestionOut
    fatwa_id: int | None = None


class QuestionStatusUpdate(BaseModel):
    status: QuestionStatus
    admin_notes: str | None = None


class QuestionAnswerUpdate(BaseModel):
    answer_text: str
    status: QuestionStatus = "answered"


class UploadAnswerResponse(BaseModel):
    question_id: int
    extracted_length: int
    status: str
    preview: str
    message: str
    publish_as_fatwa: bool = False
    fatwa_id: int | None = None


class AnswerFromTextRequest(BaseModel):
    answer_text: str = Field(min_length=1, max_length=500_000)


class QuestionsPageResponse(BaseModel):
    items: list[QuestionOut] = Field(default_factory=list)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=50)
    total: int = Field(ge=0)


class SearchMissOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    query: str
    results_count: int
    source_filter: str | None = None
    category_filter: str | None = None
    searched_at: datetime
    user_agent: str | None = None
    resolved: bool


class SearchMissesPageResponse(BaseModel):
    items: list[SearchMissOut] = Field(default_factory=list)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class SearchMissTopQuery(BaseModel):
    query: str
    count: int = Field(ge=0)


class SearchMissStatsResponse(BaseModel):
    total_misses: int = Field(ge=0)
    unresolved: int = Field(ge=0)
    top_queries: list[SearchMissTopQuery] = Field(default_factory=list)


class GuestRegister(BaseModel):
    guest_id: str
    device_info: str | None = None


class SocialAuthRequest(BaseModel):
    provider: Literal["google", "apple"]
    provider_token: str
    guest_id: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    guest_id: str | None = None
    email: str | None = None
    name: str | None = None
    provider: str | None = None
    is_active: bool
    role: UserRole = "user"
    dashboard_access: bool = False
    created_at: datetime


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plan: str
    status: str
    started_at: datetime
    expires_at: datetime | None = None
    is_valid: bool


class UserStatusOut(BaseModel):
    user: UserOut
    subscription: SubscriptionOut | None = None
    ai_trials_remaining: int = Field(ge=0)
    can_save: bool
    can_use_ai: bool


class AuthGuestResponse(BaseModel):
    user: UserOut
    token: str


class DashboardLoginRequest(BaseModel):
    email: str
    role: Literal["admin", "mufti"]
    name: str | None = None


class GoogleDashboardAuthRequest(BaseModel):
    google_token: str
    expected_role: Literal["admin", "mufti"]


class MuftiCreate(BaseModel):
    user_id: int
    display_name: str
    specialization: str | None = None
    per_question_rate: float = 15.0
    bio: str | None = None


class MuftiUpdate(BaseModel):
    display_name: str | None = None
    specialization: str | None = None
    per_question_rate: float | None = None
    is_available: bool | None = None
    bio: str | None = None


class MuftiOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    display_name: str
    specialization: str | None = None
    per_question_rate: float
    is_available: bool
    total_questions_answered: int
    total_earned: float
    joined_at: datetime
    bio: str | None = None
    user: UserOut


class MuftiStatsOut(BaseModel):
    mufti_id: int
    display_name: str
    this_month_questions: int
    this_month_earned: float
    pending_questions: int
    answered_questions: int
    avg_response_time_hours: float | None = None


class QuestionAssignRequest(BaseModel):
    mufti_id: int


class PaymentRateUpdate(BaseModel):
    per_question_rate: float


class MuftiPaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mufti_id: int
    month: str
    questions_answered: int
    per_question_rate: float
    total_amount: float
    status: Literal["pending", "paid"]
    paid_at: datetime | None = None
    payment_ref: str | None = None


class PaymentMonthRequest(BaseModel):
    month: str = Field(min_length=7, max_length=7, description="YYYY-MM")


class PaymentMarkPaidRequest(BaseModel):
    payment_ref: str | None = None


class MuftiDetailResponse(BaseModel):
    mufti: MuftiOut
    stats: MuftiStatsOut


class MuftiEarningsResponse(BaseModel):
    this_month_questions: int
    this_month_earned: float
    total_earned: float
    payment_history: list[MuftiPaymentOut] = Field(default_factory=list)


class MuftiMeResponse(BaseModel):
    mufti: MuftiOut
    stats: MuftiStatsOut


class AITrialUseResponse(BaseModel):
    trials_remaining: int = Field(ge=0)
    used: bool


class AdminSubscriptionUpsert(BaseModel):
    user_id: int
    plan: Literal["free", "monthly", "yearly"]
    status: Literal["active", "expired", "cancelled"]
    expires_at: datetime | None = None
    payment_ref: str | None = None
    payment_provider: str | None = None


class UsersStatusPageResponse(BaseModel):
    items: list[UserStatusOut] = Field(default_factory=list)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class DonationCreate(BaseModel):
    amount: float
    currency: Literal["PKR", "USD"] = "PKR"
    payment_method: Literal["stripe", "jazzcash", "easypaisa", "manual"]


class DonationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    amount: float
    currency: str
    payment_method: str
    payment_ref: str | None = None
    status: str
    ai_week_granted: bool
    donated_at: datetime
    updated_at: datetime


class DonationVerifyResponse(BaseModel):
    donation_id: int
    status: str
    ai_week_granted: bool


class DonationCreateResponse(BaseModel):
    donation_id: int
    amount: float
    currency: str
    status: str


class DonationHistoryResponse(BaseModel):
    donations: list[DonationOut] = Field(default_factory=list)
    total_donated_pkr: float = 0.0


class DonationStatsResponse(BaseModel):
    total_donations: int = Field(ge=0)
    total_amount_pkr: float = 0.0
    successful: int = Field(ge=0)
    pending: int = Field(ge=0)
    ai_weeks_granted: int = Field(ge=0)


class AdminDonationsPageResponse(BaseModel):
    items: list[DonationOut] = Field(default_factory=list)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class TranslationRequest(BaseModel):
    target_language: Literal["en", "ar"]


class TranslationOut(BaseModel):
    fatwa_id: int
    language: str
    original_question: str
    original_answer: str
    translated_question: str
    translated_answer: str
    source: Literal["cached", "fresh"]
    cached_until: datetime


class SummaryOut(BaseModel):
    fatwa_id: int
    language: str
    original_question: str
    summary: str
    source: Literal["cached", "fresh"]
    cached_until: datetime | None = None
    trials_remaining: int | None = None


class RelatedFatwaItem(BaseModel):
    id: int
    question: str
    category: str | None = None
    source: str | None = None
    score: float


class RelatedFatwasOut(BaseModel):
    fatwa_id: int
    related: list[RelatedFatwaItem] = Field(default_factory=list)
    source: Literal["cached", "fresh"]


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    created_at: datetime


class ChatSessionOut(BaseModel):
    session_id: int
    fatwa_id: int
    messages: list[ChatMessageOut] = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str = Field(min_length=5, max_length=500)
    session_id: int | None = None


class ChatResponse(BaseModel):
    session_id: int
    user_message: ChatMessageOut
    assistant_message: ChatMessageOut
    subscription_required: bool = False
