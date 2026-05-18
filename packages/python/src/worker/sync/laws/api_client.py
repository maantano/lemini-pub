"""Thin wrapper around law.go.kr OpenAPI (JSON 기반).

Adapted from legalize-pipeline (MIT/Apache-2.0). See sync/NOTICES.md.

법제처 DRF OpenAPI의 ``type=JSON`` 응답을 파싱한다. 원본 legalize-pipeline은
XML 파싱 기준이었으나, 우리 OC 키의 실제 응답 특성과 에러 안정성 때문에
JSON 으로 전환했다. ``converter.law_to_markdown`` 이 기대하는 내부 데이터
구조(``metadata`` + ``articles`` + ``addenda``)는 그대로 유지한다.

JSON 응답 특성 메모:
- ``조문단위[].항``, ``항[].호``, ``호[].목``: ``None`` | ``dict`` | ``list`` 세 가지 모두 출현.
  아래 ``_as_list`` 로 일괄 정규화한다.
- ``기본정보.소관부처``, ``기본정보.법종구분`` 등: ``{"content": "...", "...코드": "..."}``
  형태. ``_content`` 로 추출.
- ``부칙단위[].부칙내용``: 2차원 배열 (문자열 행렬). ``_flatten_lines`` 로 평문화.
- ``lsHistory`` 엔드포인트: JSON 미지원 → HTML 정규식 파싱 유지.
"""

import logging
import re
from typing import Any

import requests

from . import cache
from .config import (
    BACKOFF_BASE_SECONDS,
    LAW_API_BASE,
    LAW_API_KEY,
    MAX_RETRIES,
    REQUEST_DELAY_SECONDS,
)
from ..core.http import make_request
from ..core.throttle import Throttle

logger = logging.getLogger(__name__)

_throttle = Throttle(REQUEST_DELAY_SECONDS)


def _request(url: str, params: dict) -> requests.Response:
    """Make a throttled request with retry and exponential backoff."""
    return make_request(
        url, params,
        throttle=_throttle,
        api_key=LAW_API_KEY,
        max_retries=MAX_RETRIES,
        backoff_base=BACKOFF_BASE_SECONDS,
    )


# ──────────────────────────────────────────────────────────────────────────
# 공통 정규화 유틸
# ──────────────────────────────────────────────────────────────────────────

def _as_list(value: Any) -> list:
    """``None`` | ``dict`` | ``list`` 를 일관되게 ``list`` 로 정규화."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _content(value: Any) -> str:
    """``{"content": "..."} `` 또는 문자열에서 실제 텍스트만 추출."""
    if isinstance(value, dict):
        return str(value.get("content", ""))
    if value is None:
        return ""
    return str(value)


def _flatten_lines(value: Any) -> str:
    """2차원/1차원 배열 또는 문자열을 줄바꿈으로 평문화.

    ``부칙내용``, ``개정문내용`` 이 ``[[...]]`` 또는 ``[...]`` 로 돌아오는데
    자바스크립트 기반 원본 API의 특성이라 정형화가 불가. 깊이 상관없이
    문자열만 모아 join한다.
    """
    lines: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            if node:
                lines.append(node)
        elif node is None:
            return
        else:
            lines.append(str(node))

    walk(value)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────

def search_laws(
    query: str = "",
    page: int = 1,
    display: int = 20,
    sort: str = "lasc",
    law_type: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Search laws via the search API (JSON).

    Returns dict with keys: totalCnt, page, laws (list of law metadata dicts).
    각 law metadata 는 기존 XML 파서와 동일한 키 세트를 유지한다.
    """
    params = {
        "target": "law",
        "type": "JSON",
        "query": query,
        "page": str(page),
        "display": str(display),
        "sort": sort,
    }
    if law_type:
        params["knd"] = law_type
    if date_from and date_to:
        params["ancYd"] = f"{date_from}~{date_to}"

    resp = _request(f"{LAW_API_BASE}/lawSearch.do", params)
    payload = resp.json()
    wrapper = payload.get("LawSearch", {})

    total = int(wrapper.get("totalCnt", 0) or 0)
    page_num = int(wrapper.get("page", 1) or 1)

    laws = []
    for item in _as_list(wrapper.get("law")):
        laws.append({
            "법령일련번호": str(item.get("법령일련번호", "")),
            "현행연혁코드": str(item.get("현행연혁코드", "")),
            "법령명한글": str(item.get("법령명한글", "")),
            "법령약칭명": str(item.get("법령약칭명", "")),
            "법령ID": str(item.get("법령ID", "")),
            "공포일자": str(item.get("공포일자", "")),
            "공포번호": str(item.get("공포번호", "")),
            # JSON 응답은 `법령구분명` 을 사용 (XML 은 `법령구분`)
            "제개정구분명": str(item.get("제개정구분명", "")),
            "소관부처명": str(item.get("소관부처명", "")),
            "시행일자": str(item.get("시행일자", "")),
            "법령상세링크": str(item.get("법령상세링크", "")),
            "법령구분": str(item.get("법령구분명", "")),  # reverse_index 에서 사용
        })

    return {"totalCnt": total, "page": page_num, "laws": laws}


def _normalize_article(jo: dict) -> dict:
    """JSON 조문단위 → converter 가 기대하는 형태(항→호→목 배열)로 변환."""
    paragraphs_out: list[dict] = []

    # 조문 단위 자체가 편/장/절/관 헤더인 경우도 있음 (조문여부 != '조문')
    # 이 경우도 converter 쪽에서 content 기반으로 헤딩 처리하므로 그대로 전달
    for hang in _as_list(jo.get("항")):
        if not isinstance(hang, dict):
            continue
        subparas_out: list[dict] = []
        for ho in _as_list(hang.get("호")):
            if not isinstance(ho, dict):
                continue
            items_out: list[dict] = []
            for mok in _as_list(ho.get("목")):
                if not isinstance(mok, dict):
                    continue
                items_out.append({
                    "목번호": str(mok.get("목번호", "")),
                    "목내용": str(mok.get("목내용", "")),
                })
            subparas_out.append({
                "호번호": str(ho.get("호번호", "")),
                "호내용": str(ho.get("호내용", "")),
                "목": items_out,
            })
        paragraphs_out.append({
            "항번호": str(hang.get("항번호", "")),
            "항내용": str(hang.get("항내용", "")),
            "호": subparas_out,
        })

    return {
        "조문번호": str(jo.get("조문번호", "")),
        "조문제목": str(jo.get("조문제목", "")),
        "조문내용": str(jo.get("조문내용", "")),
        "항": paragraphs_out,
    }


def get_law_detail(mst_id: str | int) -> dict:
    """Fetch full law text and metadata by MST ID (JSON).

    Returns dict with ``metadata`` + ``articles`` + ``addenda`` (converter 호환).
    """
    params = {
        "target": "law",
        "MST": str(mst_id),
        "type": "JSON",
    }

    cached = cache.get_detail(str(mst_id))
    if cached:
        logger.debug(f"Cache hit: detail MST={mst_id}")
        raw = cached
    else:
        resp = _request(f"{LAW_API_BASE}/lawService.do", params)
        raw = resp.content

    try:
        import json as _json
        payload = _json.loads(raw)
    except ValueError as e:
        raise RuntimeError(f"Invalid JSON for MST {mst_id}: {e}") from e

    # API 에러 응답은 ``{"Result": {"code": "...", "msg": "..."}}`` 또는
    # top-level 에 "result" 필드가 있을 수 있음. 관용적으로 체크.
    if "법령" not in payload:
        msg = payload.get("Result", {}).get("msg") or payload.get("msg") or str(payload)[:200]
        raise RuntimeError(f"API error for MST {mst_id}: {msg}")

    law = payload["법령"]
    info = law.get("기본정보", {}) or {}

    metadata = {
        "법령명한글": str(info.get("법령명_한글", "")),
        "법령MST": str(mst_id),
        "법령ID": str(info.get("법령ID", "")),
        "법령구분": _content(info.get("법종구분")),
        "법령구분코드": (info.get("법종구분", {}) or {}).get("법종구분코드", "") if isinstance(info.get("법종구분"), dict) else "",
        "소관부처명": _content(info.get("소관부처")),
        "소관부처코드": (info.get("소관부처", {}) or {}).get("소관부처코드", "") if isinstance(info.get("소관부처"), dict) else "",
        "공포일자": str(info.get("공포일자", "")),
        "공포번호": str(info.get("공포번호", "")),
        "시행일자": str(info.get("시행일자", "")),
        "제개정구분": str(info.get("제개정구분", "")),
        "법령분야": "",  # JSON 응답에 해당 필드 없음 — 비워둠
    }

    articles = [_normalize_article(jo) for jo in _as_list((law.get("조문") or {}).get("조문단위"))]

    addenda_raw = _as_list((law.get("부칙") or {}).get("부칙단위"))
    addenda = []
    for bu in addenda_raw:
        if not isinstance(bu, dict):
            continue
        addenda.append({
            "부칙공포일자": str(bu.get("부칙공포일자", "")),
            "부칙공포번호": str(bu.get("부칙공포번호", "")),
            "부칙내용": _flatten_lines(bu.get("부칙내용", "")),
        })

    # Cache raw JSON after successful parse
    if not cached:
        cache.put_detail(str(mst_id), raw)

    return {
        "metadata": metadata,
        "articles": articles,
        "addenda": addenda,
        "raw_json": raw,
    }


def _parse_dot_date(raw: str) -> str:
    """Parse dot-separated date like '1958.2.22' into 'YYYYMMDD' format."""
    raw = raw.strip()
    if not raw:
        return ""
    parts = raw.split(".")
    if len(parts) == 3:
        return f"{parts[0]}{int(parts[1]):02d}{int(parts[2]):02d}"
    return raw.replace(".", "")


def get_law_history(law_name: str) -> list[dict]:
    """Fetch amendment history for a law via lsHistory HTML endpoint.

    lsHistory 는 JSON 을 지원하지 않으므로 HTML 테이블을 정규식 파싱한다.
    (원본 legalize-pipeline 의 방식 그대로)
    """
    cached = cache.get_history(law_name)
    if cached:
        logger.debug(f"Cache hit: history law_name={law_name}")
        return cached
    if cached == []:
        logger.info("rewriting poisoned empty cache for %s", law_name)

    all_entries: list[dict] = []
    page = 1

    while True:
        resp = _request(f"{LAW_API_BASE}/lawSearch.do", {
            "target": "lsHistory",
            "query": law_name,
            "type": "HTML",
            "display": "100",
            "page": str(page),
        })

        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", resp.text, re.DOTALL)
        for row in rows:
            mst_match = re.search(r"MST=(\d+)", row)
            if not mst_match:
                continue
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            if len(tds) < 8:
                continue
            clean = [re.sub(r"<[^>]+>", "", td).strip() for td in tds]
            name = clean[1]
            if name != law_name:
                continue
            prom_date = _parse_dot_date(clean[6])
            enf_date = _parse_dot_date(clean[7])
            all_entries.append({
                "법령일련번호": mst_match.group(1),
                "법령명한글": name,
                "제개정구분명": clean[3],
                "법령구분": clean[4],
                "공포번호": clean[5].replace("제 ", "").replace("호", "").strip(),
                "공포일자": prom_date,
                "시행일자": enf_date,
            })

        if len(rows) < 10:
            break
        page += 1

    all_entries.sort(key=lambda x: x["공포일자"])
    cache.put_history(law_name, all_entries)
    return all_entries
