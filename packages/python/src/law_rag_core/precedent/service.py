from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable
from xml.etree import ElementTree

from ..logging import get_logger
from ..settings import get_settings
from ..types import (
    PrecedentDetailResponse,
    PrecedentDetailSection,
    PrecedentResult,
    PrecedentSearchResponse,
    PrecedentSource,
)

LOGGER = get_logger(__name__)

SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
DETAIL_URL = "https://www.law.go.kr/DRF/lawService.do"

_HOST_ACCESS_ERROR = (
    "국가법령정보 공동활용은 등록된 서버 IP/도메인에서만 호출할 수 있습니다. "
    "현재 서버 정보를 공동활용 신청 정보에 등록해 주세요."
)

# Keywords in judgment type / result text that indicate plaintiff win
_WIN_KEYWORDS = ("인용", "승소", "원고승", "일부인용", "일부승소")
_LOSS_KEYWORDS = ("기각", "패소", "각하")


@dataclass(frozen=True)
class DetailSectionSpec:
    label: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    label: str
    provider: str
    description: str
    target: str
    request_format: str
    default_enabled: bool
    id_fields: tuple[str, ...]
    title_fields: tuple[str, ...]
    number_fields: tuple[str, ...]
    issuer_fields: tuple[str, ...]
    date_fields: tuple[str, ...]
    status_fields: tuple[str, ...]
    link_fields: tuple[str, ...]
    detail_sections: tuple[DetailSectionSpec, ...]


SOURCE_SPECS: dict[str, SourceSpec] = {
    "prec": SourceSpec(
        source_id="prec",
        label="판례",
        provider="법제처 국가법령정보 공동활용",
        description="법원 판결문과 판결요지를 함께 조회합니다.",
        target="prec",
        request_format="JSON",
        default_enabled=True,
        id_fields=("판례일련번호", "판례정보일련번호", "precSeq", "ID"),
        title_fields=("사건명", "caseNm"),
        number_fields=("사건번호", "caseNo"),
        issuer_fields=("법원명", "courtNm"),
        date_fields=("선고일자", "jubDate"),
        status_fields=("판결유형", "judType", "선고", "사건종류명"),
        link_fields=("판례상세링크", "상세링크"),
        detail_sections=(
            DetailSectionSpec("판시사항", ("판시사항",)),
            DetailSectionSpec("판결요지", ("판결요지",)),
            DetailSectionSpec("참조조문", ("참조조문",)),
            DetailSectionSpec("참조판례", ("참조판례",)),
            DetailSectionSpec("판례내용", ("판례내용",)),
        ),
    ),
    "detc": SourceSpec(
        source_id="detc",
        label="헌재결정례",
        provider="법제처 국가법령정보 공동활용",
        description="헌법재판소 결정례를 함께 검색합니다.",
        target="detc",
        request_format="JSON",
        default_enabled=True,
        id_fields=("헌재결정례일련번호", "detcSeq", "ID"),
        title_fields=("사건명",),
        number_fields=("사건번호",),
        issuer_fields=("재판부", "헌법재판소"),
        date_fields=("종국일자", "선고일자"),
        status_fields=("결정유형", "사건종류명"),
        link_fields=("헌재결정례상세링크", "상세링크"),
        detail_sections=(
            DetailSectionSpec("판시사항", ("판시사항",)),
            DetailSectionSpec("결정요지", ("결정요지",)),
            DetailSectionSpec("참조조문", ("참조조문",)),
            DetailSectionSpec("참조판례", ("참조판례",)),
            DetailSectionSpec("심판대상조문", ("심판대상조문",)),
            DetailSectionSpec("전문", ("전문",)),
        ),
    ),
    "decc": SourceSpec(
        source_id="decc",
        label="행정심판례",
        provider="법제처 국가법령정보 공동활용",
        description="행정심판 재결례와 재결 요지를 함께 조회합니다.",
        target="decc",
        request_format="JSON",
        default_enabled=True,
        id_fields=("행정심판례일련번호", "deccSeq", "ID"),
        title_fields=("사건명",),
        number_fields=("사건번호",),
        issuer_fields=("재결청", "처분청"),
        date_fields=("의결일자", "처분일자"),
        status_fields=("재결례유형명", "재결유형"),
        link_fields=("행정심판례상세링크", "상세링크"),
        detail_sections=(
            DetailSectionSpec("주문", ("주문",)),
            DetailSectionSpec("청구취지", ("청구취지",)),
            DetailSectionSpec("재결요지", ("재결요지",)),
            DetailSectionSpec("이유", ("이유",)),
        ),
    ),
    "expc": SourceSpec(
        source_id="expc",
        label="법령해석례",
        provider="법제처 국가법령정보 공동활용",
        description="법령해석 회답과 이유를 함께 조회합니다.",
        target="expc",
        request_format="JSON",
        default_enabled=True,
        id_fields=("법령해석례일련번호", "expcSeq", "ID"),
        title_fields=("안건명",),
        number_fields=("안건번호",),
        issuer_fields=("회신기관명", "질의기관명", "해석기관명"),
        date_fields=("회신일자", "해석일자", "등록일자"),
        status_fields=("해석기관명",),
        link_fields=("법령해석례상세링크", "상세링크"),
        detail_sections=(
            DetailSectionSpec("질의요지", ("질의요지",)),
            DetailSectionSpec("회답", ("회답",)),
            DetailSectionSpec("이유", ("이유",)),
            DetailSectionSpec("해석대상법령", ("해석대상법령",)),
        ),
    ),
    "acr": SourceSpec(
        source_id="acr",
        label="국민권익위 결정문",
        provider="법제처 국가법령정보 공동활용",
        description="국민권익위원회 결정문을 조회합니다.",
        target="acr",
        request_format="XML",
        default_enabled=False,
        id_fields=("일련번호", "결정문일련번호", "ID"),
        title_fields=("제목", "민원표시"),
        number_fields=("의안번호",),
        issuer_fields=("기관명",),
        date_fields=("의결일", "의결일자"),
        status_fields=("결정구분", "회의종류"),
        link_fields=("결정문상세링크", "상세링크"),
        detail_sections=(
            DetailSectionSpec("주문", ("주문",)),
            DetailSectionSpec("결정요지", ("결정요지",)),
            DetailSectionSpec("이유", ("이유",)),
            DetailSectionSpec("의결문", ("의결문",)),
        ),
    ),
    "ftc": SourceSpec(
        source_id="ftc",
        label="공정위 결정문",
        provider="법제처 국가법령정보 공동활용",
        description="공정거래위원회 결정문을 조회합니다.",
        target="ftc",
        request_format="XML",
        default_enabled=False,
        id_fields=("일련번호", "결정문일련번호", "ID"),
        title_fields=("사건명",),
        number_fields=("사건번호", "결정번호"),
        issuer_fields=("기관명",),
        date_fields=("결정일자", "의결일자"),
        status_fields=("회의종류", "문서유형"),
        link_fields=("결정문상세링크", "상세링크"),
        detail_sections=(
            DetailSectionSpec("주문", ("주문",)),
            DetailSectionSpec("결정요지", ("결정요지",)),
            DetailSectionSpec("이유", ("이유",)),
            DetailSectionSpec("의결문", ("의결문",)),
        ),
    ),
    "ppc": SourceSpec(
        source_id="ppc",
        label="개인정보위 결정문",
        provider="법제처 국가법령정보 공동활용",
        description="개인정보보호위원회 결정문을 조회합니다.",
        target="ppc",
        request_format="XML",
        default_enabled=False,
        id_fields=("일련번호", "결정문일련번호", "ID"),
        title_fields=("안건명",),
        number_fields=("의안번호",),
        issuer_fields=("기관명",),
        date_fields=("의결연월일", "의결일자"),
        status_fields=("결정", "회의종류"),
        link_fields=("결정문상세링크", "상세링크"),
        detail_sections=(
            DetailSectionSpec("주문", ("주문",)),
            DetailSectionSpec("결정요지", ("결정요지",)),
            DetailSectionSpec("주요내용", ("주요내용",)),
            DetailSectionSpec("이유", ("이유",)),
        ),
    ),
    "sfc": SourceSpec(
        source_id="sfc",
        label="증권선물위 결정문",
        provider="법제처 국가법령정보 공동활용",
        description="증권선물위원회 결정문을 조회합니다.",
        target="sfc",
        request_format="JSON",
        default_enabled=False,
        id_fields=("일련번호", "결정문일련번호", "ID"),
        title_fields=("안건명",),
        number_fields=("의결번호",),
        issuer_fields=("기관명",),
        date_fields=("의결일자",),
        status_fields=("조치대상",),
        link_fields=("결정문상세링크", "상세링크"),
        detail_sections=(
            DetailSectionSpec("조치내용", ("조치내용",)),
            DetailSectionSpec("조치이유", ("조치이유",)),
        ),
    ),
    "fsc": SourceSpec(
        source_id="fsc",
        label="금융위 결정문",
        provider="법제처 국가법령정보 공동활용",
        description="금융위원회 결정문을 조회합니다.",
        target="fsc",
        request_format="JSON",
        default_enabled=False,
        id_fields=("일련번호", "결정문일련번호", "ID"),
        title_fields=("안건명",),
        number_fields=("의결번호",),
        issuer_fields=("기관명",),
        date_fields=("의결일자",),
        status_fields=("조치대상",),
        link_fields=("결정문상세링크", "상세링크"),
        detail_sections=(
            DetailSectionSpec("조치내용", ("조치내용",)),
            DetailSectionSpec("조치이유", ("조치이유",)),
        ),
    ),
}

DEFAULT_SOURCE_IDS = tuple(
    source_id
    for source_id, spec in SOURCE_SPECS.items()
    if spec.default_enabled
)


class HostAccessError(RuntimeError):
    """Raised when law.go.kr rejects the current host/IP registration."""


class PrecedentService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.api_key = self.settings.law_api_key or ""

    def list_sources(self) -> list[PrecedentSource]:
        return [
            PrecedentSource(
                source_id=spec.source_id,
                label=spec.label,
                provider=spec.provider,
                request_format=spec.request_format.lower(),
                default_enabled=spec.default_enabled,
                description=spec.description,
            )
            for spec in SOURCE_SPECS.values()
        ]

    def search(
        self,
        query: str,
        *,
        display: int = 5,
        sources: list[str] | None = None,
        body_search: bool = False,
    ) -> PrecedentSearchResponse:
        source_ids = self._normalize_source_ids(sources)
        if not query.strip():
            return PrecedentSearchResponse(
                query=query,
                results=[],
                total_count=0,
                win_rate=None,
            )

        results: list[PrecedentResult] = []
        errors: dict[str, str] = {}
        source_counts: dict[str, int] = {}

        for source_id in source_ids:
            spec = SOURCE_SPECS[source_id]
            try:
                payload = self._fetch_search(spec, query, display=display, body_search=body_search)
                parsed = self._parse_search_payload(spec, payload)
            except HostAccessError as exc:
                LOGGER.warning("Precedent host access blocked for source=%s: %s", source_id, exc)
                errors[source_id] = str(exc)
                source_counts[source_id] = 0
                continue
            except Exception as exc:
                LOGGER.warning("Precedent search failed for source=%s query=%r: %s", source_id, query, exc)
                errors[source_id] = "검색에 실패했습니다."
                source_counts[source_id] = 0
                continue

            source_counts[source_id] = len(parsed)
            results.extend(parsed[:display])

        win_rate = self._calc_win_rate(results)
        return PrecedentSearchResponse(
            query=query,
            results=results,
            total_count=sum(source_counts.values()),
            win_rate=win_rate,
            source_counts=source_counts,
            errors=errors,
        )

    def get_detail(self, source_id: str, case_id: str) -> PrecedentDetailResponse:
        spec = self._get_source_spec(source_id)

        # M4: precedent_doc_cache DB 우선 조회. 없으면 API 호출 + 저장
        cached = self._try_cache_detail(case_id, spec)
        if cached is not None:
            return cached

        payload = self._fetch_detail(spec, case_id)
        record = self._find_best_detail_record(payload, spec, case_id)
        result = self._record_to_result(record, spec)
        if result is None:
            result = self._fallback_detail_result(record, spec, case_id)

        sections = [
            PrecedentDetailSection(label=section.label, content=content)
            for section in spec.detail_sections
            if (content := self._get_joined_text(record, section.keys))
        ]

        if not sections and result.summary:
            sections = [PrecedentDetailSection(label="요약", content=result.summary)]

        # API 에서 방금 받은 상세를 cache 에 저장 (다음 호출부터 DB hit)
        self._cache_detail_async(case_id, record)

        return PrecedentDetailResponse(result=result, sections=sections)

    def _fallback_detail_result(self, record: dict[str, Any], spec: SourceSpec, case_id: str) -> PrecedentResult:
        """상세 응답에 목록형 필수 필드가 빠진 경우에도 최소 판례 객체를 구성한다."""
        raw_external_url = self._get_first_text(record, spec.link_fields)
        external_url = f"https://www.law.go.kr{raw_external_url}" if raw_external_url and raw_external_url.startswith("/") else raw_external_url
        internal_url = (
            f"/precedent?source={urllib.parse.quote(spec.source_id)}"
            f"&caseId={urllib.parse.quote(case_id)}"
        )
        summary = None
        for section in spec.detail_sections:
            summary = self._get_joined_text(record, section.keys)
            if summary:
                break

        return PrecedentResult(
            case_id=case_id,
            case_name=self._get_first_text(record, spec.title_fields) or "제목 미상",
            case_number=self._get_first_text(record, spec.number_fields) or "",
            court_name=self._get_first_text(record, spec.issuer_fields) or spec.label,
            source_id=spec.source_id,
            source_label=spec.label,
            provider=spec.provider,
            judgment_date=self._get_first_text(record, spec.date_fields),
            judgment_type=self._get_first_text(record, spec.status_fields),
            summary=summary,
            url=internal_url,
            external_url=external_url,
        )

    def _try_cache_detail(self, case_id: str, spec: SourceSpec) -> PrecedentDetailResponse | None:
        """precedent_doc_cache 에서 상세 복원. 없으면 None."""
        try:
            from ..db import get_law_db_connection
        except ImportError:
            return None
        try:
            with get_law_db_connection() as conn:
                row = conn.execute(
                    """
                    SELECT precedent_id, title, court, case_no, judgment_date, case_type,
                           body, holding, summary, referenced_statutes, referenced_cases,
                           judgment_type, source_url
                      FROM precedent_doc_cache WHERE precedent_id = ?
                    """,
                    (str(case_id),),
                ).fetchone()
        except Exception as e:  # 테이블 없거나 DB 에러 — 조용히 pass (API fallback)
            LOGGER.debug("precedent cache miss (db error): %s", e)
            return None

        if not row:
            return None

        (pid, title, court, case_no, judgment_date, case_type, body,
         holding, summary, ref_statutes, ref_cases, judgment_type, source_url) = row

        # PrecedentResult + sections 구성 (API 경로와 동일한 shape)
        result = PrecedentResult(
            case_id=str(pid),
            case_name=title or "",
            case_number=case_no or "",
            court_name=court or "",
            source_id=spec.source_id,
            source_label=spec.label,
            provider=spec.provider,
            judgment_date=self._format_date_value(judgment_date or ""),
            judgment_type=judgment_type or None,
            summary=(holding or summary or "") or None,
            external_url=source_url or f"https://www.law.go.kr/판례/({pid})",
        )

        sections: list[PrecedentDetailSection] = []
        if holding:
            sections.append(PrecedentDetailSection(label="판시사항", content=holding))
        if summary:
            sections.append(PrecedentDetailSection(label="판결요지", content=summary))
        if ref_statutes:
            sections.append(PrecedentDetailSection(label="참조조문", content=ref_statutes))
        if ref_cases:
            sections.append(PrecedentDetailSection(label="참조판례", content=ref_cases))
        if body:
            sections.append(PrecedentDetailSection(label="판례내용", content=body))

        LOGGER.info("precedent cache hit: id=%s", pid)
        return PrecedentDetailResponse(result=result, sections=sections)

    def _cache_detail_async(self, case_id: str, record: Any) -> None:
        """API 로 받은 record 를 precedent_doc_cache 에 저장 (실패는 swallow)."""
        try:
            from worker.sync.precedents import store as prec_store
        except ImportError:
            return
        try:
            detail = {
                "precedent_id": str(case_id),
                "case_no": self._get_joined_text(record, ("사건번호",)) or "",
                "case_name": self._get_joined_text(record, ("사건명",)) or "",
                "court": self._get_joined_text(record, ("법원명", "데이터출처명")) or "",
                "court_type_code": self._get_joined_text(record, ("법원종류코드",)) or "",
                "judgment_date": (self._get_joined_text(record, ("선고일자",)) or "").replace(".", "").replace("-", ""),
                "case_type": self._get_joined_text(record, ("사건종류명",)) or "",
                "case_type_code": self._get_joined_text(record, ("사건종류코드",)) or "",
                "judgment_type": self._get_joined_text(record, ("판결유형",)) or "",
                "selgo": self._get_joined_text(record, ("선고",)) or "",
                "holding": self._get_joined_text(record, ("판시사항",)) or "",
                "summary": self._get_joined_text(record, ("판결요지",)) or "",
                "referenced_statutes": self._get_joined_text(record, ("참조조문",)) or "",
                "referenced_cases": self._get_joined_text(record, ("참조판례",)) or "",
                "body": self._get_joined_text(record, ("판례내용",)) or "",
            }
            prec_store.upsert(detail, source="on-demand")
        except Exception as e:
            LOGGER.debug("precedent cache write skipped: %s", e)

    @staticmethod
    def _format_date_value(raw: str) -> str:
        """YYYYMMDD → YYYY-MM-DD. 이미 다른 포맷이면 그대로 반환."""
        raw = (raw or "").replace(".", "").replace("-", "")
        if len(raw) == 8 and raw.isdigit():
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
        return raw

    def _get_source_spec(self, source_id: str) -> SourceSpec:
        spec = SOURCE_SPECS.get(source_id)
        if spec is None:
            raise ValueError(f"Unsupported precedent source: {source_id}")
        return spec

    def _normalize_source_ids(self, sources: list[str] | None) -> list[str]:
        if not sources:
            return list(DEFAULT_SOURCE_IDS)

        normalized: list[str] = []
        for source_id in sources:
            source_id = source_id.strip()
            if source_id in SOURCE_SPECS and source_id not in normalized:
                normalized.append(source_id)

        return normalized or list(DEFAULT_SOURCE_IDS)

    def _fetch_search(
        self,
        spec: SourceSpec,
        query: str,
        *,
        display: int,
        body_search: bool,
    ) -> Any:
        params = {
            "OC": self.api_key,
            "target": spec.target,
            "query": query,
            "type": spec.request_format,
            "display": str(display),
        }
        if body_search:
            params["search"] = "2"
        return self._request_payload(SEARCH_URL, params, request_format=spec.request_format)

    def _fetch_detail(self, spec: SourceSpec, case_id: str) -> Any:
        params = {
            "OC": self.api_key,
            "target": spec.target,
            "ID": case_id,
            "type": spec.request_format,
        }
        return self._request_payload(DETAIL_URL, params, request_format=spec.request_format)

    def _request_payload(
        self,
        base_url: str,
        params: dict[str, str],
        *,
        request_format: str,
    ) -> Any:
        if not self.api_key:
            raise RuntimeError("LAW_API_KEY is not configured.")

        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json, application/xml;q=0.9"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")

        self._raise_if_host_access_denied(raw)

        if request_format.upper() == "JSON":
            return json.loads(raw)
        return self._xml_to_dict(ElementTree.fromstring(raw))

    def _raise_if_host_access_denied(self, raw: str) -> None:
        if "사용자 정보 검증에 실패하였습니다." in raw and "IP주소 및 도메인주소" in raw:
            raise HostAccessError(_HOST_ACCESS_ERROR)

    def _parse_search_payload(self, spec: SourceSpec, payload: Any) -> list[PrecedentResult]:
        records = self._collect_candidate_records(payload, spec)
        results: list[PrecedentResult] = []
        seen_ids: set[str] = set()

        for record in records:
            result = self._record_to_result(record, spec)
            if result is None or result.case_id in seen_ids:
                continue
            seen_ids.add(result.case_id)
            results.append(result)

        return results

    def _find_best_detail_record(self, payload: Any, spec: SourceSpec, case_id: str) -> dict[str, Any]:
        records = self._collect_candidate_records(payload, spec)
        for record in records:
            if self._get_first_text(record, spec.id_fields) == case_id:
                return record

        if records:
            return max(records, key=lambda item: len(item))

        if isinstance(payload, dict):
            return payload
        raise RuntimeError("상세 판례 응답을 해석하지 못했습니다.")

    def _record_to_result(self, record: dict[str, Any], spec: SourceSpec) -> PrecedentResult | None:
        case_id = self._get_first_text(record, spec.id_fields)
        case_name = self._get_first_text(record, spec.title_fields)
        if not case_id or not case_name:
            return None

        court_name = self._get_first_text(record, spec.issuer_fields) or spec.label
        judgment_type = self._get_first_text(record, spec.status_fields)
        raw_external_url = self._get_first_text(record, spec.link_fields)
        external_url = f"https://www.law.go.kr{raw_external_url}" if raw_external_url and raw_external_url.startswith("/") else raw_external_url
        internal_url = (
            f"/precedent?source={urllib.parse.quote(spec.source_id)}"
            f"&caseId={urllib.parse.quote(case_id)}"
        )

        summary = None
        for section in spec.detail_sections:
            summary = self._get_joined_text(record, section.keys)
            if summary:
                break

        return PrecedentResult(
            case_id=case_id,
            case_name=case_name,
            case_number=self._get_first_text(record, spec.number_fields) or "",
            court_name=court_name,
            source_id=spec.source_id,
            source_label=spec.label,
            provider=spec.provider,
            judgment_date=self._get_first_text(record, spec.date_fields),
            judgment_type=judgment_type,
            summary=summary,
            url=internal_url,
            external_url=external_url,
        )

    def _collect_candidate_records(self, payload: Any, spec: SourceSpec) -> list[dict[str, Any]]:
        objects = list(self._collect_objects(payload))
        candidates = [
            obj for obj in objects
            if self._get_first_text(obj, spec.id_fields)
        ]
        if candidates:
            return candidates
        return [
            obj for obj in objects
            if self._get_first_text(obj, spec.title_fields)
            and (
                self._get_first_text(obj, spec.number_fields)
                or self._get_first_text(obj, spec.date_fields)
            )
        ]

    def _collect_objects(self, value: Any) -> Iterable[dict[str, Any]]:
        if isinstance(value, list):
            for item in value:
                yield from self._collect_objects(item)
            return

        if not isinstance(value, dict):
            return

        yield value
        for child in value.values():
            yield from self._collect_objects(child)

    def _get_first_text(self, record: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        value = self._get_value(record, keys)
        return self._to_text(value)

    def _get_joined_text(self, record: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        value = self._get_value(record, keys)
        if value is None:
            return None
        return self._to_text(value, joiner="\n")

    def _get_value(self, record: dict[str, Any], keys: tuple[str, ...]) -> Any:
        normalized = {self._normalize_key(key): key for key in record.keys()}
        for key in keys:
            actual_key = normalized.get(self._normalize_key(key))
            if actual_key is not None:
                return record.get(actual_key)
        return None

    def _to_text(self, value: Any, *, joiner: str = " / ") -> str | None:
        if value is None:
            return None

        if isinstance(value, list):
            parts = [self._to_text(item, joiner=joiner) for item in value]
            joined = joiner.join(part for part in parts if part)
            return joined or None

        if isinstance(value, dict):
            if len(value) == 1 and "#text" in value:
                return self._to_text(value["#text"], joiner=joiner)
            parts = [self._to_text(item, joiner=joiner) for item in value.values()]
            joined = joiner.join(part for part in parts if part)
            return joined or None

        text = " ".join(str(value).split()).strip()
        return text or None

    def _normalize_key(self, key: str) -> str:
        return "".join(str(key).split()).strip().lower()

    def _xml_to_dict(self, element: ElementTree.Element) -> Any:
        children = list(element)
        if not children:
            text = " ".join((element.text or "").split()).strip()
            return text or None

        result: dict[str, Any] = {}
        for child in children:
            key = self._strip_namespace(child.tag)
            value = self._xml_to_dict(child)
            if value in (None, "", []):
                continue

            if key in result:
                existing = result[key]
                if not isinstance(existing, list):
                    result[key] = [existing]
                result[key].append(value)
                continue

            result[key] = value

        text = " ".join((element.text or "").split()).strip()
        if text:
            result["#text"] = text
        return result

    @staticmethod
    def _strip_namespace(tag: str) -> str:
        return tag.split("}", 1)[-1]

    @staticmethod
    def _calc_win_rate(results: list[PrecedentResult]) -> float | None:
        wins = 0
        determined = 0
        for result in results:
            judgment_type = (result.judgment_type or "").strip()
            if not judgment_type:
                continue
            if any(keyword in judgment_type for keyword in _WIN_KEYWORDS):
                wins += 1
                determined += 1
            elif any(keyword in judgment_type for keyword in _LOSS_KEYWORDS):
                determined += 1
        if determined == 0:
            return None
        return round(wins / determined, 2)
