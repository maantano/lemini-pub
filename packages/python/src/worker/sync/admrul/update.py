"""행정규칙 증분 수집기.

주간 자동 실행: 최근 N일 발령된 규칙 신규 수집.
수동 bulk: --years 5 같은 대량 초기 수집 (OC 쿼터 보호를 위해 --max-count 권장).

저장:
  - 마크다운: WORKSPACE_ROOT/kr/행정규칙/{부처}/{규칙명}.md
  - 이후 IngestService 가 laws.sqlite (document_type='administrative_rule') 에 색인
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path

from ..core.atomic_io import atomic_write_text
from ..laws.checkpoint import load as _load_cp, save as _save_cp
from ..laws.config import LAW_API_KEY, WORKSPACE_ROOT
from . import api_client, converter

logger = logging.getLogger(__name__)

CHECKPOINT_KEY = "admrul"  # .checkpoint.json 내 네임스페이스


def _get_admrul_checkpoint() -> dict:
    all_cp = _load_cp()
    return all_cp.get(CHECKPOINT_KEY, {"processed_rule_ids": [], "last_update": ""})


def _save_admrul_checkpoint(cp: dict) -> None:
    all_cp = _load_cp()
    all_cp[CHECKPOINT_KEY] = cp
    _save_cp(all_cp)


def collect(
    *,
    days: int = 30,
    years: int = 0,
    org: str = "",
    max_count: int = 0,
    dry_run: bool = False,
) -> dict[str, int]:
    """행정규칙 수집.

    Args:
        days: 최근 N일 증분 (주간용)
        years: 최근 N년 bulk (수동 초기 수집용, days 보다 우선)
        org: 소관부처코드 필터 (옵션)
        max_count: 이번 실행에서 저장할 최대 건수 (OC 쿼터 보호)
        dry_run: API 호출만 하고 파일 쓰기 안 함
    """
    if not LAW_API_KEY:
        logger.error("LAW_OC 키 없음")
        return {"discovered": 0, "fetched": 0, "written": 0, "skipped": 0, "errors": 0}

    today = datetime.now()
    if years > 0:
        date_from = (today - timedelta(days=years * 365)).strftime("%Y%m%d")
    else:
        date_from = (today - timedelta(days=max(days, 1))).strftime("%Y%m%d")
    date_to = today.strftime("%Y%m%d")

    logger.info(f"admrul 수집: org={org or '전체'} from={date_from} to={date_to}")

    # 페이징
    discovered: list[dict] = []
    page = 1
    max_pages = 200
    while True:
        res = api_client.search_admrul(
            org=org, date_from=date_from, date_to=date_to,
            page=page, display=100,
        )
        discovered.extend(res["rules"])
        if page * 100 >= res["totalCnt"]:
            break
        if page >= max_pages:
            logger.warning("pagination max_pages=%d 도달 (totalCnt=%d)", max_pages, res["totalCnt"])
            break
        page += 1

    logger.info(f"discovered: {len(discovered)} rules")

    cp = _get_admrul_checkpoint()
    processed = set(cp.get("processed_rule_ids", []))

    fetched = written = skipped = errors = 0
    for i, item in enumerate(discovered, 1):
        rid = item["행정규칙일련번호"]
        if not rid:
            continue
        if rid in processed:
            skipped += 1
            continue
        if max_count > 0 and written >= max_count:
            logger.info(f"max_count={max_count} 도달, 중단 (처리: {i}/{len(discovered)})")
            break

        try:
            detail = api_client.get_admrul_detail(rid)
            fetched += 1

            if detail["body_is_reference_only"]:
                # 첨부 의존 규칙 — 지금은 skip, 차후 HWP/PDF 처리
                skipped += 1
                processed.add(rid)
                continue

            if dry_run:
                logger.info(f"  [{i}/{len(discovered)}] [DRY] {detail['rule_type']} {detail['rule_name'][:40]}")
                continue

            md = converter.admrul_to_markdown(detail)
            rel_path = converter.get_admrul_path(detail)
            abs_path = WORKSPACE_ROOT / rel_path
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(abs_path, md)

            processed.add(rid)
            written += 1
            logger.info(f"  [{i}/{len(discovered)}] {detail['rule_type']} {detail['rule_name'][:40]} → {rel_path}")
        except ValueError as e:  # body_is_reference_only 등
            skipped += 1
            processed.add(rid)
            logger.debug("skip %s: %s", rid, e)
        except Exception as e:
            logger.exception("rule %s failed: %s", rid, e)
            errors += 1

        if i % 50 == 0:
            logger.info(f"progress: {i}/{len(discovered)} fetched={fetched} written={written} skipped={skipped} errors={errors}")

    if not dry_run:
        cp["processed_rule_ids"] = sorted(processed)
        cp["last_update"] = today.strftime("%Y-%m-%d")
        _save_admrul_checkpoint(cp)

    result = {
        "discovered": len(discovered),
        "fetched": fetched,
        "written": written,
        "skipped": skipped,
        "errors": errors,
    }
    logger.info(f"admrul collect done: {result}")
    return result


def main() -> int:
    p = argparse.ArgumentParser(description="행정규칙 수집기")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, default=30, help="최근 N일 (기본 30)")
    g.add_argument("--years", type=int, help="최근 N년 bulk")
    p.add_argument("--org", default="", help="소관부처코드 필터 (예: 1270000=공정위)")
    p.add_argument("--max-count", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

    res = collect(
        days=args.days, years=args.years or 0,
        org=args.org, max_count=args.max_count, dry_run=args.dry_run,
    )
    return 0 if res["errors"] == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
