from __future__ import annotations

import re
from typing import Iterable

from ..ai import GeminiService
from ..logging import get_logger
from ..normalization import compact_whitespace, normalize_for_search
from ..precedent import PrecedentService
from ..repository import Repository
from ..settings import get_settings
from ..types import ChatAnswer, Citation, ChatRequest, RetrievedChunk, SavedAnswerCreate
from ..retrieval import RetrievalService


LOGGER = get_logger(__name__)


class ChatService:
    def __init__(self) -> None:
        """ChatService 초기화. 설정, 검색(RetrievalService), LLM(GeminiService),
        DB(Repository), 판례(PrecedentService) 의존성을 생성한다."""
        self.settings = get_settings()
        self.retrieval = RetrievalService()
        self.gemini = GeminiService()
        self.repository = Repository()
        self.precedent_service = PrecedentService()

    def _search_precedents_safe(self, question: str) -> list:
        """벡터 + LLM 하이브리드 키워드로 판례를 검색한다.
        흐름: 캐시 확인 → LLM 키워드 확장 → 벡터 검색으로 조문 제목 추출 →
        키워드 병합 → 판례 API 검색 → LLM 관련도 랭킹 → 결과 캐싱 후 반환."""
        # 질문 단위 캐시 확인 — 동일 질문 반복 시 API 호출 절약
        cached = self.repository.get_precedent_cache(f"q:{question.strip().lower()}")
        if cached is not None:
            # 캐시 히트 — 직렬화된 딕셔너리를 PrecedentResult로 복원
            from ..types import PrecedentResult
            return [PrecedentResult.model_validate(r) for r in cached]

        try:
            # 1단계: LLM으로 질문을 확장하여 검색 키워드 생성
            llm_keywords = self.gemini.expand_query(question)

            # 2단계: 벡터 검색 결과에서 조문 제목을 키워드로 추출
            vector_chunks = self.retrieval.retrieve(question)
            vector_titles = list(dict.fromkeys(
                c.article_title for c in vector_chunks[:6] if c.article_title and len(c.article_title) >= 2
            ))

            # 3단계: 키워드 병합 — 법률명 제외한 LLM 키워드 우선, 벡터 제목 보충
            seen_kw: set[str] = set()
            search_keywords: list[str] = []
            for kw in llm_keywords:
                if kw not in seen_kw and not kw.endswith("법") and not kw.endswith("령") and not kw.endswith("판례"):
                    seen_kw.add(kw)
                    search_keywords.append(kw)
            for kw in vector_titles[:2]:
                if kw not in seen_kw:
                    seen_kw.add(kw)
                    search_keywords.append(kw)

            # 키워드가 하나도 없으면 질문 앞부분을 잘라 폴백 키워드로 사용
            if not search_keywords:
                search_keywords = [question.split("?")[0].strip()[:20]]

            # 최대 3개 키워드로 판례 API 검색 후 결과 병합 (중복 case_id 제거)
            all_results = []
            seen_case_ids: set[str] = set()

            for kw in search_keywords[:3]:
                try:
                    resp = self.precedent_service.search(kw, display=5)
                    results = resp.results
                except Exception:
                    results = []

                for r in results:
                    if r.case_id not in seen_case_ids:
                        seen_case_ids.add(r.case_id)
                        all_results.append(r)

            if not all_results:
                return []

            # case_name 기준 중복 제거 (같은 판례가 다른 키워드로 중복 검색될 수 있음)
            seen_names: set[str] = set()
            deduped = []
            for r in all_results:
                if r.case_name not in seen_names:
                    seen_names.add(r.case_name)
                    deduped.append(r)

            # 3건 이하면 랭킹 불필요, 그대로 반환
            if len(deduped) <= 3:
                return deduped

            # LLM으로 관련도 랭킹 — 상위 3건을 앞에, 나머지를 뒤에 배치
            precedent_dicts = [r.model_dump(mode="json") for r in deduped]
            ranked_indices = self.gemini.rank_precedents(question, precedent_dicts)
            top_indices = set(ranked_indices[:3])
            ranked = [deduped[i] for i in ranked_indices[:3]]
            rest = [d for i, d in enumerate(deduped) if i not in top_indices]
            final = ranked + rest

            # 질문 단위로 최종 결과 캐싱 (24시간 TTL)
            try:
                self.repository.set_precedent_cache(
                    f"q:{question.strip().lower()}",
                    [r.model_dump(mode="json") for r in final],
                    ttl_hours=24,
                )
            except Exception:
                pass

            return final
        except Exception as exc:
            LOGGER.debug("Precedent search failed: %s", exc)
            return []

    def answer(self, request: ChatRequest) -> ChatAnswer:
        """메인 답변 생성 오케스트레이션.
        흐름: 응답 캐시 확인 → 질문 유형 감지 → 판례 검색 → 법령 청크 검색 →
        Gemini 답변 생성 → 인용 검증 → 판단(judgment) 정규화 → 히스토리 저장."""
        # 응답 캐시 확인 — 동일 질문에 대한 이전 응답이 있으면 즉시 반환
        cached = self.repository.get_response_cache(request.question)
        if cached and not request.stream:
            LOGGER.info("Cache hit for question: %s", request.question[:30])
            return ChatAnswer.model_validate(cached)

        # 질문 유형 감지 (실무형 질문인지 판단)
        guidance_profile = self._detect_guidance_profile(request.question)
        # 관련 판례 검색 (실패해도 빈 리스트 반환)
        precedents = self._search_precedents_safe(request.question)
        # 벡터DB에서 관련 법령 청크 검색
        chunks = self.retrieval.retrieve(request.question)
        # 검색 결과 없으면 폴백 응답 반환
        if not chunks:
            summary, answer, decision_factors, action_items = self._fallback_response(guidance_profile)
            return ChatAnswer(
                summary=summary,
                answer=answer,
                grounded=False,
                citations=[],
                decision_factors=decision_factors,
                action_items=action_items,
                retrieved_chunk_ids=[],
                precedents=precedents,
                warning=self._build_warning(
                    grounded=False,
                    model_grounded=False,
                    guidance_profile=guidance_profile,
                ),
            )

        # Gemini에 질문 + 검색된 청크를 전달하여 근거 기반 답변 생성
        try:
            model_payload = self.gemini.generate_grounded_answer(question=request.question, chunks=chunks)
        except Exception as exc:
            LOGGER.warning("Gemini answer generation failed: %s", exc)
            # 429/quota 에러면 요청 한도 메시지, 그 외엔 일반 에러 메시지
            error_msg = str(exc)
            if "429" in error_msg or "quota" in error_msg.lower() or "rate" in error_msg.lower():
                warning = "AI 서비스 요청 한도에 일시적으로 도달했습니다. 잠시 후 다시 시도해 주세요."
            else:
                warning = "AI 응답 생성에 실패했습니다. 잠시 후 다시 시도해 주세요."
            return ChatAnswer(
                summary="일시적으로 답변을 생성할 수 없습니다.",
                answer="AI 서비스에 일시적인 문제가 발생했습니다. 30초 후 다시 질문해 주세요.",
                grounded=False,
                citations=[],
                decision_factors=[],
                action_items=[],
                retrieved_chunk_ids=[chunk.id for chunk in chunks],
                precedents=precedents or [],
                saved=False,
                warning=warning,
            )
        # 구조화된 응답이 아닌 문자열이면 파싱 실패로 간주
        if isinstance(model_payload, str):
            raise RuntimeError("Gemini structured output parsing failed.")

        # Gemini가 반환한 인용(citation)을 실제 검색된 청크와 대조 검증
        chunk_map = {chunk.id: chunk for chunk in chunks}
        citations = self._validate_citations(model_payload.get("citations", []), chunk_map.values())
        # Gemini가 grounded라고 했지만 인용 검증 실패 시, 검색된 청크를 인용으로 대체
        model_grounded = bool(model_payload.get("grounded"))
        if model_grounded and not citations and chunks:
            citations = self._citations_from_chunks(chunks)
        # 인용이 있으면 근거 기반(grounded) 답변으로 판정
        grounded = bool(citations)
        # Gemini 응답 필드들을 정규화 (공백 압축, 불릿 정리)
        answer = compact_whitespace(model_payload.get("answer", ""))
        summary = compact_whitespace(model_payload.get("summary", ""))
        decision_factors = self._normalize_bullets(model_payload.get("decision_factors", []))
        action_items = self._normalize_bullets(model_payload.get("action_items", []))
        # 빈 필드가 있을 때 채울 폴백 응답 준비
        fallback_summary, fallback_answer, fallback_decision_factors, fallback_action_items = (
            self._fallback_response(guidance_profile)
        )

        # Gemini 응답이 비어있는 필드를 폴백 값으로 대체
        if not summary:
            summary = fallback_summary
        if not decision_factors:
            decision_factors = fallback_decision_factors
        if not action_items:
            action_items = fallback_action_items

        # 근거 없으면 폴백 답변 사용, 모델이 비근거라 판단하면 답변 보정
        if not grounded:
            answer = fallback_answer
        elif not bool(model_payload.get("grounded")):
            answer = compact_whitespace(answer) or fallback_answer

        # judgment 필드 정규화 — 판단형 질문(예: "~할 수 있나요?")에 대한 결론 구조화
        raw_judgment = model_payload.get("judgment") if isinstance(model_payload, dict) else None
        normalized_judgment = None
        if isinstance(raw_judgment, dict) and raw_judgment.get("is_judgment_question"):
            # verdict 값이 허용 목록에 없으면 "depends"로 안전하게 폴백
            verdict = raw_judgment.get("verdict")
            if verdict not in ("likely_yes", "likely_no", "depends", "needs_more_info", "not_applicable"):
                verdict = "depends"
            normalized_judgment = {
                "is_judgment_question": True,
                "verdict": verdict,
                "short_answer": raw_judgment.get("short_answer") or "",
                "reasoning": raw_judgment.get("reasoning") or "",
                "missing_facts": raw_judgment.get("missing_facts") or [],
                "typical_path": raw_judgment.get("typical_path") or "",
            }

        # 최종 ChatAnswer 객체 조립
        chat_answer = ChatAnswer(
            summary=summary or fallback_summary,
            answer=answer,
            grounded=grounded,
            citations=citations,
            decision_factors=decision_factors,
            action_items=action_items,
            retrieved_chunk_ids=[chunk.id for chunk in chunks],
            precedents=precedents,
            saved=False,
            warning=self._build_warning(
                grounded=grounded,
                model_grounded=bool(model_payload.get("grounded")),
                guidance_profile=guidance_profile,
            ),
            judgment=normalized_judgment,
        )

        # 사용자가 저장 요청 + 서버 히스토리 활성화 시 DB에 답변 저장
        if request.save and self.settings.enable_server_chat_history:
            chat_answer.saved = self._save_answer(request, chat_answer)

        # Note: response caching disabled to ensure consistent precedent results
        # Precedent search depends on LLM keyword extraction which varies slightly

        # 히스토리 저장은 API 레이어(main.py)에서 통합 관리 — 여기서는 저장하지 않음

        return chat_answer

    def _validate_citations(
        self,
        raw_citations: list[dict[str, str]],
        chunks: Iterable[RetrievedChunk],
    ) -> list[Citation]:
        """Gemini가 반환한 인용 목록을 실제 검색 청크와 대조하여 검증한다.
        존재하지 않는 chunk_id는 무시하고, 유효한 것만 Citation 객체로 변환."""
        chunk_map = {chunk.id: chunk for chunk in chunks}
        citations: list[Citation] = []
        for item in raw_citations:
            chunk_id = item.get("chunk_id")
            if not chunk_id or chunk_id not in chunk_map:
                continue
            chunk = chunk_map[chunk_id]
            citations.append(
                Citation(
                    chunk_id=chunk.id,
                    law_id=chunk.law_id,
                    law_title=chunk.law_title,
                    article_no=chunk.article_no,
                    article_title=chunk.article_title,
                    chunk_type=chunk.chunk_type,
                    quote=chunk.text,
                    score=chunk.score,
                )
            )
        return citations

    def _save_answer(self, request: ChatRequest, answer: ChatAnswer) -> bool:
        """답변을 DB에 영구 저장한다. 사용자의 저장 요청 시 호출."""
        self.repository.create_saved_answer(
            SavedAnswerCreate(
                user_id=request.user_id,
                question=request.question,
                summary=answer.summary,
                answer=answer.answer,
                citations=[citation.model_dump(mode="json") for citation in answer.citations],
            )
        )
        return True

    @staticmethod
    def _normalize_bullets(raw_items: object) -> list[str]:
        """불릿 리스트를 정규화한다. 공백 압축, 중복 제거, 최대 5개로 제한."""
        if not isinstance(raw_items, list):
            return []
        items: list[str] = []
        for item in raw_items:
            if not isinstance(item, str):
                continue
            normalized = compact_whitespace(item)
            if normalized and normalized not in items:
                items.append(normalized)
            if len(items) >= 5:
                break
        return items

    @staticmethod
    def _citations_from_chunks(chunks: Iterable[RetrievedChunk]) -> list[Citation]:
        """검색된 청크를 직접 Citation으로 변환한다. Gemini 인용 검증 실패 시 폴백용.
        상위 3개 청크만 사용."""
        citations: list[Citation] = []
        for chunk in list(chunks)[:3]:
            citations.append(
                Citation(
                    chunk_id=chunk.id,
                    law_id=chunk.law_id,
                    law_title=chunk.law_title,
                    article_no=chunk.article_no,
                    article_title=chunk.article_title,
                    chunk_type=chunk.chunk_type,
                    quote=chunk.text,
                    score=chunk.score,
                )
            )
        return citations

    @staticmethod
    def _plain_text(text: str) -> str:
        """HTML/마크다운 볼드(**) 및 연속 공백을 제거하여 순수 텍스트로 변환."""
        cleaned = re.sub(r"\*\*", "", text)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    @staticmethod
    def _detect_guidance_profile(question: str) -> str | None:
        """실무적 대응을 묻는 질문인지 판단 (도메인 무관)."""
        normalized = normalize_for_search(question)
        practical_question = any(
            term in normalized
            for term in ("어떻게", "어떻게돼", "어떻게되", "해야", "해야해", "해야하", "대처", "조치", "책임", "절차", "방법")
        )
        return "practical" if practical_question else None

    @staticmethod
    def _fallback_response(guidance_profile: str | None) -> tuple[str, str, list[str], list[str]]:
        """LLM 실패 시 범용 fallback 응답."""
        if guidance_profile == "practical":
            return (
                "결론을 내리기 전에 핵심 사실관계 정리가 필요합니다.",
                (
                    "현재 검색된 법령만으로는 결론을 단정하기 어렵습니다. 아래 판단 포인트와 "
                    "지금 할 일을 먼저 정리한 뒤 다시 질문하면 더 정확한 답변을 드릴 수 있습니다."
                ),
                [
                    "날짜, 상대방, 진행 순서 등 결론을 바꾸는 사실관계를 먼저 정리하세요.",
                    "관련 증빙(통지, 합의, 계약서, 사진, 영상 등)이 있는지 확인하세요.",
                    "질문을 한 번에 하나의 쟁점으로 나눠 보세요.",
                ],
                [
                    "상황을 시간순으로 짧게 정리하세요.",
                    "관련 자료를 보관하세요.",
                    "쟁점을 1~2개로 나눠 다시 질문하세요.",
                ],
            )
        return (
            "검색된 법령 근거가 아직 충분하지 않습니다.",
            "현재 적재된 법령 범위 안에서는 질문과 직접 연결되는 근거를 찾지 못했습니다.",
            [],
            [],
        )

    @staticmethod
    def _build_warning(
        *,
        grounded: bool,
        model_grounded: bool,
        guidance_profile: str | None,
    ) -> str | None:
        """답변 신뢰도에 따른 경고 메시지를 생성한다.
        근거 없음 → 일반 안내/실무 안내, 모델 비근거 → 추가 확인 필요, 정상 → None."""
        if not grounded:
            if guidance_profile:
                return "직접 연결되는 조문이 부족해도, 우선 확인할 사실관계와 다음 행동을 함께 보여줍니다."
            return "직접 연결되는 조문을 찾지 못했습니다. 일반적인 법률 안내를 제공합니다."
        if not model_grounded:
            return "관련 조문은 찾았지만, 과실 비율이나 최종 책임은 추가 사실관계 확인이 필요합니다."
        return None
