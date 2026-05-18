from __future__ import annotations

import re

from ..ai import GeminiService
from ..db import get_law_db_connection, sqlite_rows_to_dicts
from ..normalization import normalize_for_search
from ..settings import get_settings
from ..types import RetrievedChunk
from ..vector_store import VectorStore


# 조문번호 패턴: "민법 제123조" 또는 "제45조의2" 형태를 매칭하는 정규식
LAW_ARTICLE_RE = re.compile(
    r"(?:(?P<law>[가-힣A-Za-z0-9\s]+?)\s*)?제\s*(?P<number>\d+조(?:의\d+)?)"
)
# 한국어 조사 목록 — 토큰에서 제거하여 어근만 추출할 때 사용
PARTICLE_SUFFIXES = (
    "으로",
    "까지",
    "부터",
    "에서",
    "에게",
    "한테",
    "보다",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "의",
    "도",
    "만",
    "과",
    "와",
    "로",
)


class RetrievalService:
    def __init__(self) -> None:
        """검색 서비스 초기화 — 설정, Gemini AI 서비스, 벡터 스토어를 로드한다."""
        self.settings = get_settings()
        self.gemini = GeminiService()
        self.vector_store = VectorStore()

    def retrieve(self, question: str) -> list[RetrievedChunk]:
        """메인 검색 진입점 — exact(조문번호) + lexical(FTS) + vector(임베딩) 검색을 병렬 실행한 뒤,
        LLM 키워드 확장 결과로 추가 검색하고, 최종적으로 중복 제거 및 점수 기반 정렬로 병합한다."""
        from concurrent.futures import ThreadPoolExecutor

        # 1단계: 기본 검색 3종 + LLM 의도 분석 + 쿼리 확장을 동시에 실행.
        # 작업 5개와 워커 수를 일치시켜 마지막 작업이 큐 대기로 wall-clock에
        # 한 사이클 추가되는 일을 막는다.
        with ThreadPoolExecutor(max_workers=5) as executor:
            exact_future = executor.submit(self._exact_match, question)
            lexical_future = executor.submit(self._lexical_match, question)
            vector_future = executor.submit(self._vector_match, question)
            intent_future = executor.submit(self.gemini.analyze_intent, question)
            expand_future = executor.submit(self.gemini.expand_query, question)

            exact_rows = exact_future.result()
            lexical_rows = lexical_future.result()
            vector_rows = vector_future.result()
            intent_result = intent_future.result()
            expanded_keywords = expand_future.result()

        # LLM이 추출한 의도 키워드 가져오기
        intent_keywords = intent_result.get("keywords", [])

        # 두 소스의 키워드 합치기 (중복 제거, 순서 유지)
        all_keywords = list(dict.fromkeys(intent_keywords + expanded_keywords))

        # 2단계: 확장된 키워드로 lexical + vector 추가 검색
        if all_keywords:
            expanded_query = " ".join(all_keywords)
            lexical_rows.extend(self._lexical_match(expanded_query))
            vector_rows.extend(self._vector_match(expanded_query))

        # 3단계: 모든 결과를 ID 기준으로 병합 — 같은 청크는 높은 점수를 유지
        merged: dict[str, RetrievedChunk] = {}
        for row in [*exact_rows, *lexical_rows, *vector_rows]:
            existing = merged.get(row.id)
            if existing is None or row.score > existing.score:
                merged[row.id] = row.model_copy(update={"source": "merged"})

        # 4단계: 점수 내림차순 정렬 → 최소 점수 필터 → top_k 개수 제한
        results = sorted(merged.values(), key=lambda item: item.score, reverse=True)
        filtered = [row for row in results if row.score >= self.settings.retrieval_min_score]

        # v3: Multi-Corpus 쿼터 보정 — 법령(statute)이 인덱스의 98.9%를 점유해
        # 자율규약·행정규칙이 top_k에서 밀리는 문제를 완화한다.
        # 도메인 힌트가 아니라 "사실 차원 다양성 보정": top_k 안에서 minority 코퍼스가
        # 최소 쿼터를 확보하도록 한다.
        top_k = self.settings.retrieval_top_k
        ADMRUL_MIN = 2
        VOLUNTARY_MIN = 2
        primary = filtered[:top_k]

        def _pick_minority(dtype: str, n: int) -> list[RetrievedChunk]:
            already = {r.id for r in primary}
            cand = [r for r in filtered if r.document_type == dtype and r.id not in already]
            return cand[:n]

        extras: list[RetrievedChunk] = []
        extras.extend(_pick_minority("administrative_rule", ADMRUL_MIN))
        extras.extend(_pick_minority("voluntary_code", VOLUNTARY_MIN))

        if extras:
            # 기존 primary 에서 statute 최하위를 extras 수만큼 drop 하여 총 개수 유지
            statute_idx = [i for i, r in enumerate(primary) if r.document_type == "statute"]
            drop_count = min(len(extras), max(0, len(statute_idx) - 3))  # statute 최소 3개 보존
            if drop_count > 0:
                to_drop = set(statute_idx[-drop_count:])
                primary = [r for i, r in enumerate(primary) if i not in to_drop]
            primary = primary + extras[:drop_count if drop_count > 0 else len(extras)]

        return primary[:top_k]

    def _exact_match(self, question: str) -> list[RetrievedChunk]:
        """조문번호 정규식 매칭 — 질문에서 '제N조' 패턴을 찾아 해당 조문을 고정 점수(60.0)로 반환한다.
        법명 힌트가 있으면 law_aliases 테이블까지 참조하여 정확한 법령을 특정한다."""
        # 질문에서 조문번호 정규식 매칭 시도
        match = LAW_ARTICLE_RE.search(question)
        article_no = match.group("number").replace(" ", "") if match else None
        law_hint = normalize_for_search(match.group("law")) if match and match.group("law") else None

        # 조문번호를 찾지 못하면 빈 결과 반환
        if not article_no:
            return []

        # WHERE 조건 동적 구성: 조문번호 일치 + article 타입 필터
        where_clause = ["lc.article_no = ?"]
        params: list[object] = [article_no]
        if article_no:
            where_clause.append("lc.chunk_type = 'article'")
        # 법명 힌트가 있으면 title 또는 alias 매칭 조건 추가
        normalized_law_hint = law_hint.replace(" ", "") if law_hint else None
        if normalized_law_hint:
            where_clause.append(
                """
                (
                  ld.title_normalized = ?
                  OR EXISTS (
                    SELECT 1
                    FROM law_aliases la
                    WHERE la.law_id = ld.law_id
                      AND la.alias_normalized = ?
                  )
                )
                """
            )
            params.extend([normalized_law_hint, normalized_law_hint])

        query = f"""
            SELECT
              lc.id,
              lc.document_id,
              ld.law_id,
              ld.title AS law_title,
              ld.document_type AS document_type,
              ld.issuer AS issuer,
              ld.promulgation_no AS promulgation_no,
              lc.chunk_type,
              lc.chapter_title,
              lc.section_title,
              lc.article_no,
              lc.article_title,
              lc.text,
              '' AS search_text,
              60.0 AS score,
              'exact' AS source
            FROM law_chunks lc
            JOIN law_documents ld ON ld.id = lc.document_id
            WHERE {' AND '.join(where_clause)}
            ORDER BY lc.order_index
            LIMIT ?
        """
        params.append(self.settings.retrieval_top_k)
        return self._run_query(query, params)

    def _lexical_match(self, question: str) -> list[RetrievedChunk]:
        """FTS5 전문 검색 — 질문을 토큰화한 뒤 BM25 점수로 후보를 가져오고,
        _score_lexical_candidate로 제목/본문 매칭 가중치를 더해 최종 점수를 산출한다."""
        # 질문을 토큰화하여 검색 키워드 추출
        terms, base_terms = self._build_query_terms(question)
        if not terms:
            return []
        # FTS5 MATCH 쿼리 구성 (최대 12개 토큰, OR 연결)
        fts_query = " OR ".join(f'"{token}"' for token in terms[:12])
        query = """
            SELECT
              lc.id,
              lc.document_id,
              ld.law_id,
              ld.title AS law_title,
              ld.document_type AS document_type,
              ld.issuer AS issuer,
              ld.promulgation_no AS promulgation_no,
              lc.chunk_type,
              lc.chapter_title,
              lc.section_title,
              lc.article_no,
              lc.article_title,
              lc.text,
              law_search_fts.search_text AS search_text,
              (-bm25(law_search_fts)) AS score,
              'lexical' AS source
            FROM law_search_fts
            JOIN law_chunks lc ON lc.id = law_search_fts.chunk_id
            JOIN law_documents ld ON ld.id = lc.document_id
            WHERE law_search_fts MATCH ?
            ORDER BY bm25(law_search_fts)
            LIMIT ?
        """
        # top_k의 12배를 가져와 충분한 후보 풀 확보
        rows = self._run_query(query, [fts_query, self.settings.retrieval_top_k * 12])
        # 각 후보에 대해 키워드 매칭 기반 가중치 점수 재계산
        boosted: list[RetrievedChunk] = []
        for row in rows:
            boosted.append(
                row.model_copy(
                    update={
                        "score": self._score_lexical_candidate(
                            row,
                            base_terms=base_terms,
                            expanded_terms=terms,
                        )
                    }
                )
            )
        return sorted(boosted, key=lambda item: item.score, reverse=True)

    def _vector_match(self, question: str) -> list[RetrievedChunk]:
        """벡터 검색 — Gemini로 질문을 임베딩한 뒤, VectorStore에서 cosine similarity 기반으로
        가장 유사한 청크를 찾아 반환한다."""
        # 질문 텍스트를 Gemini 임베딩 벡터로 변환
        embedding = self.gemini.embed_text(question)
        if not embedding:
            return []
        # 벡터 스토어에서 cosine similarity 상위 후보 검색
        matches = self.vector_store.search(embedding, limit=self.settings.retrieval_top_k * 2)
        if not matches:
            return []
        # 유사도 점수 매핑 후, DB에서 청크 상세 정보 조회
        score_map = {chunk_id: score for chunk_id, score in matches}
        chunks = self._fetch_chunks_by_ids([chunk_id for chunk_id, _score in matches])
        # 각 청크에 벡터 유사도 점수를 할당하고 점수 내림차순 정렬
        results: list[RetrievedChunk] = []
        for chunk in chunks:
            results.append(
                chunk.model_copy(
                    update={
                        "score": score_map[chunk.id],
                        "source": "vector",
                    }
                )
            )
        return sorted(results, key=lambda item: item.score, reverse=True)

    def _run_query(self, query: str, params: object) -> list[RetrievedChunk]:
        """SQL 쿼리 실행 유틸리티 — 쿼리 결과를 dict 리스트로 변환한 뒤 RetrievedChunk 모델로 검증/반환한다."""
        with get_law_db_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            cursor.close()
        return [RetrievedChunk.model_validate(row) for row in sqlite_rows_to_dicts(rows)]

    def _fetch_chunks_by_ids(self, ids: list[str]) -> list[RetrievedChunk]:
        """ID 목록으로 청크 일괄 조회 — 벡터 검색 결과의 상세 정보를 DB에서 가져올 때 사용한다.
        입력 순서를 보존하여 반환한다."""
        if not ids:
            return []
        # IN 절용 플레이스홀더 동적 생성
        placeholders = ", ".join("?" for _ in ids)
        query = f"""
            SELECT
              lc.id,
              lc.document_id,
              ld.law_id,
              ld.title AS law_title,
              ld.document_type AS document_type,
              ld.issuer AS issuer,
              ld.promulgation_no AS promulgation_no,
              lc.chunk_type,
              lc.chapter_title,
              lc.section_title,
              lc.article_no,
              lc.article_title,
              lc.text,
              '' AS search_text,
              0.0 AS score,
              'vector' AS source
            FROM law_chunks lc
            JOIN law_documents ld ON ld.id = lc.document_id
            WHERE lc.id IN ({placeholders})
        """
        rows = self._run_query(query, ids)
        # 원래 ID 순서대로 정렬하여 벡터 유사도 순서 보존
        order_map = {chunk_id: index for index, chunk_id in enumerate(ids)}
        return sorted(rows, key=lambda item: order_map[item.id])

    def _build_query_terms(self, question: str) -> tuple[list[str], list[str]]:
        """질문 토큰화 + 한국어 조사 제거 — 질문을 정규화한 뒤 공백 분리하고,
        각 토큰의 원형과 조사 제거 형태를 모두 수집하여 FTS 검색 키워드로 반환한다.
        (base_terms, base_terms) 동일한 값을 두 번 반환 — expanded_terms는 별도 확장 없음."""
        # 질문 텍스트 정규화 (소문자, 특수문자 제거 등)
        normalized = normalize_for_search(question)
        raw_tokens = [token for token in normalized.split() if token]
        base_terms: list[str] = []
        seen_base: set[str] = set()

        # 각 토큰에 대해 원형 + 조사 제거 형태를 중복 없이 수집
        for token in raw_tokens:
            for candidate in (token, self._strip_particle(token)):
                # 2글자 미만이거나 이미 수집된 토큰은 건너뜀
                if len(candidate) < 2 or candidate in seen_base:
                    continue
                seen_base.add(candidate)
                base_terms.append(candidate)

        return base_terms, base_terms

    @staticmethod
    def _strip_particle(token: str) -> str:
        """한국어 조사 제거 — 토큰 끝에 붙은 조사(은/는/이/가 등)를 제거하여 어근을 추출한다.
        어근이 2글자 미만이 되면 제거하지 않고 원본을 반환한다."""
        for suffix in PARTICLE_SUFFIXES:
            if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                return token[: -len(suffix)]
        return token

    @staticmethod
    def _score_lexical_candidate(
        row: RetrievedChunk,
        *,
        base_terms: list[str],
        expanded_terms: list[str],
    ) -> float:
        """FTS 후보 점수 계산 — BM25 기본 점수에 키워드 매칭 가중치를 더한다.
        본문(+0.9), 조문 제목(+0.6), 법령 제목(+0.35) 순으로 가중치를 부여하고,
        확장 키워드는 기본 키워드보다 낮은 가중치(+0.35/+0.25)를 적용한다.
        article 타입 청크에는 소폭 보너스(+0.15)를 추가한다."""
        # 비교용 정규화 텍스트 준비
        article_title = normalize_for_search(row.article_title or "")
        law_title = normalize_for_search(row.law_title)
        # 법령명 + 조문번호 + 조문제목 + 검색텍스트 + 본문을 합쳐 haystack 구성
        haystack = normalize_for_search(
            " ".join(
                part
                for part in (
                    row.law_title,
                    row.article_no or "",
                    row.article_title or "",
                    row.search_text or "",
                    row.text,
                )
                if part
            )
        )

        # BM25 점수를 기본값으로 사용 (음수 방지)
        score = max(row.score, 0.0)

        # 기본 키워드 매칭 — 본문/조문제목/법령제목에서 발견 시 가산점
        for term in base_terms[:10]:
            if term in haystack:
                score += 0.9
            if term in article_title:
                score += 0.6
            if term in law_title:
                score += 0.35

        # 확장 키워드 매칭 — 기본 키워드와 중복되지 않는 것만 낮은 가중치로 가산
        base_term_set = set(base_terms)
        for term in expanded_terms[:14]:
            if term in haystack and term not in base_term_set:
                score += 0.35
            if term in article_title and term not in base_term_set:
                score += 0.25

        # 조문(article) 타입 청크에 소폭 보너스
        if row.chunk_type == "article":
            score += 0.15

        return score
