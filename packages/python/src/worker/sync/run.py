"""통합 sync entrypoint — Cloud Run Job 에서 호출.

서브커맨드:
  --target all          기본. laws + precedents + (monthly flag 시) repeal 까지
  --target laws         법령 증분 수집만
  --target precedents   판례 증분 수집만 (M4 에서 구현)
  --target repeal       법령 폐지 감지만 (M6 에서 구현)

실행 흐름:
  1. GCS cache 다운로드 (재개용)
  2. 각 target 순차 실행 — 개별 실패는 기록 후 계속 진행
  3. GCS 에 결과 업로드
  4. Slack 성공/실패 알림

Usage:
  # 로컬 개발
  python -m worker.sync.run --target laws --days 7

  # Cloud Run Job 에서 (주 1회)
  python -m worker.sync.run --target all

환경변수:
  LAW_OC / LAW_API_KEY         법제처 OC 키
  GCS_DATA_BUCKET              gs:// 업로드 대상 (비면 로컬 FS 유지)
  GCS_CACHE_BUCKET             gs:// cache 동기화 대상
  SLACK_BOT_TOKEN              Slack 알림 (비면 알림 skip)
  SLACK_ALERT_CHANNEL          알림 채널
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime

from .core import alerts, gcs_io

logger = logging.getLogger(__name__)


def _run_laws(days: int, dry_run: bool) -> dict:
    """법령 증분 수집."""
    from .laws.update import update as update_laws

    written = update_laws(days=days, dry_run=dry_run)
    return {"laws_written": written}


def _run_precedents(days: int = 30, max_count: int = 900) -> dict:
    """대법원 판례 증분 수집.

    - 기본: 최근 30 일 대법원 판례, 신규만 상세 조회·저장
    - max_count: OC 쿼터 보호 (개발계정 일 1,000건). Cloud Run Job 한 번에 900건 제한
    """
    from .precedents.update import collect

    res = collect(days=days, court="대법원", max_count=max_count)
    return {
        "precedents_discovered": res["discovered"],
        "precedents_fetched": res["fetched"],
        "precedents_upserted": res["upserted"],
        "precedents_unchanged": res["unchanged"],
        "precedents_errors": res["errors"],
    }


def _run_repeal() -> dict:
    """법령 폐지 감지 — M6 구현 예정."""
    logger.warning("repeal target is not implemented yet (M6 scope)")
    return {"repealed_detected": 0, "skipped": True}


def _should_run_monthly_today() -> bool:
    """매월 1주차 (1~7일) 에만 monthly job 실행."""
    return datetime.now().day <= 7


def main() -> int:
    parser = argparse.ArgumentParser(description="Lemini sync unified entrypoint")
    parser.add_argument("--target", default="all",
                        choices=["all", "laws", "precedents", "precedents-bulk", "repeal"])
    parser.add_argument("--max-precedents", type=int, default=0,
                        help="precedents-bulk 실행 시 최대 건수 (0 = 기본 900)")
    parser.add_argument("--days", type=int, default=7,
                        help="law lookback window (default 7)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-gcs-sync", action="store_true",
                        help="GCS 업로드/다운로드 건너뜀 (로컬 반복 테스트용)")
    parser.add_argument("--job-name", default=os.environ.get("CLOUD_RUN_JOB", "lemini-data-sync"),
                        help="Slack 알림에 쓸 Job 이름")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    started = time.time()
    summary: dict[str, object] = {}
    current_step = "init"

    try:
        # 1. cache 다운로드 (이전 실행 체크포인트 복원)
        if not args.skip_gcs_sync:
            current_step = "gcs_download"
            count = gcs_io.download_cache_from_gcs()
            summary["cache_files_restored"] = count

        # 2. targets 실행 — 각각 독립 try 로 개별 실패가 전체를 막지 않음
        # 주 1회 자동 실행('all')에서는 법령 최신화 + 폐지 감지만.
        # 판례는 on-demand 캐시(law_rag_core.precedent.service) 로 자연 누적되므로
        # 주간 자동 증분 수집은 비활성화. 필요 시 --target precedents 또는
        # --target precedents-bulk 로 수동 실행.
        if args.target == "all":
            targets = ["laws", "repeal"]
        else:
            targets = [args.target]
        failures_list: list[tuple[str, BaseException]] = []

        for t in targets:
            current_step = t
            try:
                if t == "laws":
                    summary.update(_run_laws(days=args.days, dry_run=args.dry_run))
                elif t == "precedents":
                    summary.update(_run_precedents())
                elif t == "precedents-bulk":
                    # 수동 bulk 초기 수집용 — 쿼터 주의 (일 1,000건)
                    from .precedents.update import collect
                    res = collect(years=10, court="대법원", max_count=args.max_precedents or 900)
                    summary.update({
                        "precedents_discovered": res["discovered"],
                        "precedents_fetched": res["fetched"],
                        "precedents_upserted": res["upserted"],
                        "precedents_errors": res["errors"],
                    })
                elif t == "repeal":
                    if args.target == "all" and not _should_run_monthly_today():
                        logger.info("repeal target skipped (not in first week of month)")
                        summary["repeal_skipped_reason"] = "not_first_week"
                        continue
                    summary.update(_run_repeal())
            except Exception as e:  # 개별 target 실패
                logger.exception("target %s failed", t)
                failures_list.append((t, e))

        # 3. GCS 업로드
        if not args.skip_gcs_sync and not args.dry_run:
            current_step = "gcs_upload"
            uploaded = gcs_io.upload_workspace_to_gcs()
            summary["uploaded_files"] = uploaded["files"]
            summary["uploaded_bytes"] = uploaded["bytes"]

        duration_s = int(time.time() - started)
        summary["duration_s"] = duration_s

        # 4. Slack 알림
        if failures_list:
            # 첫 번째 실패만 기준으로 알림 (모두 나열하면 너무 장황)
            step, exc = failures_list[0]
            alerts.failure(
                args.job_name,
                step=step,
                exc=exc,
                partial={k: v for k, v in summary.items() if k != "duration_s"},
            )
            # 부분 실패도 exit 1 로 Cloud Run Job 이 실패로 기록하도록
            return 1

        logger.info("sync complete: %s", summary)
        alerts.success(args.job_name, summary)
        return 0

    except BaseException as e:  # 예상 못한 치명 오류
        logger.exception("fatal failure at step=%s", current_step)
        alerts.failure(args.job_name, step=current_step, exc=e, partial=summary)
        return 2


if __name__ == "__main__":
    sys.exit(main())
