"""판례 DRF OpenAPI 클라이언트 (JSON 기반).

Adapted from legalize-pipeline (MIT/Apache-2.0). See sync/NOTICES.md.

엔드포인트:
  lawSearch.do?target=prec  판례 목록 검색
  lawService.do?target=prec&ID={판례일련번호}  판례 상세

응답 구조 (JSON):
  PrecSearch.prec[]       — 검색 결과 배열
  PrecService             — 상세 (판시사항·판결요지·판례내용 등)

변환 규칙:
  DRF 응답은 HTML entity (<br/>) 를 포함하지만 그대로 유지 — 사용 측(PrecedentService)이
  rendering 단계에서 다듬는다. 여기서는 저장 시점에 text 로만 보관.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from ..core.http import make_request
from ..core.throttle import Throttle
from ..laws.config import (
    BACKOFF_BASE_SECONDS,
    LAW_API_BASE,
    LAW_API_KEY,
    MAX_RETRIES,
    REQUEST_DELAY_SECONDS,
)

logger = logging.getLogger(__name__)

# 법령용 throttle 과 독립된 버킷 (동시 실행 시 안전)
_throttle = Throttle(REQUEST_DELAY_SECONDS)


def _request(url: str, params: dict) -> requests.Response:
    return make_request(
        url, params,
        throttle=_throttle,
        api_key=LAW_API_KEY,
        max_retries=MAX_RETRIES,
        backoff_base=BACKOFF_BASE_SECONDS,
    )


def search_precedents(
    query: str = "",
    court: str = "",
    case_type: str = "",
    page: int = 1,
    display: int = 100,
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """판례 목록 검색.

    Args:
        query: 키워드 (사건명·본문)
        court: 법원 필터 (예: '대법원', '서울고등법원')
        case_type: 사건종류 (민사/형사/가사/행정/특허/일반행정)
        page, display: 페이지네이션
        date_from, date_to: 선고일자 범위 (YYYYMMDD)

    Returns:
        {totalCnt: int, page: int, precedents: [ {...} ] }
    """
    params = {
        "target": "prec",
        "type": "JSON",
        "query": query,
        "page": str(page),
        "display": str(display),
    }
    if court:
        params["curt"] = court
    if case_type:
        params["gana"] = case_type  # 법제처 DRF 가이드 기준
    if date_from and date_to:
        params["prncYd"] = f"{date_from}~{date_to}"

    resp = _request(f"{LAW_API_BASE}/lawSearch.do", params)
    payload = resp.json()
    wrapper = payload.get("PrecSearch", {})

    total = int(wrapper.get("totalCnt", 0) or 0)
    page_num = int(wrapper.get("page", 1) or 1)

    items_raw = wrapper.get("prec")
    if items_raw is None:
        items_raw = []
    elif isinstance(items_raw, dict):
        items_raw = [items_raw]

    precedents = []
    for item in items_raw:
        precedents.append({
            "판례일련번호": str(item.get("판례일련번호", "")),
            "사건번호": str(item.get("사건번호", "")),
            "사건명": str(item.get("사건명", "")),
            "법원명": str(item.get("법원명", "")),
            "법원종류코드": str(item.get("법원종류코드", "")),
            "선고일자": str(item.get("선고일자", "")),
            "선고": str(item.get("선고", "")),
            "사건종류명": str(item.get("사건종류명", "")),
            "사건종류코드": str(item.get("사건종류코드", "")),
            "판결유형": str(item.get("판결유형", "")),
            "판례상세링크": str(item.get("판례상세링크", "")),
            "데이터출처명": str(item.get("데이터출처명", "")),
        })

    return {"totalCnt": total, "page": page_num, "precedents": precedents}


def get_precedent_detail(precedent_id: str | int) -> dict:
    """판례 상세 조회.

    Returns:
        {
            'precedent_id', 'case_no', 'case_name', 'court', 'court_type_code',
            'judgment_date' (YYYYMMDD), 'case_type', 'case_type_code',
            'judgment_type', 'selgo' (선고/판결),
            'holding' (판시사항), 'summary' (판결요지),
            'referenced_statutes' (참조조문), 'referenced_cases' (참조판례),
            'body' (판례내용 원문), 'raw_json' (원본 바이트)
        }
    """
    params = {
        "target": "prec",
        "ID": str(precedent_id),
        "type": "JSON",
    }

    resp = _request(f"{LAW_API_BASE}/lawService.do", params)
    raw = resp.content

    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise RuntimeError(f"Invalid JSON for precedent {precedent_id}: {e}") from e

    if "PrecService" not in payload:
        msg = payload.get("Result", {}).get("msg") or payload.get("msg") or str(payload)[:200]
        raise RuntimeError(f"API error for precedent {precedent_id}: {msg}")

    p = payload["PrecService"]

    def _s(key: str) -> str:
        v = p.get(key)
        return str(v) if v is not None else ""

    return {
        "precedent_id": str(precedent_id),
        "case_no": _s("사건번호"),
        "case_name": _s("사건명"),
        "court": _s("법원명"),
        "court_type_code": _s("법원종류코드"),
        "judgment_date": _s("선고일자"),
        "case_type": _s("사건종류명"),
        "case_type_code": _s("사건종류코드"),
        "judgment_type": _s("판결유형"),
        "selgo": _s("선고"),
        "holding": _s("판시사항"),
        "summary": _s("판결요지"),
        "referenced_statutes": _s("참조조문"),
        "referenced_cases": _s("참조판례"),
        "body": _s("판례내용"),
        "raw_json": raw,
    }
