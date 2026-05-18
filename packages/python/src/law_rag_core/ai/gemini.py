from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any

from google import genai
from google.genai import types

from pathlib import Path

from ..logging import get_logger
from ..settings import get_settings
from ..types import RetrievedChunk

PROMPTS_DIR = Path(__file__).parent / "prompts"


_SHARED_PRINCIPLES: str | None = None


def _load_shared_principles() -> str:
    global _SHARED_PRINCIPLES
    if _SHARED_PRINCIPLES is None:
        _SHARED_PRINCIPLES = (PROMPTS_DIR / "_shared_principles.txt").read_text(encoding="utf-8")
    return _SHARED_PRINCIPLES


def _load_prompt(name: str) -> str:
    """prompts/ 디렉토리에서 프롬프트 텍스트 로드. {shared_principles} 자동 치환."""
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    if "{shared_principles}" in text:
        text = text.replace("{shared_principles}", _load_shared_principles())
    return text


LOGGER = get_logger(__name__)

import re as _re

_STATUTE_PATTERN = _re.compile(
    r"(?:[가-힣]{2,15}(?:법|법률)\s*제\d+조(?:의\d+)?(?:\s*제\d+항)?)"
)


def _extract_cited_statutes(text: str) -> list[str]:
    """사용자 질문/문서에서 한국 법령 인용 패턴 추출."""
    if not text:
        return []
    return list(dict.fromkeys(_STATUTE_PATTERN.findall(text)))


# 참고 법령(evidence) 섹션 예산은 원문 길이에 비례해 동적으로 결정.
# 원문과 균형을 맞춰 LLM이 한쪽(원문/법령)에 쏠리지 않도록 함.
CHAIN_PARALLEL_WORKERS = 5

EVIDENCE_BUDGET_RATIO = 1.0   # 원문 대비 배수
EVIDENCE_BUDGET_MIN = 8000    # 원문이 짧아도 제도 맥락 확보용 최소치
EVIDENCE_BUDGET_MAX = 25000   # 프롬프트 폭주 방지용 상한


_PRECEDENT_STOPWORDS = {
    "검토", "요청", "사항", "쟁점", "핵심", "배경", "프로세스", "개요", "결과",
    "판단", "근거", "일반", "주요", "관련", "해당", "여부", "가능", "가능성",
    "당사", "구상", "중인", "경우", "문제", "활용", "확인", "필요", "방안",
    "가능한지", "있는지", "없는지", "할수있는지", "제공", "적용",
}
# 범용 품사·결정유형 마커 — 도메인 무관 (어느 법률 분야 판례 사건명에나 공통적으로 등장)
_PRECEDENT_LEGAL_MARKERS = {
    # 결정유형/처분
    "위반", "처벌", "벌칙", "금지", "무효", "취소", "해지", "해제",
    "처분", "환수", "징수", "부과", "몰수", "추징", "기각", "인용",
    # 청구·구제
    "청구", "손해배상", "부당이득", "구제", "확인", "이행",
    # 책임·의무 일반
    "책임", "과실", "의무", "위약", "채무불이행", "침해", "불법행위",
}


def _compute_evidence_budget(document_chars: int) -> int:
    """원문 길이에 비례한 evidence 예산. 상·하한으로 보호."""
    target = int(document_chars * EVIDENCE_BUDGET_RATIO)
    return max(EVIDENCE_BUDGET_MIN, min(target, EVIDENCE_BUDGET_MAX))


def _normalize_precedent_candidate(candidate: str) -> str:
    candidate = _re.sub(r"[·ㆍ/|,;:()\[\]{}<>\"'“”‘’]+", " ", str(candidate))
    return " ".join(candidate.split()).strip()


def _strip_korean_particle(token: str) -> str:
    for suffix in ("으로부터", "으로서", "으로써", "에게서", "에서", "에게", "까지", "부터", "인지", "인가", "으로", "라고", "이라는", "있는지", "없는지", "은", "는", "이", "가", "을", "를", "과", "와", "의", "도", "만"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def _extract_law_names(text: str) -> list[str]:
    laws = []
    tokens = _re.findall(r"[가-힣A-Za-z0-9·ㆍ]{2,20}", text)

    def add(law: str) -> None:
        law = law.replace("ㆍ", "").replace("·", "").replace(" ", "")
        if law and law not in laws:
            laws.append(law)

    for i, token in enumerate(tokens):
        if not token.endswith(("법", "법률")):
            continue
        prev = tokens[i - 1] if i > 0 else ""
        if prev and token in {"보호법", "관리법", "규제법"} and prev not in _PRECEDENT_STOPWORDS:
            add(prev + token)
            continue
        add(token)
    return laws


def _extract_precedent_terms(text: str) -> list[str]:
    """질문 원문에서 판례 DB가 잘 받는 짧은 법률 쟁점어를 범용적으로 추출한다."""
    terms: list[str] = []

    def add(term: str) -> None:
        term = _normalize_precedent_candidate(term)
        compact = term.replace(" ", "")
        if not term or len(term) > 30:
            return
        if len(compact) < 2 or term in _PRECEDENT_STOPWORDS:
            return
        if term not in terms:
            terms.append(term)

    normalized = text.replace(" ", "")
    has_violation_context = any(marker in normalized for marker in ("위반", "처벌", "벌칙", "금지"))

    tokens = []
    for raw in _re.findall(r"[가-힣A-Za-z0-9]{2,20}", text):
        token = _strip_korean_particle(raw)
        if token and token not in _PRECEDENT_STOPWORDS:
            tokens.append(token)

    # 범용 명사구 추출 — "A B" 형태에서 B 가 법률 마커(위반·취소·청구…)이면 "A B" 를 쟁점 후보로
    for i, token in enumerate(tokens):
        compact = token.replace(" ", "")
        if compact.endswith(("법", "법률", "령", "규칙")):
            continue
        if i + 1 < len(tokens):
            nxt = tokens[i + 1]
            nxt_compact = nxt.replace(" ", "")
            if (
                _re.search(r"[가-힣]", compact)
                and _re.search(r"[가-힣]", nxt_compact)
                and nxt_compact in _PRECEDENT_LEGAL_MARKERS
                and compact not in _PRECEDENT_LEGAL_MARKERS
            ):
                add(f"{token} {nxt}")

    if has_violation_context:
        for law in _extract_law_names(text):
            add(f"{law}위반")

    for token in tokens:
        if len(terms) >= 12:
            break
        compact = token.replace(" ", "")
        if compact.endswith(("법", "법률", "령", "규칙")):
            continue
        if len(compact) >= 4 and _re.search(r"[가-힣]", compact):
            add(token)

    return terms


_CASE_NO_PATTERN = _re.compile(r"\d{4}\s?[가-힣]{1,3}\s?\d+")


def _extract_case_numbers(text: str) -> list[str]:
    """질문·문서 본문에서 한국 판례 사건번호 패턴을 추출 (예: '2020두38171')."""
    if not text:
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for m in _CASE_NO_PATTERN.finditer(text):
        s = _re.sub(r"\s+", "", m.group(0))
        if s not in seen:
            seen.add(s)
            hits.append(s)
    return hits


def _spacing_variants(term: str) -> list[str]:
    """law.go.kr DRF의 공백 민감도 대응 — 한 키워드로 3개 변형 생성.
    - 원본
    - 공백 제거 버전
    - 마지막 어절만
    """
    term = _normalize_precedent_candidate(term)
    if not term:
        return []
    out: list[str] = [term]
    compact = term.replace(" ", "")
    if compact and compact not in out:
        out.append(compact)
    parts = term.split()
    if len(parts) > 1:
        last = parts[-1]
        if len(last) >= 2 and last not in out:
            out.append(last)
    return out


def _build_precedent_query(question: str, gemini_service: Any = None) -> list[str]:
    """판례 검색용 쿼리를 생성 (범용 — 도메인 힌트 없음).

    설계 원칙:
    1. 사건번호 패턴은 그대로 보존 (예: '2020두38171' → DRF 에서 직접 조회)
    2. LLM analyze_intent 키워드 + 질문 명사 추출을 소스로
    3. 각 후보에 공백 변형 자동 생성 (law.go.kr 공백 민감도)
    4. 3어절 이상 자연어 구는 제거 (DRF AND 검색 특성상 0건)
    """
    candidates: list[str] = []
    if not question or not question.strip():
        return candidates

    def _add(candidate: str) -> None:
        candidate = _normalize_precedent_candidate(candidate)
        if not candidate or len(candidate) > 20:
            return
        # 3어절 이상은 DRF 에서 거의 0건 (실측 검증됨)
        if len(candidate.split()) > 2:
            return
        if candidate not in candidates:
            candidates.append(candidate)

    # 1) 사건번호 직접 쿼리 (가장 정확한 히트 경로)
    for case_no in _extract_case_numbers(question):
        if case_no not in candidates:
            candidates.append(case_no)

    # 2) LLM analyze_intent 키워드 → 공백 변형 확장
    if gemini_service is not None:
        try:
            intent = gemini_service.analyze_intent(question)
            intent_keywords = [
                str(k).strip() for k in ((intent or {}).get("keywords") or [])
                if str(k).strip()
            ]
            for kw in intent_keywords[:6]:
                terms = _extract_precedent_terms(kw) or [kw]
                for t in terms:
                    for v in _spacing_variants(t):
                        _add(v)
        except Exception as exc:
            LOGGER.warning("analyze_intent for precedent query failed: %s", exc)

    # 3) 질문 원문에서 명사 추출 → 공백 변형
    for term in _extract_precedent_terms(question):
        for v in _spacing_variants(term):
            if len(candidates) >= 16:
                break
            _add(v)
        if len(candidates) >= 16:
            break

    return candidates


def _search_precedents_as_chunks(
    question: str,
    limit: int = 5,
    *,
    rerank_with: Any = None,
) -> list[Any]:
    """판례 검색 결과를 RetrievedChunk 유사 객체로 변환하여 evidence에 포함.

    - 판시사항·판결요지·참조조문 전문을 절단 없이 프롬프트에 주입
    - 헤더: "=== 판례: [법원] YYYY. M. D. 선고 사건번호 판결 — 사건명 ==="
    - rerank_with 가 주어지면 rank_precedents() 로 관련도 재정렬
    - score 는 재정렬 후 순서 기반 감소
    - v3: 판례 쿼리는 analyze_intent keywords 로 압축하여 law.go.kr 413 에러 회피

    실패 시 빈 리스트 (외부 API 의존이므로 robust).
    """
    try:
        from ..precedent import PrecedentService
    except Exception:
        return []
    if not question or not question.strip():
        return []

    class _PrecChunk:
        def __init__(self, p: Any, score: float, detail_sections: list[Any] | None = None):
            case_no = getattr(p, 'case_number', '') or ''
            case_name = getattr(p, 'case_name', '') or ''
            court = getattr(p, 'court_name', None) or ''
            date = getattr(p, 'judgment_date', None) or ''
            self.id = f"prec:{getattr(p, 'case_id', '')}"
            self.law_title = getattr(p, 'source_label', '판례') or '판례'
            self.article_no = case_no
            self.article_title = case_name

            # 헤더 — 변호사 인용 형식
            header_parts = []
            if court:
                header_parts.append(court)
            if date:
                header_parts.append(f"{date} 선고")
            if case_no:
                header_parts.append(f"{case_no} 판결")
            header = " ".join(header_parts) if header_parts else "판례"
            if case_name:
                header = f"{header} — {case_name}"

            body_sections: list[str] = [f"=== 판례: {header} ==="]
            # 목록 API는 summary가 비어 있는 경우가 많으므로 상세 API의 판시사항·판결요지를 우선 사용한다.
            for section in detail_sections or []:
                label = getattr(section, "label", "") or ""
                content = _clean_precedent_text(getattr(section, "content", "") or "")
                if not content:
                    continue
                if label in ("판례내용", "전문") and len(content) > 6000:
                    content = content[:6000]
                body_sections.append(f"[{label}]\n{content}")
            summary = _clean_precedent_text(getattr(p, 'summary', None) or getattr(p, 'decision', None) or "")
            if summary and all(summary not in part for part in body_sections):
                body_sections.append(f"[요약]\n{summary}")
            self.text = "\n".join(body_sections)
            self.has_body = len(body_sections) > 1
            self.score = score

    # v4: 병렬 fan-out — 공백변형 포함 후보들을 search=1(제목) + search=2(본문) 둘 다 시도
    #     display 를 50 으로 올려 landmark 판례가 뒤쪽에 파묻히는 문제 해결
    queries = _build_precedent_query(question, gemini_service=rerank_with)
    if not queries:
        queries = [question.strip()[:60]]

    svc = PrecedentService()
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _one(q: str, body: bool) -> tuple[list[Any], int]:
        try:
            resp = svc.search(q, display=100, body_search=body)
            cases = list(getattr(resp, 'results', [])) or []
            total = int(getattr(resp, 'total_count', 0) or 0)
            return cases, total
        except Exception as exc:
            LOGGER.warning("Precedent search failed (query=%r body=%s): %s", q[:40], body, exc)
            return [], 0

    # 판례별 누적 점수 (범용 — 도메인 힌트 0)
    # score = 쿼리 특이도(1/total)×3 + body_search 보너스 + 대법원 가중 + 쿼리 중복 합의
    scores: dict[str, float] = {}
    meta: dict[str, Any] = {}  # cid → 판례 객체

    tasks = [(q, body) for q in queries if q for body in (False, True)]

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_one, q, body): (q, body) for (q, body) in tasks}
        for fut in as_completed(futs):
            q, body = futs[fut]
            cases, total = fut.result()
            if not cases:
                continue
            # 범용 신호 1: 쿼리 특이도 (좁을수록 ↑)
            specificity = 3.0 / max(total, 1)
            # 범용 신호 2: body_search=True 가중
            body_bonus = 2.0 if body else 0.0
            for p in cases:
                cid = getattr(p, 'case_id', '')
                # 사건번호 기반 정규화 key — 서로 다른 source 에서 같은 사건 중복 방지
                case_num = _re.sub(r"\s+", "", getattr(p, 'case_number', '') or '')
                dedup_key = case_num or cid
                if not dedup_key:
                    continue
                court = getattr(p, 'court_name', '') or ''
                # 범용 신호 3: 심급 — 대법원 > 고법 > 지법 > 행정심판·권익위·법제처
                if '대법원' in court:
                    court_bonus = 3.0
                elif '헌법재판소' in court:
                    court_bonus = 2.5
                elif '고등법원' in court:
                    court_bonus = 1.5
                elif '지방법원' in court or '행정법원' in court or '가정법원' in court:
                    court_bonus = 0.8
                else:
                    # 권익위·행심·법제처 등 — 판례는 아니지만 실무 참고로 최소 가중
                    court_bonus = 0.0

                # 범용 신호 5: 연도 — 최근 판례 가중 (2010년 이후 연 0.05)
                date_str = getattr(p, 'judgment_date', '') or ''
                year_bonus = 0.0
                if len(date_str) >= 4 and date_str[:4].isdigit():
                    y = int(date_str[:4])
                    year_bonus = max(0.0, (y - 2010) * 0.05)

                # 범용 신호 6: 사건명에 쿼리 토큰이 통째로 포함 → 정확 매칭 보너스
                case_name_norm = (getattr(p, 'case_name', '') or '').replace(' ', '')
                q_norm = q.replace(' ', '')
                name_match_bonus = 0.8 if (q_norm and q_norm in case_name_norm) else 0.0

                delta = specificity + body_bonus + court_bonus + year_bonus + name_match_bonus
                # 범용 신호 4: 중복 쿼리 합의 — 이미 있으면 누적
                if dedup_key in scores:
                    scores[dedup_key] += delta + 1.5
                    # 대법원 판례를 우선 유지 (같은 사건 여러 source 나올 때)
                    if '대법원' in court and '대법원' not in (getattr(meta[dedup_key], 'court_name', '') or ''):
                        meta[dedup_key] = p
                else:
                    scores[dedup_key] = delta
                    meta[dedup_key] = p

    if not scores:
        return []

    # 점수 내림차순 → rerank 입력용 pre-sort
    ordered_keys = sorted(scores, key=lambda c: scores[c], reverse=True)
    FAN_CAP = max(limit * 30, 200)
    results = [meta[c] for c in ordered_keys[:FAN_CAP]]

    LOGGER.info(
        "Precedent fan-out: %d unique cases (pre-sorted by specificity), queries=%d",
        len(results), len(queries),
    )

    # v4: 두 단계 재정렬
    #  1) 먼저 제목·사건명만으로 rerank (수백 건 대상)
    #  2) top-K (limit * 3) 만 상세 API 호출해 판시사항 보강
    #  3) 상세 포함 후 최종 limit 개로 자름
    if rerank_with is not None and results:
        # rerank 입력 상한 — 수백 건 그대로 넣으면 LLM 컨텍스트 과부하로 빈 결과.
        # case_name 만으로 1차 필터링하기에 충분한 수 (60건) 로 자름.
        RERANK_INPUT_CAP = 60
        head = results[:RERANK_INPUT_CAP]
        tail = results[RERANK_INPUT_CAP:]
        try:
            dicts = []
            for p in head:
                dicts.append({
                    "case_id": getattr(p, 'case_id', ''),
                    "case_name": getattr(p, 'case_name', ''),
                    "court_name": getattr(p, 'court_name', ''),
                    "case_number": getattr(p, 'case_number', ''),
                    "judgment_date": getattr(p, 'judgment_date', ''),
                    "summary": getattr(p, 'summary', '') or getattr(p, 'decision', ''),
                })
            indices = rerank_with.rank_precedents(question, dicts, top_k=limit * 3)
            if indices:
                ranked_head = [head[i] for i in indices if 0 <= i < len(head)]
                # rank 에 안 든 head 나머지 + tail 을 뒤에 붙여 recall 유지
                ranked_set = {id(p) for p in ranked_head}
                remainder = [p for p in head if id(p) not in ranked_set] + tail
                results = ranked_head + remainder
        except Exception as exc:
            LOGGER.warning("rank_precedents failed, keeping original order: %s", exc)

    # 상위 후보에만 상세 API 호출 (판시사항·판결요지 보강)
    detail_sections_by_id: dict[str, list[Any]] = {}
    TOP_FOR_DETAIL = max(limit * 4, 20)
    top_for_detail = results[:TOP_FOR_DETAIL]
    for p in top_for_detail:
        cid = getattr(p, 'case_id', '')
        source_id = getattr(p, 'source_id', 'prec') or 'prec'
        if not cid:
            continue
        try:
            detail = svc.get_detail(source_id, cid)
            sections = list(getattr(detail, "sections", []) or [])
            detail_sections_by_id[cid] = sections
            # summary 가 없으면 상세 응답·판시사항에서 채움 (2차 rerank 품질 ↑)
            detailed_result = getattr(detail, "result", None)
            if detailed_result is not None and getattr(detailed_result, "summary", None) and not getattr(p, "summary", None):
                p.summary = detailed_result.summary
            if not getattr(p, "summary", None):
                for sec in sections:
                    label = getattr(sec, "label", "") or ""
                    content = (getattr(sec, "content", "") or "").strip()
                    if content and label in ("판시사항", "판결요지", "요약", "판례내용"):
                        p.summary = content[:600]
                        break
        except Exception as exc:
            LOGGER.warning("Precedent detail fetch failed (source=%s id=%s): %s", source_id, cid, exc)

    # v5: 2차 rerank — 판시사항·판결요지 기반으로 진짜 관련성 재평가
    if rerank_with is not None and top_for_detail:
        try:
            dicts2 = [{
                "case_id": getattr(p, 'case_id', ''),
                "case_name": getattr(p, 'case_name', ''),
                "court_name": getattr(p, 'court_name', ''),
                "case_number": getattr(p, 'case_number', ''),
                "judgment_date": getattr(p, 'judgment_date', ''),
                "summary": getattr(p, 'summary', '') or getattr(p, 'decision', ''),
            } for p in top_for_detail]
            indices2 = rerank_with.rank_precedents(question, dicts2, top_k=limit)
            if indices2:
                ranked2 = [top_for_detail[i] for i in indices2 if 0 <= i < len(top_for_detail)]
                # 2차 랭킹 결과 + 안 뽑힌 나머지 (recall guard)
                ranked2_set = {id(p) for p in ranked2}
                rest = [p for p in top_for_detail if id(p) not in ranked2_set] + results[TOP_FOR_DETAIL:]
                results = ranked2 + rest
        except Exception as exc:
            LOGGER.warning("2nd rerank (by holding) failed: %s", exc)

    # 최종 청크: 상세 있는 건 우선, 모자라면 상세 없는 것도 포함 (제목/summary 만이라도)
    chunks: list[Any] = []
    for i, p in enumerate(results):
        if len(chunks) >= limit:
            break
        cid = getattr(p, 'case_id', '')
        chunk = _PrecChunk(p, score=1.0 - i * 0.05, detail_sections=detail_sections_by_id.get(cid))
        chunks.append(chunk)

    # Recall guard: top-limit 안에 대법원·헌재 판례 0 건이면 경고 (데이터 커버리지 부족 신호)
    high_court_hits = sum(
        1 for c in chunks
        if '대법원' in (getattr(c, '_court_cached', '') or '')
        or '헌법재판소' in (getattr(c, '_court_cached', '') or '')
    )
    # _court_cached 가 없으므로 results 기반 다시 집계
    high_court_hits = sum(
        1 for p in results[:limit]
        if '대법원' in (getattr(p, 'court_name', '') or '')
        or '헌법재판소' in (getattr(p, 'court_name', '') or '')
    )
    if high_court_hits == 0 and chunks:
        LOGGER.warning(
            "Precedent recall low: 상위 %d 건에 대법원/헌재 판례 0건 (쿼리 빌더 약함 가능)",
            limit,
        )
    return chunks


def _clean_precedent_text(text: str) -> str:
    """DRF 판례 HTML 조각을 evidence 친화적인 일반 텍스트로 정리."""
    if not text:
        return ""
    text = _re.sub(r"<br\s*/?>", "\n", str(text), flags=_re.IGNORECASE)
    text = _re.sub(r"<[^>]+>", "", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    lines = [" ".join(line.split()).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _retrieve_chunks_for_queries(
    queries: list[str],
    *,
    existing_ids: set[str] | None = None,
    per_query_limit: int = 8,
) -> list[Any]:
    """쿼리 리스트로 벡터+lexical 검색해 중복 제거한 청크 목록 반환.

    축별 쿼리 모음을 받아 각각 검색 → 청크 id 기준 dedup.
    LLM 호출은 하지 않음(analyze_intent/expand_query 경로 우회).
    """
    from ..retrieval.service import RetrievalService

    if not queries:
        return []

    retrieval = RetrievalService()
    seen: set[str] = set(existing_ids or set())
    collected: list[Any] = []

    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        try:
            vec = retrieval._vector_match(q)
        except Exception as exc:
            LOGGER.warning("Vector match failed for %r: %s", q, exc)
            vec = []
        try:
            lex = retrieval._lexical_match(q)
        except Exception as exc:
            LOGGER.warning("Lexical match failed for %r: %s", q, exc)
            lex = []
        merged = sorted(vec + lex, key=lambda c: c.score, reverse=True)[:per_query_limit]
        for c in merged:
            if c.id in seen:
                continue
            seen.add(c.id)
            collected.append(c)
    return collected


def _build_evidence(
    chunks_by_axis: dict[str, list[Any]],
    *,
    total_budget: int,
) -> str:
    """축별 청크를 예산 안에서 균등 분배해 evidence 텍스트 생성.

    - 축 개수로 예산 1차 분배 (축이 1개면 독점)
    - 각 축 안에서는 점수 높은 청크부터 원본 길이만큼 채움
    - MIN/MAX 상수 없음 — 원본 그대로, 남은 예산 소진 시 종료
    """
    axes = [a for a in chunks_by_axis if chunks_by_axis.get(a)]
    if not axes:
        return "(없음)"
    per_axis = max(total_budget // len(axes), 500)

    blocks: list[str] = []
    for axis in axes:
        remaining = per_axis
        lines: list[str] = []
        for c in sorted(chunks_by_axis[axis], key=lambda x: x.score, reverse=True):
            if remaining <= 0:
                break
            # v3: 고시번호·발령기관이 있으면 헤더에 노출 (변호사급 인용 지원)
            doc_type = getattr(c, "document_type", None)
            issuer = getattr(c, "issuer", None)
            promul = getattr(c, "promulgation_no", None)
            parts = [c.law_title]
            if doc_type == "administrative_rule" and promul:
                parts.append(f"(고시/훈령 제{promul}호)" if not str(promul).startswith("제") else f"({promul})")
            elif doc_type == "voluntary_code" and issuer:
                parts.append(f"({issuer})")
            if c.article_no:
                parts.append(c.article_no)
            if c.article_title:
                parts.append(c.article_title)
            header = f"[{' '.join(parts)}]"
            body = c.text or ""
            take = min(len(body), remaining)
            if take <= 0:
                break
            lines.append(f"{header}\n{body[:take]}")
            remaining -= take + len(header)
        if lines:
            title = f"### 관련 법령 — {axis}" if len(axes) > 1 else "### 관련 법령"
            blocks.append(title + "\n" + "\n\n".join(lines))
    return "\n\n".join(blocks) if blocks else "(없음)"


def _normalize_observation(obs: dict) -> dict:
    """관찰 항목(observations[])의 issues 배열/severity를 정규화.

    하위호환: 구 clause_review 필드(clause_no, clause_title, issue, suggestion, risk_level)도
    새 필드(locator, locator_title, severity)로 매핑.
    - 구 단일 issue/suggestion → issues 배열로 변환
    - issues 내 severity가 없으면 obs 레벨에서 상속
    - 전체 issues 중 최고 severity로 obs.severity 동기화
    """
    # 구 필드 → 새 필드 매핑
    if "locator" not in obs:
        clause_no = obs.get("clause_no")
        if clause_no is not None:
            obs["locator"] = clause_no if "조" in str(clause_no) else f"제{clause_no}조"
    if "locator_title" not in obs and obs.get("clause_title") is not None:
        obs["locator_title"] = obs["clause_title"]
    if "severity" not in obs and obs.get("risk_level") is not None:
        obs["severity"] = obs["risk_level"]

    issues = obs.get("issues")
    if not isinstance(issues, list):
        issues = []

    # 구 단일 issue/suggestion을 배열로 흡수
    legacy_issue = obs.get("issue") or obs.get("concern")
    legacy_suggestion = obs.get("suggestion")
    legacy_basis = obs.get("legal_basis") or obs.get("basis")
    if (legacy_issue or legacy_suggestion) and not issues:
        issues = [{
            "severity": obs.get("severity") if obs.get("severity") in ("high", "medium", "low") else "low",
            "concern": legacy_issue,
            "suggestion": legacy_suggestion,
            "basis": legacy_basis,
        }]

    cleaned: list[dict] = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        # 구 키 → 새 키
        if "concern" not in it and it.get("issue") is not None:
            it["concern"] = it["issue"]
        if "basis" not in it and it.get("legal_basis") is not None:
            it["basis"] = it["legal_basis"]
        if not (it.get("concern") or it.get("suggestion")):
            continue
        sev = it.get("severity")
        if sev not in ("high", "medium", "low"):
            cur = obs.get("severity")
            it["severity"] = cur if cur in ("high", "medium", "low") else "low"
        cleaned.append(it)
    obs["issues"] = cleaned

    # severity 동기화
    if cleaned:
        order = {"high": 3, "medium": 2, "low": 1}
        top_sev = max(cleaned, key=lambda x: order.get(x.get("severity", "low"), 1)).get("severity", "low")
        cur = obs.get("severity")
        if cur not in ("high", "medium", "low", "ok") or order.get(cur, 0) < order.get(top_sev, 0):
            obs["severity"] = top_sev
    elif obs.get("severity") not in ("high", "medium", "low", "ok"):
        obs["severity"] = "ok"

    # 구 필드 보존(프론트 하위호환): issues에 아직 issue/suggestion 키가 없으면 첨가
    for it in obs["issues"]:
        if "issue" not in it and it.get("concern") is not None:
            it["issue"] = it["concern"]
        if "legal_basis" not in it and it.get("basis") is not None:
            it["legal_basis"] = it["basis"]

    return obs


def _merge_risk_scenarios(base: dict, risk: dict) -> dict:
    """Chain B(리스크 시나리오) 결과를 Chain 2 기본 결과에 병합.

    - 별도 필드 risk_scenarios에 전체 시나리오 보존 (UI·추적용)
    - preventable_by(예방 전략)에 따라 적절한 위치에 분배:
      - strengthen/extend + target_locator 매칭 → 해당 observation.issues에 추가
      - add_new 또는 매칭 실패 → gaps(누락 조항)에 추가
      - external → external_considerations(외부 고려사항)에 추가
    """
    if not isinstance(risk, dict):
        return base
    scenarios = risk.get("scenarios") or []
    if not scenarios:
        return base
    base = dict(base) if isinstance(base, dict) else {}
    base["risk_scenarios"] = scenarios

    def _norm(s: str) -> str:
        return (s or "").replace(" ", "").lower()

    observations = base.get("observations") or []
    loc_index: dict[str, dict] = {}
    for o in observations:
        if isinstance(o, dict):
            loc_index[_norm(o.get("locator", ""))] = o

    for s in scenarios:
        if not isinstance(s, dict):
            continue
        strategy = s.get("preventable_by") or ""
        target = (s.get("target_locator") or "").strip()
        trigger = s.get("trigger") or ""
        cause = s.get("root_cause") or ""
        suggestion = s.get("suggestion") or ""
        severity = s.get("severity") if s.get("severity") in ("high", "medium", "low") else "low"
        basis = s.get("basis")
        affected = ", ".join(s.get("affected") or [])

        concern = f"[리스크 시나리오] {trigger}"
        if cause:
            concern += f" — 원인: {cause}"
        if affected:
            concern += f" — 영향: {affected}"

        if strategy in ("strengthen", "extend") and target and target not in ("외부", "신설"):
            obs = loc_index.get(_norm(target))
            if obs is not None:
                obs.setdefault("issues", []).append({
                    "severity": severity,
                    "concern": concern,
                    "suggestion": suggestion,
                    "basis": basis,
                })
                order = {"high": 3, "medium": 2, "low": 1, "ok": 0}
                cur = obs.get("severity") or "ok"
                if order.get(severity, 0) > order.get(cur, 0):
                    obs["severity"] = severity
                obs["issues"][-1].setdefault("issue", obs["issues"][-1]["concern"])
                obs["issues"][-1].setdefault("legal_basis", basis)
                continue

        if strategy == "external":
            ext = base.get("external_considerations") or []
            seen = {(ec.get("section_title") or "", ec.get("topic") or "") for ec in ext if isinstance(ec, dict)}
            title = "예방 가능한 리스크"
            key = (title, trigger[:80])
            if key not in seen:
                ext.append({
                    "section_title": title,
                    "topic": trigger[:80],
                    "detail": concern,
                    "suggestion": suggestion,
                    "basis": basis,
                })
                base["external_considerations"] = ext
            continue

        # add_new 또는 매칭 실패 → gaps
        gaps = base.get("gaps") or []
        seen_g = {(g.get("topic") or "") for g in gaps if isinstance(g, dict)}
        topic = trigger[:120]
        if topic and topic not in seen_g:
            gaps.append({
                "topic": topic,
                "reason": cause or concern,
                "suggestion": suggestion,
                "_severity": severity,
                "_from": "risk_scenario",
            })
            base["gaps"] = gaps

    return base


def _merge_purpose_alignment(base: dict, purpose: dict) -> dict:
    """Chain 2.5(목적 정합성) 결과를 Chain 2 기본 결과에 병합.

    - declared_purpose(선언 목적)는 document_overview.purpose로 노출
    - alignment_gaps(정합성 결손)는:
      - target_locator가 있고 strengthen/extend 전략 → 해당 observation.issues에 추가
      - '신설'이거나 매칭 실패 시 gaps(누락 조항)로 추가
    """
    if not isinstance(purpose, dict):
        return base
    base = dict(base) if isinstance(base, dict) else {}

    # declared_purpose를 document_overview에 노출
    dp = (purpose.get("declared_purpose") or "").strip()
    if dp:
        overview = base.get("document_overview") or {}
        if isinstance(overview, dict):
            overview = dict(overview)
            if not overview.get("purpose"):
                overview["purpose"] = dp
            base["document_overview"] = overview

    gaps_from_purpose = purpose.get("alignment_gaps") or []
    if not gaps_from_purpose:
        return base

    observations = base.get("observations") or []
    # locator 정규화 (공백·"제X조" 표기 통일)
    def _norm(s: str) -> str:
        return (s or "").replace(" ", "").lower()

    loc_index: dict[str, dict] = {}
    for o in observations:
        if isinstance(o, dict):
            loc_index[_norm(o.get("locator", ""))] = o

    extra_gaps: list[dict] = []
    for g in gaps_from_purpose:
        if not isinstance(g, dict):
            continue
        strategy = g.get("fix_strategy") or ""
        target = (g.get("target_locator") or "").strip()
        gap_text = g.get("gap") or ""
        suggestion = g.get("suggestion") or ""
        severity = g.get("severity") if g.get("severity") in ("high", "medium", "low") else "low"
        basis = g.get("basis")

        attach_to = None
        if target and target != "신설" and strategy in ("strengthen", "extend"):
            attach_to = loc_index.get(_norm(target))

        if attach_to is not None:
            issues = attach_to.setdefault("issues", [])
            issues.append({
                "severity": severity,
                "concern": f"[목적 정합성] {gap_text}" + (f" — {g.get('why', '')}" if g.get("why") else ""),
                "suggestion": suggestion,
                "basis": basis,
            })
            # severity 갱신
            order = {"high": 3, "medium": 2, "low": 1, "ok": 0}
            cur = attach_to.get("severity") or "ok"
            if order.get(severity, 0) > order.get(cur, 0):
                attach_to["severity"] = severity
            # 구 필드 거울상
            attach_to["issues"][-1].setdefault("issue", attach_to["issues"][-1]["concern"])
            attach_to["issues"][-1].setdefault("legal_basis", basis)
        else:
            extra_gaps.append({
                "topic": gap_text,
                "reason": g.get("why") or "",
                "suggestion": suggestion,
                "_severity": severity,
                "_from": "purpose_alignment",
            })

    if extra_gaps:
        existing_gaps = base.get("gaps") or []
        seen = {(x.get("topic") or "") for x in existing_gaps if isinstance(x, dict)}
        for eg in extra_gaps:
            if eg["topic"] and eg["topic"] not in seen:
                seen.add(eg["topic"])
                existing_gaps.append(eg)
        base["gaps"] = existing_gaps

    return base


def _merge_institutional(base: dict, institutional: dict) -> dict:
    """Chain 3(제도적 검토) 결과를 Chain 2 기본 결과에 병합.

    Chain 2(문구 검토)가 이미 일부 external_considerations를 냈을 수 있으므로
    section_title+topic 기준으로 중복 제거 후 병합.
    - external_considerations: 외부 제도·절차 고려사항
    - gaps: 누락된 조항·규정
    - action_items: 권장 조치 항목
    """
    if not isinstance(institutional, dict):
        return base
    base = dict(base) if isinstance(base, dict) else {}

    existing_ext = base.get("external_considerations") or []
    seen_ext = {(ec.get("section_title") or "", ec.get("topic") or "") for ec in existing_ext if isinstance(ec, dict)}
    for ec in institutional.get("external_considerations") or []:
        if not isinstance(ec, dict):
            continue
        key = (ec.get("section_title") or "", ec.get("topic") or "")
        if key in seen_ext:
            continue
        seen_ext.add(key)
        existing_ext.append(ec)
    base["external_considerations"] = existing_ext

    existing_gaps = base.get("gaps") or []
    seen_gap = {(g.get("topic") or "") for g in existing_gaps if isinstance(g, dict)}
    for g in institutional.get("gaps") or []:
        if not isinstance(g, dict):
            continue
        t = g.get("topic") or ""
        if t and t in seen_gap:
            continue
        seen_gap.add(t)
        existing_gaps.append(g)
    base["gaps"] = existing_gaps

    actions = base.get("action_items") or []
    for a in institutional.get("action_items") or []:
        if a and a not in actions:
            actions.append(a)
    base["action_items"] = actions

    return base


def _adapt_review_result(result: dict) -> dict:
    """LLM 응답 스키마를 새/구 형식 양방향으로 정규화하여 프론트엔드 호환성 보장.

    - 구 필드(clause_reviews, parties 등)가 오면 새 필드(observations 등)로 매핑
    - 새 필드가 오면 구 필드(clause_reviews 등)에도 거울상으로 채움 (프론트 하위호환)
    - observations 내 각 항목은 _normalize_observation으로 정규화
    - gaps, key_points, external_considerations 등 리스트 필드 보장
    """
    if not isinstance(result, dict):
        return result

    # observations ← clause_reviews 하위호환 흡수
    obs = result.get("observations")
    if not isinstance(obs, list):
        obs = []
    legacy_clause = result.get("clause_reviews")
    if (not obs) and isinstance(legacy_clause, list):
        obs = list(legacy_clause)
    obs = [_normalize_observation(o) for o in obs if isinstance(o, dict)]
    result["observations"] = obs

    # 구 필드 거울상 — 기존 UI가 깨지지 않게
    if not result.get("clause_reviews"):
        result["clause_reviews"] = obs

    # document_overview ← parties/document_type/review_perspective 하위호환
    overview = result.get("document_overview")
    if not isinstance(overview, dict):
        overview = {}
    if not overview.get("nature") and result.get("document_type"):
        overview["nature"] = result.get("document_type")
    if not overview.get("principals") and isinstance(result.get("parties"), list):
        overview["principals"] = result.get("parties")
    if not overview.get("standpoint") and result.get("review_perspective"):
        overview["standpoint"] = result.get("review_perspective")
    result["document_overview"] = overview

    # 거울상 구 필드 채우기
    if not result.get("document_type") and overview.get("nature"):
        result["document_type"] = overview["nature"]
    if not result.get("parties") and isinstance(overview.get("principals"), list):
        result["parties"] = overview["principals"]

    # gaps ← missing_clauses 하위호환
    gaps = result.get("gaps")
    if not isinstance(gaps, list):
        gaps = []
    legacy_missing = result.get("missing_clauses")
    if (not gaps) and isinstance(legacy_missing, list):
        for mc in legacy_missing:
            if not isinstance(mc, dict):
                continue
            gaps.append({
                "topic": mc.get("clause") or mc.get("topic"),
                "reason": mc.get("reason"),
                "suggestion": mc.get("suggestion"),
            })
    result["gaps"] = gaps
    if not result.get("missing_clauses"):
        result["missing_clauses"] = [
            {"clause": g.get("topic"), "reason": g.get("reason"), "suggestion": g.get("suggestion")}
            for g in gaps if g.get("topic")
        ]

    # key_points ← key_negotiation_points 하위호환
    kp = result.get("key_points")
    if not isinstance(kp, list) or not kp:
        if isinstance(result.get("key_negotiation_points"), list):
            result["key_points"] = result["key_negotiation_points"]
    if not result.get("key_negotiation_points") and isinstance(result.get("key_points"), list):
        result["key_negotiation_points"] = result["key_points"]

    # external_considerations 보장 (리스트)
    if not isinstance(result.get("external_considerations"), list):
        result["external_considerations"] = []

    # review_axes 보장
    if not isinstance(result.get("review_axes"), list):
        result["review_axes"] = []

    return result


def _normalize_clause_issues(cr: dict) -> dict:
    """clause_review 항목의 issues 배열 정규화 및 severity 동기화.

    - 구 형식(issue/suggestion 단일 키)을 issues 배열로 래핑
    - severity가 유효하지 않으면 risk_level에서 상속, 없으면 'low'
    - issues 중 최고 severity를 risk_level에 반영
    """
    issues = cr.get("issues")
    if not isinstance(issues, list):
        issues = []
    legacy_issue = cr.get("issue")
    legacy_suggestion = cr.get("suggestion")
    if (legacy_issue or legacy_suggestion) and not issues:
        issues = [{
            "severity": cr.get("risk_level") if cr.get("risk_level") in ("high", "medium", "low") else "low",
            "issue": legacy_issue,
            "suggestion": legacy_suggestion,
            "legal_basis": cr.get("legal_basis"),
        }]
    # severity 보정 + 빈 항목 제거
    cleaned: list[dict] = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        if not (it.get("issue") or it.get("suggestion")):
            continue
        sev = it.get("severity")
        if sev not in ("high", "medium", "low"):
            it["severity"] = cr.get("risk_level") if cr.get("risk_level") in ("high", "medium", "low") else "low"
        cleaned.append(it)
    cr["issues"] = cleaned
    # risk_level 동기화: issues 중 가장 높은 severity 반영 (단, 이미 ok면 유지)
    if cleaned:
        order = {"high": 3, "medium": 2, "low": 1}
        top = max(cleaned, key=lambda x: order.get(x.get("severity", "low"), 1))
        top_sev = top.get("severity", "low")
        cur = cr.get("risk_level")
        if cur not in ("high", "medium", "low", "ok") or order.get(cur, 0) < order.get(top_sev, 0):
            cr["risk_level"] = top_sev
    return cr


# 채팅 답변의 JSON 출력 스키마 정의.
# Gemini의 response_schema로 전달하여 구조화된 답변(summary, answer, citations 등)을 강제함.
# judgment 블록은 판단형 질문("~할 수 있나요?")에만 채워지며, 아니면 생략됨.
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "answer": {"type": "string"},
        "grounded": {"type": "boolean"},
        "decision_factors": {
            "type": "array",
            "items": {"type": "string"},
        },
        "action_items": {
            "type": "array",
            "items": {"type": "string"},
        },
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["chunk_id", "reason"],
            },
        },
        "judgment": {
            "type": "object",
            "properties": {
                "is_judgment_question": {"type": "boolean"},
                "verdict": {"type": "string"},
                "short_answer": {"type": "string"},
                "reasoning": {"type": "string"},
                "missing_facts": {"type": "array", "items": {"type": "string"}},
                "typical_path": {"type": "string"},
            },
        },
    },
    "required": ["summary", "answer", "grounded", "decision_factors", "action_items", "citations"],
}


class GeminiService:
    """Gemini API를 활용한 법률 AI 서비스 클래스.

    임베딩, 질의 확장, 판례 랭킹, 근거 기반 답변 생성, 문서 검토 등
    법률 RAG 파이프라인의 핵심 LLM 호출을 담당한다.
    """

    # 질의 확장 결과를 LRU 방식으로 캐싱 (최대 500개)
    _expand_cache: OrderedDict[str, list[str]] = OrderedDict()
    _EXPAND_CACHE_MAX = 500

    def __init__(self) -> None:
        """Gemini API 클라이언트 초기화. API 키가 없으면 client=None으로 설정."""
        self.settings = get_settings()
        # API 키가 설정되어 있을 때만 클라이언트 생성
        self.client = (
            genai.Client(api_key=self.settings.gemini_api_key)
            if self.settings.gemini_api_key
            else None
        )
        # 임베딩 API 호출 실패 시 False로 전환하여 이후 호출 차단
        self._embedding_available = True

    def is_configured(self) -> bool:
        """API 키가 설정되어 Gemini 클라이언트가 사용 가능한지 확인."""
        return self.client is not None

    def embed_text(self, text: str) -> list[float] | None:
        """텍스트를 임베딩 벡터(기본 768차원)로 변환.

        - 클라이언트 미설정 또는 이전 실패 시 None 반환
        - 한 번 실패하면 _embedding_available=False로 전환하여 이후 호출 차단
        """
        if not self.client or not self._embedding_available:
            return None

        try:
            # Gemini 임베딩 모델로 벡터 생성 요청
            response = self.client.models.embed_content(
                model=self.settings.gemini_embedding_model,
                contents=text,
                config=types.EmbedContentConfig(
                    output_dimensionality=self.settings.embedding_dim,
                ),
            )
        except Exception as exc:  # pragma: no cover - depends on network/provider state
            # 네트워크/API 오류 시 이번 실행에서는 임베딩 비활성화
            self._embedding_available = False
            LOGGER.warning("Embedding request failed, disabling vector ingest for this run: %s", exc)
            return None
        embeddings = response.embeddings or []
        if not embeddings:
            return None
        return list(embeddings[0].values)

    def analyze_intent(self, question: str) -> dict[str, Any]:
        """사용자 질문의 의도(intent)를 분석하고 검색 전략을 수립.

        반환: intent(유형), keywords(검색 키워드), question_focus(초점), needs_follow_up(추가질문 필요 여부)
        실패 시 기본값(general_qa)으로 폴백.
        """
        if not self.client:
            return {"intent": "general_qa", "keywords": [], "question_focus": "일반", "needs_follow_up": False}

        try:
            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=_load_prompt("analyze_intent.txt").format(question=question),
                config=types.GenerateContentConfig(temperature=0.0),
            )
            if response.text:
                raw = response.text.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()
                return json.loads(raw)
        except Exception as exc:
            LOGGER.warning("Intent analysis failed: %s", exc)

        return {"intent": "general_qa", "keywords": [], "question_focus": "일반", "needs_follow_up": False}

    def expand_query(self, question: str) -> list[str]:
        """사용자 질문을 법률 검색용 키워드 최대 8개로 확장. LRU 캐시 적용.

        - OrderedDict 기반 LRU 캐시 (최대 500개)로 동일 질문 재호출 방지
        - Gemini에 JSON 배열 스키마를 강제하여 파싱 안정성 확보
        """
        if not self.client:
            return []

        # 캐시 히트
        cached = self._expand_cache.get(question)
        if cached is not None:
            self._expand_cache.move_to_end(question)
            return cached

        prompt = _load_prompt("expand_query.txt").format(question=question)

        result: list[str] = []
        try:
            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema={"type": "array", "items": {"type": "string"}},
                ),
            )
            if response.text:
                keywords = json.loads(response.text)
                if isinstance(keywords, list):
                    result = [str(k) for k in keywords[:8]]
        except Exception as exc:
            LOGGER.warning("Query expansion failed: %s", exc)

        # 캐시 저장 (LRU)
        self._expand_cache[question] = result
        if len(self._expand_cache) > self._EXPAND_CACHE_MAX:
            self._expand_cache.popitem(last=False)

        return result

    def rank_precedents(self, question: str, precedents: list[dict], *, top_k: int | None = None) -> list[int]:
        """판례 목록을 사용자 질문과의 관련성 순으로 LLM 랭킹. 상위 인덱스 리스트 반환.

        - 각 판례의 사건명+요지 앞 150자를 LLM에 전달
        - JSON 정수 배열로 관련도 높은 순서의 인덱스를 받음
        - 실패 시 원래 순서(0, 1, 2, ...) 그대로 반환
        - top_k: 반환할 최대 개수 (기본 min(len, 5))
        """
        if not self.client or not precedents:
            return list(range(len(precedents)))

        # summary 가 길면 prompt 폭주 — rank 판단엔 앞부분 400자면 충분
        def _trim(s: str) -> str:
            s = (s or "").strip()
            return s[:400] + ("…" if len(s) > 400 else "")

        cases_text = "\n".join(
            f"{i}. {p.get('case_name', '?')} [{p.get('court_name','')} {p.get('judgment_date','')} 선고 {p.get('case_number','')} 판결]\n요지: {_trim(p.get('summary') or '')}"
            for i, p in enumerate(precedents)
        )

        max_items = top_k if top_k else min(len(precedents), 5)
        max_items = min(max_items, len(precedents))
        prompt = _load_prompt("rank_precedents.txt").format(
            question=question, cases_text=cases_text, max_items=max_items,
        )

        try:
            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema={"type": "array", "items": {"type": "integer"}},
                ),
            )
            if response.text:
                indices = json.loads(response.text)
                if isinstance(indices, list):
                    return [i for i in indices if isinstance(i, int) and 0 <= i < len(precedents)]
        except Exception as exc:
            LOGGER.warning("Precedent ranking failed: %s", exc)

        return list(range(len(precedents)))

    def generate_follow_up_questions(
        self,
        question: str,
        conversation_history: list[dict[str, str]],
    ) -> dict[str, Any]:
        """대화형 추가 질문 생성 (Ouroboros 패턴).

        - 대화 이력을 분석하여 정보가 부족하면 후속 질문을 생성
        - 충분하면 ready=True와 함께 수집된 사실관계·도메인·분석 질문을 반환
        - 실패 시 ready=True로 폴백하여 즉시 분석 진행
        """
        if not self.client:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        history_text = ""
        if conversation_history:
            history_text = "\n".join(
                f"{'사용자' if m['role'] == 'user' else 'AI'}: {m['content']}"
                for m in conversation_history
            )

        prompt = _load_prompt("follow_up_questions.txt").format(
            history_text=history_text if history_text else "(첫 질문)",
            question=question,
        )

        try:
            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                ),
            )
            if response.text:
                raw = response.text.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()
                return json.loads(raw)
        except Exception as exc:
            LOGGER.warning("Follow-up question generation failed: %s", exc)

        # Fallback: just say ready and analyze with what we have
        return {"ready": True, "gathered_facts": {}, "domain": "일반", "analysis_question": question}

    def generate_structured_analysis(
        self,
        *,
        question: str,
        facts: dict[str, str],
        domain: str,
        chunks: list[RetrievedChunk],
    ) -> dict[str, Any]:
        """수집된 사실관계와 법령 청크를 기반으로 구조화된 법률 분석 생성.

        - facts: 대화에서 수집한 사실관계 (key-value)
        - chunks: RAG로 검색된 관련 법령 청크 (최대 10개, 각 500자 제한)
        - 반환: summary, answer, favorable/cautionary facts, action_plan 등
        """
        if not self.client:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        facts_text = "\n".join(f"- {k}: {v}" for k, v in facts.items())
        # v3: 500자 제한 제거 — 법령 조문 맥락 손실 방지
        chunks_text = "\n\n".join(
            f"[{c.id}] {c.law_title} {c.article_no or ''} {c.article_title or ''}\n{c.text}"
            for c in chunks[:10]
        )

        prompt = _load_prompt("structured_analysis.txt").format(
            domain=domain,
            facts_text=facts_text,
            chunks_text=chunks_text,
        )

        try:
            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.2),
            )
            if response.text:
                raw = response.text.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()
                return json.loads(raw)
        except Exception as exc:
            LOGGER.warning("Structured analysis failed: %s", exc)

        return {
            "summary": "분석을 완료하지 못했습니다.",
            "answer": "죄송합니다. 분석 중 오류가 발생했습니다. 자유 질문으로 다시 시도해 주세요.",
            "favorable_facts": [], "cautionary_facts": [],
            "recommended_evidence": [], "action_plan": [],
            "key_deadlines": [], "estimated_cost": "미정", "estimated_timeline": "미정",
        }

    def generate_document_draft(
        self,
        *,
        document_type: str,
        facts: dict[str, str],
        domain: str,
    ) -> dict[str, str]:
        """법률 서류 초안 생성 (내용증명, 진정서 등).

        - 사실관계와 도메인을 기반으로 서류 본문·안내사항·비용 추정 생성
        - 프롬프트 파일이 없는 서류 유형은 미지원 안내 반환
        """
        if not self.client:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        facts_text = "\n".join(f"- {k}: {v}" for k, v in facts.items())

        try:
            prompt = _load_prompt("document_draft.txt").format(
                document_type=document_type, domain=domain, facts_text=facts_text,
            )
        except Exception:
            prompt = None
        if not prompt:
            return {
                "title": document_type,
                "content": f"'{document_type}' 유형의 서류 초안은 아직 지원하지 않습니다.",
                "instructions": "",
                "estimated_cost": "",
                "disclaimer": "이 초안은 참고용이며, 발송/제출 전 전문가 검토를 권장합니다.",
            }

        try:
            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.2),
            )
            if response.text:
                raw = response.text.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()
                return json.loads(raw)
        except Exception as exc:
            LOGGER.warning("Document draft generation failed: %s", exc)

        return {
            "title": document_type,
            "content": "서류 초안 생성 중 오류가 발생했습니다. 다시 시도해 주세요.",
            "instructions": "",
            "estimated_cost": "",
            "disclaimer": "이 초안은 참고용이며, 발송/제출 전 전문가 검토를 권장합니다.",
        }

    _GROUNDED_FALLBACK: dict[str, Any] = {
        "summary": "근거를 충분히 구성하지 못했습니다.",
        "answer": "현재 검색된 법령 근거만으로는 답변을 구성하지 못했습니다.",
        "grounded": False,
        "decision_factors": [],
        "action_items": [],
        "citations": [],
    }

    def generate_grounded_answer(
        self,
        *,
        question: str,
        chunks: list[RetrievedChunk],
    ) -> dict[str, Any]:
        """검색된 법령 청크를 근거로 JSON 스키마 제약 답변 생성.

        - ANSWER_SCHEMA를 response_schema로 전달하여 구조화된 출력 강제
        - 각 청크의 id, 법령명, 조항번호, 원문을 evidence로 조합
        - transient 오류(5xx/타임아웃/빈 응답)는 지수 백오프 재시도
        - schema/permanent 오류 및 모든 재시도 실패 시 grounded=False 폴백 반환
        """
        if not self.client:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        evidence = []
        for chunk in chunks:
            evidence.append(
                "\n".join(
                    [
                        f"chunk_id: {chunk.id}",
                        f"law_title: {chunk.law_title}",
                        f"article_no: {chunk.article_no or '-'}",
                        f"article_title: {chunk.article_title or '-'}",
                        f"text: {chunk.text}",
                    ]
                )
            )

        prompt = _load_prompt("grounded_answer.txt").format(
            question=question,
            evidence="\n\n".join(evidence),
        )

        try:
            return self._call_llm_json(
                prompt,
                temperature=0.0,
                timeout_s=60.0,
                max_retries=2,
                schema=ANSWER_SCHEMA,
                chain_name="grounded_answer",
            )
        except Exception as exc:
            # Chat 본체가 500으로 떨어지지 않게 폴백 응답으로 회복.
            # 호출자가 'grounded=False'를 보고 인용 검증을 우회한다.
            LOGGER.warning("grounded_answer fallback engaged: %s", exc)
            return dict(self._GROUNDED_FALLBACK)

    def _parse_clauses_llm(self, document_text: str) -> list[dict[str, str]]:
        """정규식 파싱 실패 시 LLM으로 조항 구조 추출 (검토 아닌 파싱만).

        - 문서 앞부분 15,000자만 전달하여 토큰 초과 방지
        - 2개 미만 조항이 나오면 전체를 하나의 조항으로 반환
        """
        if not self.client:
            return [{"clause_no": "전체", "title": "", "text": document_text}]

        try:
            # v3: parse_clauses 15k자 제한 제거 — 긴 계약서 뒷부분 조항 누락 방지.
            # Gemini 2.5 Flash-Lite가 100만 토큰 context 지원하므로 전체 주입 가능.
            prompt = _load_prompt("parse_clauses.txt").format(document_text=document_text)
            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.0),
            )
            if response.text:
                raw = response.text.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()
                parsed = json.loads(raw)
                if isinstance(parsed, list) and len(parsed) >= 2:
                    LOGGER.info("LLM clause parsing: %d clauses extracted", len(parsed))
                    return parsed
        except Exception as exc:
            LOGGER.warning("LLM clause parsing failed: %s", exc)

        return [{"clause_no": "전체", "title": "", "text": document_text}]

    def extract_issues(self, clauses: list[dict[str, str]], question: str) -> list[dict[str, Any]]:
        """파싱된 조항에서 법적 쟁점을 추출하고 RAG 검색 키워드를 생성.

        - 최대 20개 조항, 각 200자까지 요약하여 LLM에 전달
        - 반환: 쟁점별 search_keywords 포함 리스트 (축별 RAG 폴백용)
        """
        if not self.client:
            return []

        # v3: 조항 200자 절단 → 800자 (쟁점 추출에 필요한 최소 맥락 확보)
        clauses_summary = "\n".join(
            f"제{cl['clause_no']}조 {cl.get('title', '')}: {cl['text'][:800]}"
            for cl in clauses[:20]
        )

        try:
            prompt = _load_prompt("extract_issues.txt").format(
                clauses_summary=clauses_summary,
                question=question,
            )
            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            if response.text:
                result = json.loads(response.text)
                if isinstance(result, list):
                    return result
        except Exception as exc:
            LOGGER.warning("Issue extraction failed: %s", exc)

        return []

    def _call_chain(
        self,
        prompt_name: str,
        format_vars: dict[str, str],
        *,
        chain_name: str = "chain",
        default: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """공통 체인 호출: 프롬프트 로드 → LLM 호출 → JSON 파싱."""
        fallback = default or {}
        if not self.client:
            return fallback
        try:
            prompt = _load_prompt(prompt_name).format(**format_vars)
            result = self._call_review(prompt)
            if not isinstance(result, dict):
                return fallback
            return result
        except Exception as exc:
            LOGGER.warning("%s chain failed: %s", chain_name, exc)
            return fallback

    def review_judgment(
        self,
        *,
        document_excerpt: str,
        scan_context: str,
        institutional_frame: str,
        axes: list[dict[str, Any]],
        rejected_citations: list[dict[str, Any]],
        evidence: str,
        question: str,
    ) -> dict[str, Any]:
        """Chain D: 사용자 질문이 판단형인지 판별하고, 판단형이면 종합 결론 생성.

        - is_judgment_question=False → UI에서 숨김 (판단 불필요 질문)
        - is_judgment_question=True → verdict(likely_yes/no/depends 등), reasoning, missing_facts 등 채움
        - 제도 프레임, 축, 기각 인용, evidence를 종합하여 판단
        """
        if not self.client:
            return {}
        try:
            axes_block = "\n".join(
                f"- {ax.get('name', '?')}: {ax.get('why', '')}" for ax in axes
            ) or "(선언된 축 없음)"
            rejected_block = "\n".join(
                f"- {r.get('ref', '?')}: {r.get('reason', '')}" for r in rejected_citations
            ) or "(없음)"
            prompt = _load_prompt("judgment.txt").format(
                institutional_frame=institutional_frame or "(없음)",
                axes_block=axes_block,
                rejected_block=rejected_block,
                scan_context=scan_context or "(없음)",
                evidence=evidence or "(없음)",
                document_excerpt=document_excerpt[:8000],
                question=question or "(없음)",
            )
            result = self._call_review(prompt)
            if not isinstance(result, dict):
                return {}
            if not result.get("is_judgment_question"):
                return {"is_judgment_question": False, "judgments": []}

            valid_verdicts = ("likely_yes", "likely_no", "depends", "needs_more_info", "not_applicable")

            raw_judgments = result.get("judgments")
            if not isinstance(raw_judgments, list) or not raw_judgments:
                # 구 스키마 폴백: 단일 verdict 필드가 최상위에 있는 경우
                if result.get("verdict"):
                    raw_judgments = [{
                        "issue_label": result.get("issue_label") or "",
                        "verdict": result.get("verdict"),
                        "short_answer": result.get("short_answer") or "",
                        "reasoning": result.get("reasoning") or "",
                        "key_authorities": result.get("key_authorities") or [],
                        "missing_facts": result.get("missing_facts") or [],
                        "typical_path": result.get("typical_path") or "",
                        "contradicting_views": result.get("contradicting_views") or [],
                    }]
                else:
                    raw_judgments = []

            judgments: list[dict[str, Any]] = []
            for j in raw_judgments:
                if not isinstance(j, dict):
                    continue
                v = j.get("verdict")
                if v not in valid_verdicts:
                    v = "depends"
                judgments.append({
                    "issue_label": j.get("issue_label") or "",
                    "verdict": v,
                    "short_answer": j.get("short_answer") or "",
                    "reasoning": j.get("reasoning") or "",
                    "key_authorities": j.get("key_authorities") or [],
                    "missing_facts": j.get("missing_facts") or [],
                    "typical_path": j.get("typical_path") or "",
                    "contradicting_views": j.get("contradicting_views") or [],
                })

            out: dict[str, Any] = {
                "is_judgment_question": True,
                "judgments": judgments,
                "rejected_citations": result.get("rejected_citations") or [],
            }
            # 하위호환: 첫 판단을 최상위에도 노출 (기존 소비처 — ask.py, slack/bot.py, web/page.tsx)
            if judgments:
                first = judgments[0]
                out.update({
                    "verdict": first["verdict"],
                    "short_answer": first["short_answer"],
                    "reasoning": first["reasoning"],
                    "key_authorities": first["key_authorities"],
                    "missing_facts": first["missing_facts"],
                    "typical_path": first["typical_path"],
                    "contradicting_views": first["contradicting_views"],
                })
            return out
        except Exception as exc:
            LOGGER.warning("Judgment chain failed: %s", exc)
            return {}

    def review_risk_scenarios(
        self,
        *,
        document_excerpt: str,
        scan_context: str,
        institutional_frame: str,
        question: str,
    ) -> dict[str, Any]:
        """Chain B: 문서에서 발생 가능한 리스크 시나리오를 전담 도출."""
        result = self._call_chain(
            "risk_scenarios.txt",
            dict(scan_context=scan_context or "(없음)", institutional_frame=institutional_frame or "(없음)",
                 document_excerpt=document_excerpt[:8000], question=question or "(없음)"),
            chain_name="risk_scenarios", default={"scenarios": []},
        )
        scenarios = [s for s in (result.get("scenarios") or []) if isinstance(s, dict)]
        return {"scenarios": scenarios}

    def review_purpose_alignment(
        self,
        *,
        document_excerpt: str,
        scan_context: str,
        institutional_frame: str,
        question: str,
    ) -> dict[str, Any]:
        """Chain 2.5: 문서의 선언 목적 대비 수단·조항의 정합성(결손) 검토."""
        result = self._call_chain(
            "purpose_alignment.txt",
            dict(scan_context=scan_context or "(없음)", institutional_frame=institutional_frame or "(없음)",
                 document_excerpt=document_excerpt[:8000], question=question or "(없음)"),
            chain_name="purpose_alignment", default={"declared_purpose": "", "alignment_gaps": []},
        )
        gaps = [g for g in (result.get("alignment_gaps") or []) if isinstance(g, dict)]
        return {"declared_purpose": result.get("declared_purpose") or "", "alignment_gaps": gaps}

    def review_institutional(
        self,
        *,
        document_excerpt: str,
        scan_context: str,
        institutional_frame: str,
        axes: list[dict[str, Any]],
        evidence: str,
        question: str,
    ) -> dict[str, Any]:
        """Chain 3: 문서 외부의 제도·절차·연계 요건 집중 점검."""
        default = {"external_considerations": [], "gaps": [], "action_items": []}
        if not axes and not institutional_frame:
            return default
        axes_block = "\n".join(
            f"- {ax.get('name', '?')}: {ax.get('why', '')}" for ax in axes
        ) or "(선언된 축 없음 — 문서 성격에서 추론)"
        result = self._call_chain(
            "institutional_review.txt",
            dict(institutional_frame=institutional_frame or "(없음)", axes_block=axes_block,
                 scan_context=scan_context or "(없음)", evidence=evidence or "(없음)",
                 document_excerpt=document_excerpt[:6000], question=question or "(없음)"),
            chain_name="institutional_review", default=default,
        )
        return {
            "external_considerations": result.get("external_considerations") or [],
            "gaps": result.get("gaps") or [],
            "action_items": result.get("action_items") or [],
        }

    def map_institutional_frame(
        self,
        *,
        document_excerpt: str,
        scan_context: str,
        question: str,
    ) -> dict[str, Any]:
        """Chain 1.5: 문서의 제도 프레임·검토 축(axes)·RAG 검색 쿼리를 선언.

        - 문서 내부 조항뿐 아니라 외부 법적·제도적 체계를 LLM이 먼저 파악
        - axes: 검토해야 할 축(이름, 이유, lookup_queries)
        - rejected_citations: 문서에 인용되었으나 부적절한 법령 참조
        - lookup_queries는 이후 축별 RAG 검색에 사용됨
        """
        if not self.client:
            return {"institutional_frame": "", "axes": []}
        try:
            cited = _extract_cited_statutes((question or "") + " " + (document_excerpt or "")[:3000])
            prompt = _load_prompt("institutional_mapping.txt").format(
                scan_context=scan_context or "(없음)",
                document_excerpt=document_excerpt[:6000],
                question=question or "(없음)",
                cited_statutes="\n".join(f"- {s}" for s in cited) if cited else "(없음)",
            )
            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            )
            if not response.text:
                return {"institutional_frame": "", "axes": []}
            result = json.loads(response.text)
            if isinstance(result, dict):
                axes = result.get("axes") or []
                if isinstance(axes, list):
                    cleaned: list[dict[str, Any]] = []
                    for ax in axes:
                        if not isinstance(ax, dict):
                            continue
                        name = (ax.get("name") or "").strip()
                        if not name:
                            continue
                        qs = ax.get("lookup_queries") or []
                        qs = [q.strip() for q in qs if isinstance(q, str) and q.strip()]
                        cleaned.append({
                            "name": name,
                            "why": ax.get("why") or "",
                            "lookup_queries": qs[:6],
                        })
                    result["axes"] = cleaned
                rejected = result.get("rejected_citations") or []
                if isinstance(rejected, list):
                    result["rejected_citations"] = [
                        {"ref": (r.get("ref") or "").strip(), "reason": (r.get("reason") or "").strip()}
                        for r in rejected if isinstance(r, dict) and r.get("ref")
                    ]
                else:
                    result["rejected_citations"] = []
                return result
        except Exception as exc:
            LOGGER.warning("Institutional mapping failed: %s", exc)
        return {"institutional_frame": "", "axes": [], "rejected_citations": []}

    @staticmethod
    def _parse_clauses(document_text: str) -> list[dict[str, str]]:
        """정규식으로 문서 텍스트에서 조항(제X조, 1., Article N 등)을 구조적으로 추출.

        - 6가지 패턴 후보 중 가장 많이 매칭되는 패턴을 자동 선택
        - 2개 미만 매칭 시 전체를 하나의 '전체' 조항으로 반환 (LLM 파싱 폴백 유도)
        - 중복 번호는 첫 등장만 사용
        """
        import re
        clauses: list[dict[str, str]] = []

        # 여러 패턴 시도 — 매칭되는 패턴 중 가장 많이 잡히는 걸 사용
        pattern_candidates = [
            re.compile(r'^제\s*(\d+)\s*조[\s(]*(.*)', re.MULTILINE),
            re.compile(r'^(\d+)\.\s+(.+)', re.MULTILINE),
            re.compile(r'^Article\s+(\d+)[\.\s]+(.*)', re.MULTILINE | re.IGNORECASE),
            re.compile(r'^Section\s+(\d+)[\.\s]+(.*)', re.MULTILINE | re.IGNORECASE),
            re.compile(r'^Clause\s+(\d+)[\.\s]+(.*)', re.MULTILINE | re.IGNORECASE),
            re.compile(r'^\((\d+)\)\s+(.+)', re.MULTILINE),
        ]

        # 각 패턴으로 매칭 시도, 가장 많이 잡히는 패턴 선택
        best_positions: list[tuple[int, str, str]] = []
        for pattern in pattern_candidates:
            positions: list[tuple[int, str, str]] = []
            for m in pattern.finditer(document_text):
                clause_no = m.group(1)
                title_hint = m.group(2).strip()[:80] if m.group(2) else ""
                positions.append((m.start(), clause_no, title_hint))
            if len(positions) > len(best_positions):
                best_positions = positions

        positions = best_positions

        if not positions:
            # 정규식으로 안 잡히면 전체를 넘기고 모델이 판단
            return [{"clause_no": "전체", "title": "", "text": document_text}]

        # 조항이 2개 미만이면 파싱 실패로 간주 → 전체를 넘김
        if len(set(p[1] for p in positions)) < 2:
            return [{"clause_no": "전체", "title": "", "text": document_text}]

        # 위치순 정렬 후 중복 번호 제거 (첫 등장만)
        positions.sort(key=lambda x: x[0])
        seen_nos: set[str] = set()
        unique_positions: list[tuple[int, str, str]] = []
        for pos, no, title in positions:
            if no not in seen_nos:
                seen_nos.add(no)
                unique_positions.append((pos, no, title))

        # 각 조항의 텍스트 범위 추출
        for i, (pos, no, title) in enumerate(unique_positions):
            end_pos = unique_positions[i + 1][0] if i + 1 < len(unique_positions) else len(document_text)
            text = document_text[pos:end_pos].strip()
            # 제목 추출: 첫 줄에서 번호 제거한 나머지
            first_line = text.split('\n')[0]
            title_clean = re.sub(r'^[\d]+\.\s*', '', first_line).strip()[:100]
            clauses.append({
                "clause_no": no,
                "title": title_clean or title,
                "text": text,
            })

        return clauses

    def _build_review_prompt(
        self,
        *,
        clauses: list[dict[str, str]],
        question: str,
        evidence: str,
        batch_label: str = "",
        previous_summary: str = "",
        sibling_docs_hint: str = "",
    ) -> str:
        """Chain 2(조항별 상세 검토)용 프롬프트 조립.

        - 배치 분할 시 이전 배치 결과를 previous_summary로 연계
        - 파싱 실패(전체 1건) 시 모델에게 조항 구분을 위임
        - 관련 문서 힌트(sibling_docs_hint)는 참고용으로만 전달
        """
        # 파싱이 안 된 경우 (전체 1건) → 모델에게 조항 구분을 맡김
        unparsed = len(clauses) == 1 and clauses[0]["clause_no"] == "전체"

        if unparsed:
            clauses_section = f"\n{clauses[0]['text']}\n"
            clause_numbers = "(자동 파싱 실패 — 모델이 직접 조항을 구분하여 검토)"
            clause_count_str = "문서를 읽고 조항을 직접 구분한 뒤, 구분한 조항 수만큼"
        else:
            clauses_section = ""
            for cl in clauses:
                # v3: 조항 2000자 절단 제거 — 긴 조항 뒷부분 누락 방지
                clauses_section += f"\n### 제{cl['clause_no']}조 {cl['title']}\n{cl['text']}\n"
            clause_numbers = ", ".join(f"제{cl['clause_no']}조" for cl in clauses)
            clause_count_str = str(len(clauses))

        prev_context = ""
        if sibling_docs_hint:
            prev_context += f"""
## 관련 문서 목록 (참고만 — 이 문서의 "누락"으로 판단하지 말 것)
{sibling_docs_hint}
"""
        if previous_summary:
            prev_context += f"""
## 이전 배치 검토 결과 요약
{previous_summary}
위 내용을 참고하여, 아래 조항을 이어서 검토하라. 이전에 검토한 내용과 일관성을 유지하라.
"""

        return _load_prompt("review_document.txt").format(
            batch_label=batch_label,
            clause_count_str=clause_count_str,
            evidence=evidence if evidence else "(없음)",
            prev_context=prev_context,
            clause_numbers=clause_numbers,
            clauses_section=clauses_section,
            question=question,
        )

    @staticmethod
    def _classify_llm_failure(exc: Exception, response: Any = None) -> str:
        """LLM 호출 실패 유형 분류.

        반환: 'permanent' | 'schema' | 'transient'
        - permanent: 재시도 무의미 — 인증/할당량/safety filter/max_tokens 종료/잘못된 인자
        - schema   : JSON 파싱 실패 — 동일 temperature에서는 재시도해도 거의 같음
        - transient: 5xx, 타임아웃, 빈 응답 등 — 재시도 가치 있음
        """
        if isinstance(exc, json.JSONDecodeError):
            return "schema"
        msg = str(exc).lower()
        if any(k in msg for k in ("permission", "unauthenticated", "api key", "quota", "invalid argument")):
            return "permanent"
        # finish_reason 기반 분류 (있을 때만)
        try:
            candidates = getattr(response, "candidates", None) or []
            if candidates:
                finish = getattr(candidates[0], "finish_reason", None)
                finish_str = str(finish).upper() if finish is not None else ""
                if "SAFETY" in finish_str or "MAX_TOKENS" in finish_str or "RECITATION" in finish_str:
                    return "permanent"
        except Exception:
            pass
        return "transient"

    def _call_llm_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.1,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        schema: Any = None,
        chain_name: str = "llm",
    ) -> dict[str, Any]:
        """LLM JSON 호출 공통 래퍼.

        - response_mime_type='application/json'으로 마크다운 래핑 원천 제거
        - schema 인자가 있으면 response_schema도 함께 전달
        - 호출 자체를 ThreadPoolExecutor로 감싸 timeout_s 강제 (SDK 기본 타임아웃 의존 X)
        - 실패 분류:
            permanent → 즉시 raise
            schema    → 재시도 없이 raise (호출자가 폴백)
            transient → 지수 백오프 재시도 (1s → 2s → 4s)
        """
        import time as _time
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutTimeout

        cfg_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "response_mime_type": "application/json",
        }
        if schema is not None:
            cfg_kwargs["response_schema"] = schema
        config = types.GenerateContentConfig(**cfg_kwargs)

        def _do_call() -> Any:
            return self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=config,
            )

        last_exc: Exception | None = None
        last_response: Any = None
        for attempt in range(max_retries + 1):
            response: Any = None
            try:
                with ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(_do_call)
                    response = future.result(timeout=timeout_s)
                last_response = response
                if not getattr(response, "text", None):
                    raise ValueError("empty response text")
                raw = response.text.strip()
                # mime_type=json이라도 일부 모델 버전이 ``` 래핑을 남길 수 있어 방어적 처리 유지
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()
                return json.loads(raw)
            except _FutTimeout as exc:
                last_exc = TimeoutError(f"LLM call exceeded {timeout_s}s")
                kind = "transient"
            except Exception as exc:
                last_exc = exc
                kind = self._classify_llm_failure(exc, response or last_response)

            if kind == "permanent":
                LOGGER.warning("%s call failed (permanent): %s", chain_name, last_exc)
                raise last_exc
            if kind == "schema":
                LOGGER.warning("%s call failed (schema, no retry): %s", chain_name, last_exc)
                raise last_exc
            # transient
            if attempt < max_retries:
                backoff = 1.0 * (2 ** attempt)
                LOGGER.warning(
                    "%s call attempt %d/%d failed (transient: %s) — retry in %.1fs",
                    chain_name, attempt + 1, max_retries + 1, last_exc, backoff,
                )
                _time.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    def _call_review(self, prompt: str, *, max_retries: int = 2) -> dict[str, Any]:
        """리뷰 프롬프트를 Gemini LLM에 전송하고 응답을 JSON으로 파싱.

        _call_llm_json의 얇은 래퍼. 기존 호출자 호환을 위해 시그니처 유지.
        """
        return self._call_llm_json(
            prompt,
            temperature=0.1,
            timeout_s=60.0,
            max_retries=max_retries,
            chain_name="_call_review",
        )

    def review_document(
        self,
        *,
        document_text: str,
        question: str,
        chunks: list[RetrievedChunk],
        previous_review: dict[str, Any] | None = None,
        _sibling_docs_hint: str = "",
    ) -> dict[str, Any]:
        """문서 검토 메인 오케스트레이션 (Multi-Chain 파이프라인).

        실행 순서:
        1. 조항 파싱 (정규식 → 실패 시 LLM)
        2. Chain 1: 전체 스캔 (조항별 요약·리스크·연관 조항)
        3. Chain 1.5: 제도 프레임·축 선언 + 축별 RAG 검색
        4. Chain 2(조항별 상세 검토) + Chain 2.5/3/B/D 병렬 실행
        5. 결과 병합 및 정규화

        큰 문서는 20,000자 단위로 자동 분할하여 배치 처리.
        """
        if not self.client:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        # 조항 파싱 (정규식 → 실패 시 LLM 파싱)
        parsed_clauses = self._parse_clauses(document_text)
        if len(parsed_clauses) == 1 and parsed_clauses[0]["clause_no"] == "전체":
            parsed_clauses = self._parse_clauses_llm(document_text)

        # Chain 1: 전체 스캔 — 조항별 요약 + 리스크 + 연관 조항 파악
        scan_result = None
        try:
            clauses_for_scan = ""
            for cl in parsed_clauses:
                clauses_for_scan += f"\n### 제{cl['clause_no']}조 {cl.get('title', '')}\n{cl['text'][:800]}\n"

            scan_prompt = _load_prompt("review_scan.txt").format(
                clauses_section=clauses_for_scan,
                question=question,
            )
            scan_result = self._call_review(scan_prompt)
            scan_items = []
            if isinstance(scan_result, dict):
                scan_items = scan_result.get("section_scan") or scan_result.get("clause_scan") or []
            if scan_items:
                LOGGER.info("Chain 1 scan done: %d sections scanned", len(scan_items))
        except Exception as exc:
            LOGGER.warning("Chain 1 scan failed: %s", exc)

        # 스캔 결과를 Chain 2에 전달 — "지도"를 보면서 상세 검토
        scan_context = ""
        scan_items = []
        if isinstance(scan_result, dict):
            scan_items = scan_result.get("section_scan") or scan_result.get("clause_scan") or []
        if scan_items:
            scan_lines = []
            for cs in scan_items:
                locator = cs.get("locator") or f"제{cs.get('clause_no', '?')}조"
                severity = cs.get("severity") or cs.get("risk_level") or "ok"
                related_items = cs.get("related") or cs.get("related_clauses") or []
                related = ", ".join(item for item in related_items if isinstance(item, str))
                scan_lines.append(
                    f"{locator} [{severity}]: "
                    f"{cs.get('summary', '')} "
                    f"{'(연관: ' + related + ')' if related else ''}"
                )
            scan_context = "\n## 전체 조항 스캔 결과 (Chain 1 — 상세 검토 시 참고)\n" + "\n".join(scan_lines) + "\n"

        # Chain 1.5: 제도 프레임·축 선언 (문서 바깥의 법적·제도적 체계 스스로 도출)
        institutional = self.map_institutional_frame(
            document_excerpt=document_text,
            scan_context=scan_context,
            question=question,
        )
        frame_text = institutional.get("institutional_frame") or ""
        axes = institutional.get("axes") or []
        LOGGER.info(
            "Chain 1.5 institutional mapping: %d axes (%s)",
            len(axes),
            ", ".join(ax.get("name", "?") for ax in axes) or "none",
        )

        # 축별 RAG — 각 축의 lookup_queries로 벡터+lexical 앙상블 검색
        chunks_by_axis: dict[str, list[Any]] = {}
        existing_ids = {c.id for c in chunks}
        if chunks:
            chunks_by_axis["호출측 전달"] = list(chunks)

        for ax in axes:
            name = ax.get("name") or "축"
            qs = ax.get("lookup_queries") or []
            if not qs:
                continue
            try:
                axis_chunks = _retrieve_chunks_for_queries(
                    qs, existing_ids=existing_ids,
                )
                if axis_chunks:
                    chunks_by_axis[name] = axis_chunks
                    for c in axis_chunks:
                        existing_ids.add(c.id)
            except Exception as exc:
                LOGGER.warning("Axis RAG failed (%s): %s", name, exc)

        # 판례 축 — 사용자 질문·문서 발췌 기반으로 관련 판례 검색해 evidence에 포함
        # v3: rank_precedents 로 관련도 재정렬, 전문 포함
        try:
            prec_query = (question or "").strip() or document_text[:800]
            prec_chunks = _search_precedents_as_chunks(
                prec_query, limit=5, rerank_with=self,
            )
            if prec_chunks:
                chunks_by_axis["관련 판례"] = prec_chunks
                LOGGER.info("Precedent context: %d cases merged (reranked)", len(prec_chunks))
        except Exception as exc:
            LOGGER.warning("Precedent enrichment failed: %s", exc)

        # 축이 하나도 나오지 않았거나 모두 비었을 때의 fallback:
        # 기존 extract_issues 기반 쟁점 키워드로라도 RAG 돌려 evidence 확보
        if not any(k for k in chunks_by_axis if k != "호출측 전달"):
            try:
                issues = self.extract_issues(parsed_clauses, question)
                fallback_queries: list[str] = []
                for issue in issues[:8]:
                    fallback_queries.extend(issue.get("search_keywords", [])[:3])
                fallback_queries = list(dict.fromkeys(q for q in fallback_queries if q))[:12]
                if fallback_queries:
                    fb = _retrieve_chunks_for_queries(fallback_queries, existing_ids=existing_ids)
                    if fb:
                        chunks_by_axis["문서 쟁점 기반"] = fb
            except Exception as exc:
                LOGGER.warning("Fallback issue RAG failed: %s", exc)

        evidence_budget = _compute_evidence_budget(len(document_text))
        LOGGER.info("Evidence budget: %d chars (doc=%d chars, axes=%d)", evidence_budget, len(document_text), len(chunks_by_axis))
        evidence = _build_evidence(chunks_by_axis, total_budget=evidence_budget)

        # Chain 1 스캔 결과 + 이전 결과를 previous_summary에 합침
        previous_summary = scan_context  # Chain 1 결과가 Chain 2의 맥락
        if previous_review:
            prev_reviews = previous_review.get("clause_reviews", [])
            summary_lines = []
            for cr in prev_reviews:
                risk = cr.get("risk_level", "ok")
                if risk in ("high", "medium"):
                    summary_lines.append(
                        f"제{cr.get('clause_no', '?')}조 [{risk}]: {cr.get('issue', '')[:100]}"
                    )
            if summary_lines:
                previous_summary = "\n".join(summary_lines)
            prev_summary_text = previous_review.get("summary", "")
            if prev_summary_text:
                previous_summary = f"{prev_summary_text}\n\n주요 리스크:\n{previous_summary}"

        # 배치 크기 결정: 조항별 텍스트 합산이 ~20,000자를 넘으면 분할
        BATCH_CHAR_LIMIT = 20000
        batches: list[list[dict[str, str]]] = []
        current_batch: list[dict[str, str]] = []
        current_chars = 0

        for cl in parsed_clauses:
            # v3: len([:2000]) 버그 수정 — 실제 길이로 배치 분할 판정
            cl_chars = len(cl["text"])
            if current_chars + cl_chars > BATCH_CHAR_LIMIT and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_chars = 0
            current_batch.append(cl)
            current_chars += cl_chars

        if current_batch:
            batches.append(current_batch)

        from concurrent.futures import ThreadPoolExecutor

        def _run_institutional() -> dict[str, Any]:
            return self.review_institutional(
                document_excerpt=document_text,
                scan_context=scan_context,
                institutional_frame=frame_text,
                axes=axes,
                evidence=evidence,
                question=question,
            )

        def _run_purpose() -> dict[str, Any]:
            return self.review_purpose_alignment(
                document_excerpt=document_text,
                scan_context=scan_context,
                institutional_frame=frame_text,
                question=question,
            )

        def _run_risk() -> dict[str, Any]:
            return self.review_risk_scenarios(
                document_excerpt=document_text,
                scan_context=scan_context,
                institutional_frame=frame_text,
                question=question,
            )

        def _run_judgment() -> dict[str, Any]:
            return self.review_judgment(
                document_excerpt=document_text,
                scan_context=scan_context,
                institutional_frame=frame_text,
                axes=axes,
                rejected_citations=institutional.get("rejected_citations") or [],
                evidence=evidence,
                question=question,
            )

        try:
            if len(batches) == 1:
                # 단일 배치: Chain 2(detail) + Chain 3(institutional) 병렬
                prompt = self._build_review_prompt(
                    clauses=batches[0],
                    question=question,
                    evidence=evidence,
                    previous_summary=previous_summary,
                    sibling_docs_hint=_sibling_docs_hint,
                )
                with ThreadPoolExecutor(max_workers=CHAIN_PARALLEL_WORKERS) as executor:
                    detail_future = executor.submit(self._call_review, prompt)
                    institutional_future = executor.submit(_run_institutional)
                    purpose_future = executor.submit(_run_purpose)
                    risk_future = executor.submit(_run_risk)
                    judgment_future = executor.submit(_run_judgment)
                    result = detail_future.result()
                    institutional_review = institutional_future.result()
                    purpose_review = purpose_future.result()
                    risk_review = risk_future.result()
                    judgment_review = judgment_future.result()
                if result:
                    result = _adapt_review_result(result)
                    result = _merge_institutional(result, institutional_review)
                    result = _merge_purpose_alignment(result, purpose_review)
                    result = _merge_risk_scenarios(result, risk_review)
                    result["judgment"] = judgment_review
            else:
                # 다중 배치: Chain 2(detail) 배치 루프 + Chain 2.5/3/B/D 병렬
                side_executor = ThreadPoolExecutor(max_workers=CHAIN_PARALLEL_WORKERS)
                institutional_future = side_executor.submit(_run_institutional)
                purpose_future = side_executor.submit(_run_purpose)
                risk_future = side_executor.submit(_run_risk)
                judgment_future = side_executor.submit(_run_judgment)

                all_observations: list[dict] = []
                all_gaps: list[dict] = []
                all_externals: list[dict] = []
                all_key_points: list[str] = []
                all_actions: list[str] = []
                all_axes: list[str] = []
                batch_summaries: list[str] = []
                overview: dict[str, Any] = {}

                for i, batch in enumerate(batches):
                    batch_label = f"(배치 {i + 1}/{len(batches)}: 제{batch[0]['clause_no']}조~제{batch[-1]['clause_no']}조)"

                    carry_summary = previous_summary
                    if batch_summaries:
                        carry_summary += "\n" + "\n".join(batch_summaries)

                    prompt = self._build_review_prompt(
                        clauses=batch,
                        question=question,
                        evidence=evidence,
                        batch_label=batch_label,
                        previous_summary=carry_summary,
                        sibling_docs_hint=_sibling_docs_hint,
                    )

                    batch_result = self._call_review(prompt)
                    if not batch_result:
                        continue
                    batch_result = _adapt_review_result(batch_result)

                    if not overview:
                        overview = batch_result.get("document_overview") or {}

                    all_observations.extend(batch_result.get("observations", []))
                    all_gaps.extend(batch_result.get("gaps", []))
                    all_externals.extend(batch_result.get("external_considerations", []))
                    for kp in batch_result.get("key_points", []):
                        if kp and kp not in all_key_points:
                            all_key_points.append(kp)
                    for ai_ in batch_result.get("action_items", []):
                        if ai_ and ai_ not in all_actions:
                            all_actions.append(ai_)
                    for ax in batch_result.get("review_axes", []):
                        if ax and ax not in all_axes:
                            all_axes.append(ax)

                    batch_summary = batch_result.get("summary", "")
                    if batch_summary:
                        batch_summaries.append(f"배치 {i + 1}: {batch_summary}")

                    LOGGER.info("Document review batch %d/%d done: %d units", i + 1, len(batches), len(batch))

                sev_levels = [o.get("severity", "ok") for o in all_observations]
                if "high" in sev_levels:
                    overall_risk = "high"
                elif "medium" in sev_levels:
                    overall_risk = "medium"
                elif "low" in sev_levels:
                    overall_risk = "low"
                else:
                    overall_risk = "n/a"

                result = {
                    "document_overview": overview,
                    "detected_clauses": [f"제{cl['clause_no']}조 {cl['title']}" for cl in parsed_clauses],
                    "summary": " ".join(batch_summaries),
                    "review_axes": all_axes,
                    "observations": all_observations,
                    "gaps": all_gaps,
                    "external_considerations": all_externals,
                    "unreviewed_clauses": [],
                    "overall_risk": overall_risk,
                    "key_points": all_key_points,
                    "action_items": all_actions,
                    "disclaimer": "본 검토는 참고용이며 법률 자문이 아닙니다.",
                }
                result = _adapt_review_result(result)
                try:
                    institutional_review = institutional_future.result()
                    purpose_review = purpose_future.result()
                    risk_review = risk_future.result()
                    judgment_review = judgment_future.result()
                finally:
                    side_executor.shutdown(wait=False)
                result = _merge_institutional(result, institutional_review)
                result = _merge_purpose_alignment(result, purpose_review)
                result = _merge_risk_scenarios(result, risk_review)
                result["judgment"] = judgment_review

            # institutional_frame·axes·rejected_citations를 결과에 부착 (UI 노출용)
            if isinstance(result, dict):
                result["institutional_frame"] = frame_text
                result["axes"] = axes
                result["rejected_citations"] = institutional.get("rejected_citations") or []

            # detected_clauses 보장
            if "detected_clauses" not in result:
                result["detected_clauses"] = [f"제{cl['clause_no']}조 {cl['title']}" for cl in parsed_clauses]

            # 검증
            detected = len(parsed_clauses)
            reviewed = len(result.get("observations", []) or result.get("clause_reviews", []))
            if detected > 0 and reviewed < detected:
                result.setdefault("unreviewed_clauses", [])
                if not result["unreviewed_clauses"]:
                    result["unreviewed_clauses"] = [
                        f"(경고: {detected}개 조항 중 {reviewed}개만 검토됨)"
                    ]

            # v3: 결정론 Verify — basis 의 법령·판례·고시·자율규약 실존 대조
            # 실패 항목은 drop + warnings.dropped_basis 에 기록
            try:
                from .verify import verify_basis
                all_evidence_chunks: list[Any] = []
                for axis_chunks in chunks_by_axis.values():
                    all_evidence_chunks.extend(axis_chunks)
                result, verify_warnings = verify_basis(result, all_evidence_chunks)
                if verify_warnings:
                    LOGGER.info(
                        "Verify dropped %d basis items (grounded=%s)",
                        len(verify_warnings),
                        result.get("confidence", {}).get("grounded"),
                    )
            except Exception as exc:
                LOGGER.warning("verify_basis failed: %s", exc)

            return result

        except Exception as exc:
            LOGGER.warning("Document review failed: %s", exc)

        return _adapt_review_result({
            "document_overview": {"nature": "확인 실패"},
            "summary": "문서 검토 중 오류가 발생했습니다.",
            "observations": [],
            "gaps": [],
            "external_considerations": [],
            "unreviewed_clauses": [],
            "overall_risk": "n/a",
            "action_items": [],
            "disclaimer": "본 검토는 참고용이며 법률 자문이 아닙니다.",
        })

    # ── 오케스트레이션: 여러 파일 검토 ─────────────────────

    def review_documents_orchestrated(
        self,
        *,
        documents: list[dict[str, str]],
        question: str,
        chunks: list[RetrievedChunk],
    ) -> dict[str, Any]:
        """복수 파일 검토 오케스트레이션: 파일별 개별 검토(병렬) → PM 종합 검토.

        documents: [{"filename": str, "text": str}]
        - 파일 1개면 review_document로 직접 처리
        - 복수 파일이면 sibling_docs_hint로 서로 참조하며 병렬 검토 후 종합
        """
        if not self.client:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        # 파일 1개면 기존 방식
        if len(documents) == 1:
            return self.review_document(
                document_text=documents[0]["text"],
                question=question,
                chunks=chunks,
            )

        # Step 0: 전처리 — 파일별 조항 파싱 + 힌트 생성
        doc_contexts: list[dict[str, Any]] = []
        for doc in documents:
            clauses = self._parse_clauses(doc["text"])
            clause_titles = [f"제{cl['clause_no']}조 {cl['title']}" for cl in clauses]
            doc_contexts.append({
                "filename": doc["filename"],
                "text": doc["text"],
                "clauses": clauses,
                "clause_titles": clause_titles,
            })

        # 힌트: 각 파일의 조항 목록
        def build_hint(exclude_idx: int) -> str:
            lines = []
            for i, ctx in enumerate(doc_contexts):
                if i == exclude_idx:
                    continue
                titles = ", ".join(ctx["clause_titles"][:15])
                lines.append(f"- {ctx['filename']}: {titles}")
            return "\n".join(lines)

        # Step 1: 개별 검토 (병렬)
        from concurrent.futures import ThreadPoolExecutor, as_completed

        individual_results: list[dict[str, Any]] = [{}] * len(doc_contexts)

        def review_one(idx: int) -> tuple[int, dict[str, Any]]:
            ctx = doc_contexts[idx]
            hint = build_hint(idx)
            result = self.review_document(
                document_text=ctx["text"],
                question=question,
                chunks=chunks,
                _sibling_docs_hint=hint,
            )
            result["filename"] = ctx["filename"]
            return idx, result

        with ThreadPoolExecutor(max_workers=min(len(doc_contexts), CHAIN_PARALLEL_WORKERS)) as executor:
            futures = {executor.submit(review_one, i): i for i in range(len(doc_contexts))}
            for future in as_completed(futures):
                try:
                    idx, result = future.result()
                    individual_results[idx] = result
                except Exception as exc:
                    idx = futures[future]
                    LOGGER.warning("Individual review failed for %s: %s", doc_contexts[idx]["filename"], exc)
                    individual_results[idx] = {
                        "filename": doc_contexts[idx]["filename"],
                        "summary": "검토 실패",
                        "observations": [],
                        "clause_reviews": [],
                    }

        LOGGER.info("Orchestration Step 1 done: %d individual reviews", len(individual_results))

        # Step 2: PM 종합 검토
        try:
            comprehensive = self._review_comprehensive(individual_results, question)
        except Exception as exc:
            LOGGER.warning("Comprehensive review failed: %s", exc)
            # 종합 실패 시 개별 결과를 합쳐서 반환
            comprehensive = self._merge_individual_results(individual_results)

        LOGGER.info("Orchestration Step 2 done: comprehensive review")

        return comprehensive

    def _review_comprehensive(
        self,
        individual_results: list[dict[str, Any]],
        question: str,
    ) -> dict[str, Any]:
        """PM 종합 검토: 개별 파일 검토 결과를 요약·통합하여 최종 검토 보고서 생성.

        - 각 파일의 주요 리스크·누락 사항을 텍스트로 요약 후 LLM에 전달
        - 종합 실패 시 _merge_individual_results로 단순 합치기 폴백
        """
        # 개별 결과를 요약 텍스트로 변환
        summaries = []
        for r in individual_results:
            filename = r.get("filename", "unknown")
            overview = r.get("document_overview") or {}
            nature = overview.get("nature") or r.get("document_type") or "unknown"
            summary = r.get("summary", "")
            risk = r.get("overall_risk", "unknown")

            # 핵심 주의 지점 추출 (observations 기반)
            risks = []
            observations = r.get("observations") or r.get("clause_reviews") or []
            for o in observations:
                sev = o.get("severity") or o.get("risk_level")
                if sev not in ("high", "medium"):
                    continue
                locator = o.get("locator") or (f"제{o.get('clause_no')}조" if o.get("clause_no") else "?")
                issues = o.get("issues") or []
                if issues:
                    for it in issues:
                        if it.get("severity") in ("high", "medium"):
                            concern = it.get("concern") or it.get("issue") or ""
                            risks.append(f"  - {locator} [{it.get('severity')}]: {concern[:150]}")
                else:
                    concern = o.get("concern") or o.get("issue") or ""
                    risks.append(f"  - {locator} [{sev}]: {concern[:150]}")

            gaps = []
            for g in r.get("gaps") or [{"topic": m.get("clause"), "reason": m.get("reason")} for m in (r.get("missing_clauses") or [])]:
                topic = g.get("topic") or "?"
                reason = (g.get("reason") or "")[:100]
                gaps.append(f"  - {topic}: {reason}")

            section = f"### {filename} ({nature}, 리스크: {risk})\n{summary}\n"
            if risks:
                section += "주의 지점:\n" + "\n".join(risks) + "\n"
            if gaps:
                section += "부족·보완:\n" + "\n".join(gaps) + "\n"
            summaries.append(section)

        individual_summaries = "\n".join(summaries)

        prompt = _load_prompt("review_comprehensive.txt").format(
            individual_summaries=individual_summaries,
            question=question,
        )

        result = self._call_review(prompt)
        if not result:
            return self._merge_individual_results(individual_results)

        result = _adapt_review_result(result)

        # 개별 결과도 포함
        result["individual_reviews"] = individual_results
        return result

    @staticmethod
    def _merge_individual_results(individual_results: list[dict[str, Any]]) -> dict[str, Any]:
        """PM 종합 검토 실패 시 개별 결과를 단순 병합하여 폴백 반환.

        - 각 파일의 observations, gaps, externals 등을 하나의 리스트로 합침
        - overall_risk는 전체 observations 중 최고 severity로 결정
        - _source_file 필드를 추가하여 출처 파일 추적 가능
        """
        all_obs = []
        all_gaps = []
        all_externals = []
        all_actions = []
        all_key_points = []
        all_axes = []

        for r in individual_results:
            fname = r.get("filename", "")
            for o in r.get("observations", []) or r.get("clause_reviews", []):
                o = dict(o) if isinstance(o, dict) else o
                if isinstance(o, dict):
                    o["_source_file"] = fname
                    all_obs.append(o)
            all_gaps.extend(r.get("gaps", []) or [{"topic": m.get("clause"), "reason": m.get("reason"), "suggestion": m.get("suggestion")} for m in (r.get("missing_clauses") or []) if isinstance(m, dict)])
            all_externals.extend(r.get("external_considerations", []))
            for x in r.get("action_items", []):
                if x and x not in all_actions:
                    all_actions.append(x)
            for x in r.get("key_points", []) or r.get("key_negotiation_points", []):
                if x and x not in all_key_points:
                    all_key_points.append(x)
            for x in r.get("review_axes", []):
                if x and x not in all_axes:
                    all_axes.append(x)

        sev = [o.get("severity", "ok") for o in all_obs]
        overall = "high" if "high" in sev else "medium" if "medium" in sev else "low" if "low" in sev else "n/a"

        first_overview = next((r.get("document_overview") for r in individual_results if isinstance(r.get("document_overview"), dict)), {})

        merged = {
            "document_overview": first_overview or {},
            "summary": " / ".join(r.get("summary", "") for r in individual_results if r.get("summary")),
            "review_axes": all_axes,
            "observations": all_obs,
            "gaps": all_gaps,
            "external_considerations": all_externals,
            "overall_risk": overall,
            "key_points": all_key_points,
            "action_items": all_actions,
            "individual_reviews": individual_results,
            "disclaimer": "본 검토는 참고용이며 법률 자문이 아닙니다.",
        }
        return _adapt_review_result(merged)
