from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class LawDocumentRecord(BaseModel):
    law_id: str
    law_mst: str | None = None
    title: str
    law_type: str | None = None
    ministry: dict[str, Any] | list[Any] | None = None
    promulgation_date: date | None = None
    effective_date: date | None = None
    status: str | None = None
    source_url: str | None = None
    aliases: list[str] = Field(default_factory=list)
    content_hash: str


class LawChunkRecord(BaseModel):
    chunk_type: str
    chapter_title: str | None = None
    section_title: str | None = None
    article_no: str | None = None
    article_title: str | None = None
    text: str
    search_text: str
    order_index: int
    token_count: int
    content_hash: str
    embedding: list[float] | None = None


class IngestedLaw(BaseModel):
    document: LawDocumentRecord
    chunks: list[LawChunkRecord]


class Citation(BaseModel):
    chunk_id: str
    law_id: str
    law_title: str
    article_no: str | None = None
    article_title: str | None = None
    chunk_type: str
    quote: str
    score: float


class RetrievedChunk(BaseModel):
    id: str
    document_id: str
    law_id: str
    law_title: str
    chunk_type: str
    chapter_title: str | None = None
    section_title: str | None = None
    article_no: str | None = None
    article_title: str | None = None
    text: str
    search_text: str
    score: float
    source: Literal["exact", "lexical", "vector", "merged"]
    # v3: 고시번호·자율규약 issuer 노출 (변호사급 인용 형식 지원)
    document_type: str | None = None   # 'statute' | 'administrative_rule' | 'voluntary_code'
    issuer: str | None = None          # 발령기관·협회명
    promulgation_no: str | None = None  # 고시·발령번호 (예: "제2022-26호")


class PrecedentResult(BaseModel):
    case_id: str                    # 판례일련번호
    case_name: str                  # 사건명
    case_number: str                # 사건번호
    court_name: str                 # 법원명
    source_id: str = "prec"
    source_label: str = "판례"
    provider: str | None = None
    judgment_date: str | None = None  # 선고일자
    judgment_type: str | None = None  # 판결유형 (승소/패소/일부승소 등)
    summary: str | None = None       # 판시사항 or 판결요지
    url: str | None = None           # 판례 상세 URL
    external_url: str | None = None  # 공식 원문 링크


class PrecedentSearchResponse(BaseModel):
    query: str
    results: list[PrecedentResult]
    total_count: int
    win_rate: float | None = None    # 원고 승소 비율 (if calculable)
    source_counts: dict[str, int] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)
    note: str = "판례 통계는 검색된 결과 내 참고 수치이며, 법률 조언이 아닙니다."


class PrecedentSource(BaseModel):
    source_id: str
    label: str
    provider: str
    request_format: str
    default_enabled: bool = False
    description: str | None = None


class PrecedentDetailSection(BaseModel):
    label: str
    content: str


class PrecedentDetailResponse(BaseModel):
    result: PrecedentResult
    sections: list[PrecedentDetailSection] = Field(default_factory=list)
    note: str = "원문과 요약은 참고용이며, 최종 판단 전에는 반드시 공식 원문을 다시 확인해 주세요."


class DocumentOverview(BaseModel):
    nature: str = ""
    principals: list[str] = Field(default_factory=list)
    standpoint: str = ""


class ReviewObservationIssue(BaseModel):
    severity: str = "medium"
    concern: str = ""
    suggestion: str = ""
    basis: str | None = None


class ReviewObservation(BaseModel):
    locator: str = ""
    locator_title: str = ""
    original_text: str = ""
    severity: str = "medium"
    issues: list[ReviewObservationIssue] = Field(default_factory=list)


class ReviewGap(BaseModel):
    topic: str = ""
    reason: str = ""
    suggestion: str = ""


class ExternalConsideration(BaseModel):
    section_title: str = ""
    topic: str = ""
    detail: str = ""
    suggestion: str | None = None


class JudgmentBlock(BaseModel):
    is_judgment_question: bool = False
    verdict: str | None = None
    short_answer: str = ""
    reasoning: str = ""
    key_authorities: list[str] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)
    typical_path: str = ""
    contradicting_views: list[str] = Field(default_factory=list)
    rejected_citations: list[dict[str, Any]] = Field(default_factory=list)


class DocumentReviewResponse(BaseModel):
    document_overview: DocumentOverview = Field(default_factory=DocumentOverview)
    document_type: str = ""
    summary: str = ""
    institutional_frame: str = ""
    institutional_axes: list[dict[str, Any]] = Field(default_factory=list)
    review_axes: list[str] = Field(default_factory=list)
    observations: list[ReviewObservation] = Field(default_factory=list)
    gaps: list[ReviewGap] = Field(default_factory=list)
    external_considerations: list[ExternalConsideration] = Field(default_factory=list)
    risk_scenarios: list[dict[str, Any]] = Field(default_factory=list)
    overall_risk: str = "n/a"
    key_points: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    rejected_citations: list[dict[str, Any]] = Field(default_factory=list)
    judgment: JudgmentBlock | None = None
    disclaimer: str = ""


class ChatRequest(BaseModel):
    question: str
    stream: bool = False
    save: bool = False
    channel: Literal["web", "mobile", "admin"] = "web"
    user_id: str | None = None


class ChatAnswer(BaseModel):
    summary: str
    answer: str
    grounded: bool
    citations: list[Citation]
    decision_factors: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    retrieved_chunk_ids: list[str]
    precedents: list[PrecedentResult] = Field(default_factory=list)
    saved: bool = False
    warning: str | None = None
    judgment: dict[str, Any] | None = None


class SavedAnswerCreate(BaseModel):
    user_id: str | None = None
    question: str
    summary: str
    answer: str
    citations: list[dict[str, Any]] = Field(default_factory=list)


class SavedAnswerRecord(SavedAnswerCreate):
    id: str
    created_at: datetime


class FeedbackCreate(BaseModel):
    question: str | None = None
    question_hash: str | None = None
    rating: Literal["up", "down"]
    reason: str | None = None


class AuthResult(BaseModel):
    token: str
    user_id: str
    nickname: str
    profile_image: str | None = None
    is_new: bool = False


class UserRecord(BaseModel):
    id: str
    kakao_id: str
    nickname: str
    profile_image: str | None = None
    created_at: datetime


class IngestRunRequest(BaseModel):
    input_path: str
    mode: Literal["minimal", "full"] = "minimal"
    apply_schema: bool = False
    reindex: bool = False


class IngestJobRecord(BaseModel):
    id: str
    status: str
    source_type: str
    started_at: datetime
    finished_at: datetime | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    error_log: str | None = None


class AdminStats(BaseModel):
    documents: int
    chunks: int
    saved_answers: int
    feedback: int
    estimated_text_bytes: int
    estimated_vector_bytes: int
    relation_bytes: dict[str, int]
