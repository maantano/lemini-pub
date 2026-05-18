"""판례 수집기.

두 가지 모드:
  bulk      최근 N 년 대법원 판례 초기 수집 (수동 실행, 예: --years 10)
  recent    최근 N 일 대법원 신규 판례 증분 (주간 자동 실행, 기본 30 일)

DRF API 쿼터 고려:
  개발계정 일 1,000건 — 5천 건 수집은 5일 분할 권장.
  각 일자에 `--max-count 900` 으로 제한 가능.

저장:
  precedent_doc_cache 테이블에 upsert (content_hash 기반 변경 감지).
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta

from ..laws.config import LAW_API_KEY
from . import api_client, store

logger = logging.getLogger(__name__)


def _iterate_search(
    query: str,
    court: str,
    case_type: str,
    date_from: str,
    date_to: str,
    display: int = 100,
    max_pages: int = 200,
) -> list[dict]:
    """판례 검색 결과를 페이지 순회로 전부 수집 (max_pages 상한)."""
    collected: list[dict] = []
    page = 1
    while True:
        result = api_client.search_precedents(
            query=query, court=court, case_type=case_type,
            date_from=date_from, date_to=date_to,
            page=page, display=display,
        )
        collected.extend(result["precedents"])
        if page * display >= result["totalCnt"]:
            break
        if page >= max_pages:
            logger.warning(
                "precedents pagination hit max_pages=%d at totalCnt=%d — truncating",
                max_pages, result["totalCnt"],
            )
            break
        page += 1
    return collected


def collect(
    *,
    years: int = 0,
    days: int = 0,
    court: str = "대법원",
    case_types: list[str] | None = None,
    queries: list[str] | None = None,
    max_count: int = 0,
    dry_run: bool = False,
) -> dict[str, int]:
    """판례 수집 실행.

    Args:
        years: 최근 N 년치 수집 (bulk 모드, 0 이면 days 기반)
        days: 최근 N 일 수집 (recent 모드)
        court: 법원 필터 (기본 '대법원')
        case_types: 사건종류 리스트 (기본 ['민사','형사','가사','행정','특허','일반행정'])
        queries: 빈 쿼리로 전체 수집이 불가할 경우 쿼리 리스트로 분할 수집
        max_count: 이번 실행에서 새로 저장할 최대 건수 (쿼터 보호)
        dry_run: 실제 API 호출·저장 없이 플랜만 출력

    Returns:
        {'discovered': N, 'fetched': N, 'upserted': N, 'unchanged': N, 'errors': N}
    """
    if not LAW_API_KEY:
        logger.error("LAW_OC 키 없음 — 수집 불가")
        return {"discovered": 0, "fetched": 0, "upserted": 0, "unchanged": 0, "errors": 0}

    # DRF API 의 gana 필터는 일부 사건종류 조합에서 정상 동작하지 않음 —
    # 기본은 필터 없이 법원·날짜만으로 한 번에 조회하고, 필요 시 호출자가 명시적으로 지정.
    if case_types is None:
        case_types = [""]  # 빈 문자열 = case_type 필터 생략

    today = datetime.now()
    if years > 0:
        date_from = (today - timedelta(days=years * 365)).strftime("%Y%m%d")
    elif days > 0:
        date_from = (today - timedelta(days=days)).strftime("%Y%m%d")
    else:
        raise ValueError("years 또는 days 중 하나는 > 0 이어야 함")
    date_to = today.strftime("%Y%m%d")

    logger.info(f"collecting precedents: court={court} types={case_types} from={date_from} to={date_to}")

    # 전체 판례 목록 수집 (중복 제거)
    seen_ids: set[str] = set()
    discovered: list[dict] = []

    search_plans: list[tuple[str, str]] = []
    if queries:
        for q in queries:
            for ct in case_types:
                search_plans.append((q, ct))
    else:
        for ct in case_types:
            search_plans.append(("", ct))

    for q, ct in search_plans:
        try:
            items = _iterate_search(
                query=q, court=court, case_type=ct,
                date_from=date_from, date_to=date_to,
            )
            for item in items:
                pid = item["판례일련번호"]
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    discovered.append(item)
        except Exception as e:
            logger.exception("search failed: q=%r ct=%r: %s", q, ct, e)

    logger.info(f"discovered unique precedents: {len(discovered)}")

    if dry_run:
        return {"discovered": len(discovered), "fetched": 0, "upserted": 0, "unchanged": 0, "errors": 0}

    # 상세 수집 + 저장
    fetched = upserted = unchanged = errors = 0
    for i, item in enumerate(discovered, 1):
        pid = item["판례일련번호"]
        if not pid:
            continue
        if max_count > 0 and upserted >= max_count:
            logger.info(f"max_count={max_count} 도달 — 중단 (처리: {i}/{len(discovered)})")
            break
        try:
            # 이미 DB에 있고 본문이 변경 없으면 상세조회 skip 하는 최적화
            if store.exists(pid):
                # TODO: 증분 수집 시 content_hash 비교 위해 상세조회 필요할 수도
                # 지금은 이미 있으면 skip (bulk 모드 기준 — 판례는 수정이 거의 없음)
                unchanged += 1
                continue

            detail = api_client.get_precedent_detail(pid)
            fetched += 1
            if store.upsert(detail):
                upserted += 1
            else:
                unchanged += 1
        except Exception as e:
            logger.exception("detail failed for %s: %s", pid, e)
            errors += 1

        if i % 100 == 0:
            logger.info(f"progress: {i}/{len(discovered)} fetched={fetched} upserted={upserted} errors={errors}")

    result = {
        "discovered": len(discovered),
        "fetched": fetched,
        "upserted": upserted,
        "unchanged": unchanged,
        "errors": errors,
    }
    logger.info(f"collect done: {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="판례 수집기")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--years", type=int, help="최근 N 년치 bulk 수집")
    g.add_argument("--days", type=int, help="최근 N 일 증분")
    parser.add_argument("--court", default="대법원")
    parser.add_argument("--case-types", nargs="*")
    parser.add_argument("--queries", nargs="*", help="검색 쿼리 분할 (예: '계약' '손해배상')")
    parser.add_argument("--max-count", type=int, default=0,
                        help="이번 실행 최대 저장 건수 (OC 쿼터 보호). 0 = 무제한")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

    res = collect(
        years=args.years or 0,
        days=args.days or 0,
        court=args.court,
        case_types=args.case_types,
        queries=args.queries,
        max_count=args.max_count,
        dry_run=args.dry_run,
    )
    return 0 if res["errors"] == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
