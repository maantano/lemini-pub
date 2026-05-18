"""행정규칙 DRF OpenAPI 클라이언트.

엔드포인트:
  lawSearch.do?target=admrul   행정규칙 목록
  lawService.do?target=admrul&ID={행정규칙일련번호}   상세

응답 구조 (JSON):
  AdmRulSearch.admrul[]      — 목록
  AdmRulService.행정규칙기본정보  — 메타
  AdmRulService.조문내용       — 문자열 (법령의 조문단위[] 과 달리 단일 텍스트)
  AdmRulService.부칙           — 문자열
  AdmRulService.첨부파일       — 첨부 파일 링크·이름

본문이 "자세한 내용은 상단 메뉴..." 같은 placeholder 인 경우 약 5% 존재.
수집 시 body_is_reference_only 플래그로 구분 — 차후 첨부 처리 대상.
"""

from __future__ import annotations

import json
import logging
import re
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

_throttle = Throttle(REQUEST_DELAY_SECONDS)

# 본문이 참조만 담고 있어 실제 내용은 첨부파일에 있을 때의 힌트
_REFERENCE_ONLY_RE = re.compile(
    r"(자세한\s*내용|상단\s*메뉴|첨부\s*파일을\s*참(고|조)|별첨|별도\s*파일)"
)


def _request(url: str, params: dict) -> requests.Response:
    return make_request(
        url, params,
        throttle=_throttle,
        api_key=LAW_API_KEY,
        max_retries=MAX_RETRIES,
        backoff_base=BACKOFF_BASE_SECONDS,
    )


def search_admrul(
    query: str = "",
    org: str = "",
    rule_type: str = "",
    page: int = 1,
    display: int = 100,
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """행정규칙 목록 검색.

    Args:
        query: 키워드 (규칙명)
        org: 소관부처코드 (예: '1270000' = 공정거래위원회)
        rule_type: 행정규칙종류코드 (1:훈령, 2:예규, 3:고시, 4:공고 등)
        date_from, date_to: 발령일자 범위 (YYYYMMDD)
    """
    params = {
        "target": "admrul",
        "type": "JSON",
        "query": query,
        "page": str(page),
        "display": str(display),
    }
    if org:
        params["org"] = org
    if rule_type:
        params["knd"] = rule_type
    if date_from and date_to:
        params["efYd"] = f"{date_from}~{date_to}"

    resp = _request(f"{LAW_API_BASE}/lawSearch.do", params)
    payload = resp.json()
    wrapper = payload.get("AdmRulSearch", {})

    total = int(wrapper.get("totalCnt", 0) or 0)
    page_num = int(wrapper.get("page", 1) or 1)

    items_raw = wrapper.get("admrul")
    if items_raw is None:
        items_raw = []
    elif isinstance(items_raw, dict):
        items_raw = [items_raw]

    rules = []
    for item in items_raw:
        rules.append({
            "행정규칙일련번호": str(item.get("행정규칙일련번호", "")),
            "행정규칙ID": str(item.get("행정규칙ID", "")),
            "행정규칙명": str(item.get("행정규칙명", "")),
            "행정규칙종류": str(item.get("행정규칙종류", "")),
            "행정규칙종류코드": str(item.get("행정규칙종류코드", "")),
            "소관부처명": str(item.get("소관부처명", "")),
            "소관부처코드": str(item.get("소관부처코드", "")),
            "발령일자": str(item.get("발령일자", "")),
            "발령번호": str(item.get("발령번호", "")),
            "시행일자": str(item.get("시행일자", "")),
            "제개정구분명": str(item.get("제개정구분명", "")),
            "현행연혁구분": str(item.get("현행연혁구분", "")),
            "행정규칙상세링크": str(item.get("행정규칙상세링크", "")),
        })

    return {"totalCnt": total, "page": page_num, "rules": rules}


def get_admrul_detail(rule_id: str | int) -> dict:
    """행정규칙 상세 조회.

    Returns:
        {
            'rule_id', 'rule_name', 'rule_type', 'rule_type_code',
            'org_name', 'org_code',
            'promulgation_date' (YYYYMMDD), 'effective_date',
            'promulgation_no', 'amendment_type',
            'body' (조문내용 — img 태그 제거됨),
            'addenda' (부칙),
            'reason' (제개정이유),
            'amendment_doc' (개정문),
            'body_is_reference_only' (bool) — 본문이 "자세한 내용은..." 형태인지,
            'attachments' [{'name', 'link'}],
            'raw_json'
        }
    """
    params = {
        "target": "admrul",
        "ID": str(rule_id),
        "type": "JSON",
    }

    resp = _request(f"{LAW_API_BASE}/lawService.do", params)
    raw = resp.content

    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise RuntimeError(f"Invalid JSON for admrul {rule_id}: {e}") from e

    if "AdmRulService" not in payload:
        msg = payload.get("Result", {}).get("msg") or payload.get("msg") or str(payload)[:200]
        raise RuntimeError(f"API error for admrul {rule_id}: {msg}")

    s = payload["AdmRulService"]
    info = s.get("행정규칙기본정보", {}) or {}

    body_raw = s.get("조문내용", "")
    # 배열/중첩 리스트 평문화 (조문별로 개행 구분)
    def _flatten(node) -> list[str]:
        out: list[str] = []
        if isinstance(node, list):
            for x in node:
                out.extend(_flatten(x))
        elif isinstance(node, str):
            if node:
                out.append(node)
        elif node is not None:
            out.append(str(node))
        return out

    if isinstance(body_raw, str):
        body_joined = body_raw
    else:
        body_joined = "\n\n".join(_flatten(body_raw))

    # <img> 태그 제거
    body_clean = re.sub(r"<img[^>]*>", "", body_joined).strip()
    body_is_ref_only = bool(_REFERENCE_ONLY_RE.search(body_clean)) and len(body_clean) < 400

    # 부칙 — 문자열 or dict
    buch = s.get("부칙", "")
    if isinstance(buch, dict):
        buch = json.dumps(buch, ensure_ascii=False)
    buch_clean = re.sub(r"<img[^>]*>", "", str(buch)).strip()

    # 첨부파일 정리
    attach = s.get("첨부파일", {}) or {}
    attachments = []
    names = attach.get("첨부파일명", "")
    links = attach.get("첨부파일링크", "")
    if isinstance(names, str) and isinstance(links, str) and names and links:
        # 한 개
        attachments.append({"name": names, "link": links})
    elif isinstance(names, list) and isinstance(links, list):
        for n, l in zip(names, links):
            attachments.append({"name": str(n), "link": str(l)})

    # 제개정이유
    reason = s.get("제개정이유", {}) or {}
    reason_text = ""
    if isinstance(reason, dict):
        rc = reason.get("제개정이유내용", "")
        if isinstance(rc, list):
            rc = " ".join(str(x) for x in rc if x)
        reason_text = re.sub(r"<img[^>]*>", "", str(rc)).strip()

    return {
        "rule_id": str(rule_id),
        "rule_name": str(info.get("행정규칙명", "")),
        "rule_type": str(info.get("행정규칙종류", "")),
        "rule_type_code": str(info.get("행정규칙종류코드", "")),
        "org_name": str(info.get("소관부처명", "")),
        "org_code": str(info.get("소관부처코드", "")),
        "promulgation_date": str(info.get("발령일자", "")),
        "effective_date": str(info.get("시행일자", "")),
        "promulgation_no": str(info.get("발령번호", "")),
        "amendment_type": str(info.get("제개정구분명", "")),
        "status": str(info.get("현행여부", "")),
        "body": body_clean,
        "addenda": buch_clean,
        "reason": reason_text,
        "body_is_reference_only": body_is_ref_only,
        "attachments": attachments,
        "raw_json": raw,
    }
