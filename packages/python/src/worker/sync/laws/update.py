"""Incremental updater for new/amended laws.

Uses search API to find recently changed laws, fetches detail, converts to
Markdown, and writes to WORKSPACE_ROOT/kr/. Idempotent via checkpoint.

원본 legalize-pipeline은 git commit 기반 저장이지만, 우리는 파일시스템/GCS에
직접 쓰기만 함. git_engine · import_laws · generate_metadata 의존을 제거한
경량 버전이다.

Usage:
    python -m worker.sync.laws.update                    # Update recent laws (default 7 days)
    python -m worker.sync.laws.update --days 30          # Look back 30 days
    python -m worker.sync.laws.update --law-type 법률    # Only 법률
    python -m worker.sync.laws.update --dry-run          # Preview only

Adapted from legalize-pipeline (MIT/Apache-2.0). See sync/NOTICES.md.
"""

import argparse
import logging
from datetime import datetime, timedelta

from ..core.atomic_io import atomic_write_text
from .api_client import get_law_detail, search_laws
from .checkpoint import get_last_update, get_processed_msts, mark_processed, set_last_update
from .config import KR_DIR, LAW_API_KEY, WORKSPACE_ROOT
from .converter import (
    entry_sort_key,
    format_date,
    get_law_path,
    law_to_markdown,
    reset_path_registry,
)
from .failures import classify, log_failure, mark_failed, mark_failed_and_quarantine

logger = logging.getLogger(__name__)


def update(
    days: int = 7,
    law_type_filter: str | None = None,
    dry_run: bool = False,
    max_pages: int = 50,
) -> int:
    """Query API for recently amended laws and import their latest versions.

    Returns the number of laws written.
    """
    if not LAW_API_KEY:
        logger.error("No API key (LAW_OC) configured. Cannot update.")
        return 0

    reset_path_registry()

    last = get_last_update()
    since = last.replace("-", "") if last else (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    today = datetime.now().strftime("%Y%m%d")

    logger.info(f"Searching amendments from {since} to {today}")

    # Collect all search results with pagination bounded by max_pages
    all_laws: list[dict] = []
    page = 1
    while True:
        result = search_laws(query="", page=page, display=100, date_from=since, date_to=today)
        all_laws.extend(result["laws"])
        if page * 100 >= result["totalCnt"]:
            break
        if page >= max_pages:
            raise RuntimeError(
                f"laws.update pagination exceeded max_pages={max_pages} "
                f"(totalCnt={result['totalCnt']}, collected={len(all_laws)}). "
                f"Likely pagination regression, unexpected window size, or backfill — "
                f"raise --max-pages explicitly if this is intentional."
            )
        page += 1

    processed = get_processed_msts()
    new_laws = [law for law in all_laws if law["법령일련번호"] and law["법령일련번호"] not in processed]
    new_laws.sort(key=lambda x: entry_sort_key(
        x.get("공포일자", ""),
        x.get("법령명한글", ""),
        x.get("공포번호", ""),
        x.get("법령일련번호", ""),
    ))

    logger.info(f"Found {len(all_laws)} results, {len(new_laws)} new after checkpoint filter")

    written = 0
    errors = 0

    for i, law in enumerate(new_laws, 1):
        mst = law["법령일련번호"]
        name = law.get("법령명한글", "")

        file_path = None
        try:
            detail = get_law_detail(mst)
            meta = detail["metadata"]
            law_type = meta.get("법령구분", "")

            if law_type_filter and law_type_filter != law_type:
                continue

            fetched_name = meta.get("법령명한글", name)
            law_id = meta.get("법령ID", "")
            file_path = get_law_path(fetched_name, law_type, law_id)
            abs_path = WORKSPACE_ROOT / file_path

            meta["제개정구분"] = law.get("제개정구분명", meta.get("제개정구분", ""))
            if not meta.get("공포번호"):
                meta["공포번호"] = law.get("공포번호", "")

            prom_date = format_date(meta.get("공포일자", ""))

            if dry_run:
                logger.info(f"  [{i}/{len(new_laws)}] [DRY-RUN] MST={mst} {prom_date} {fetched_name} -> {file_path}")
                continue

            content = law_to_markdown(detail)
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(abs_path, content)

            mark_processed(mst)
            written += 1
            logger.info(f"  [{i}/{len(new_laws)}] Wrote MST={mst} {prom_date} {fetched_name}")

        except ValueError as e:  # empty body
            log_failure("update", str(mst), name, e)
            if file_path is not None:
                mark_failed_and_quarantine(
                    mst=str(mst), reason="empty_body", detail=str(e),
                    path=WORKSPACE_ROOT / file_path,
                    step="update", law_name=name,
                )
            else:
                mark_failed(mst=str(mst), reason="empty_body", detail=str(e),
                            step="update", law_name=name)
            errors += 1
        except Exception as e:
            log_failure("update", str(mst), name, e)
            mark_failed(mst=str(mst), reason=classify(e), detail=str(e),
                        step="update", law_name=name)
            errors += 1

        if i % 50 == 0:
            logger.info(f"Progress: {i}/{len(new_laws)} (written={written}, errors={errors})")

    if not dry_run:
        set_last_update(format_date(today))

    logger.info(f"Update done: written={written}, errors={errors}")
    return written


def main():
    parser = argparse.ArgumentParser(description="Incremental law updater")
    parser.add_argument("--days", type=int, default=7, help="Look back N days (default: 7)")
    parser.add_argument("--law-type", help="Filter by 법령구분 (e.g., 법률)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help=(
            "Abort if pagination exceeds N pages (100 items/page). "
            "Default 50 = 5000 items, sized for daily cron. "
            "Raise for backfill (e.g. --days 3650 --max-pages 500)."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    written = update(
        days=args.days,
        law_type_filter=args.law_type,
        dry_run=args.dry_run,
        max_pages=args.max_pages,
    )

    logger.info(f"Update complete: {written} laws written")


if __name__ == "__main__":
    main()
