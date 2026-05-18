"""주요 부처별 행정규칙 샘플 수집 (M8-A-2 검증용).

'query' 파라미터에 부처명을 넣어 해당 부처 발령 규칙을 필터링.
각 부처당 max_count 건씩 수집해서 WORKSPACE_ROOT/kr/행정규칙/ 에 저장.

사용:
  PYTHONPATH=packages/python/src WORKSPACE_ROOT=$(pwd)/data/sync \\
  python scripts/sample_admrul_by_agencies.py
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/python/src"))

from worker.sync.admrul import api_client, converter
from worker.sync.core.atomic_io import atomic_write_text
from worker.sync.laws.config import WORKSPACE_ROOT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


AGENCIES = [
    ("공정거래위원회", 50),
    ("식품의약품안전처", 50),
    ("금융위원회", 50),
    ("개인정보보호위원회", 30),
    ("국토교통부", 40),
    ("보건복지부", 40),
    ("금융감독원", 30),
]


def collect_for_agency(name: str, max_count: int) -> tuple[int, int, int]:
    written = skipped = errors = 0
    seen_ids: set[str] = set()

    page = 1
    while written < max_count:
        res = api_client.search_admrul(query=name, page=page, display=50)
        if not res["rules"]:
            break
        for item in res["rules"]:
            if item.get("소관부처명") != name:
                continue  # query 에 맞지 않는 타 부처 규칙 제외
            rid = item["행정규칙일련번호"]
            if not rid or rid in seen_ids:
                continue
            seen_ids.add(rid)
            if written >= max_count:
                break
            try:
                detail = api_client.get_admrul_detail(rid)
                if detail["body_is_reference_only"]:
                    skipped += 1
                    continue
                md = converter.admrul_to_markdown(detail)
                path = WORKSPACE_ROOT / converter.get_admrul_path(detail)
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, md)
                written += 1
            except Exception as e:
                log.warning("  %s / %s 실패: %s", rid, item.get("행정규칙명"), e)
                errors += 1

        if page * 50 >= res["totalCnt"]:
            break
        page += 1

    return written, skipped, errors


def main() -> int:
    log.info(f"WORKSPACE_ROOT={WORKSPACE_ROOT}")
    total_written = total_skipped = total_errors = 0
    summary: dict[str, tuple[int, int, int]] = {}

    for agency, max_count in AGENCIES:
        log.info("=" * 60)
        log.info(f"[{agency}] 목표 {max_count}건")
        w, s, e = collect_for_agency(agency, max_count)
        log.info(f"[{agency}] written={w} skipped={s} errors={e}")
        summary[agency] = (w, s, e)
        total_written += w
        total_skipped += s
        total_errors += e

    log.info("=" * 60)
    log.info("최종 결과:")
    for agency, (w, s, e) in summary.items():
        log.info(f"  {agency}: 저장 {w}, skip(참조전용) {s}, 에러 {e}")
    log.info(f"  합계: 저장 {total_written}, skip {total_skipped}, 에러 {total_errors}")
    return 0 if total_errors < total_written else 1


if __name__ == "__main__":
    raise SystemExit(main())
