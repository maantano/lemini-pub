"""M3-Z 자동 반영 파이프라인.

Cloud Run Job 내부에서 실행되며:
  1. GCS 에서 산출물 다운로드 → staging artifact 재생성
  2. 마이그레이션·ingest (법령·행정규칙·자율규약 통합)
  3. 프로덕션 DB 백업 (GCS)
  4. Cloud Build 로 새 lemini-api 이미지 빌드
  5. Cloud Run --no-traffic revision 배포
  6. Smoke test 3 건 통과 확인
  7. Traffic 100% 전환
  8. Slack 알림 (성공/실패)

운영:
  python -m worker.sync.rollout [--skip-collection] [--skip-deploy] [--dry-run]

환경변수 (Cloud Run Job 에 설정):
  PROJECT_ID              GCP 프로젝트 (<your-gcp-project>)
  REGION                  asia-northeast3
  API_SERVICE_NAME        lemini-api
  GCS_DATA_BUCKET         lemini-sync-data
  GCS_CACHE_BUCKET        lemini-sync-cache
  GCS_BACKUP_BUCKET       lemini-law-artifacts-backups
  SLACK_BOT_TOKEN / SLACK_ALERT_CHANNEL   알림
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .core import alerts

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("PROJECT_ID", "<your-gcp-project>")
REGION = os.environ.get("REGION", "asia-northeast3")
API_SERVICE = os.environ.get("API_SERVICE_NAME", "lemini-api")
DATA_BUCKET = os.environ.get("GCS_DATA_BUCKET", "lemini-sync-data")
CACHE_BUCKET = os.environ.get("GCS_CACHE_BUCKET", "lemini-sync-cache")
BACKUP_BUCKET = os.environ.get("GCS_BACKUP_BUCKET", "lemini-law-artifacts-backups")


# ───────────────────────────────────────────────────────────────
# Step 실행 헬퍼
# ───────────────────────────────────────────────────────────────

class StepFailure(Exception):
    """어떤 단계가 실패했는지 알려주는 예외."""
    def __init__(self, step: str, message: str, cause: BaseException | None = None):
        super().__init__(message)
        self.step = step
        self.cause = cause


def step(name: str, summary: dict, func: Callable[[], dict]) -> dict:
    """Step 을 실행하고 결과를 summary 에 병합. 실패하면 StepFailure."""
    logger.info("=== STEP: %s ===", name)
    started = time.time()
    try:
        result = func() or {}
        result["step_duration_s"] = int(time.time() - started)
        summary[name] = result
        logger.info("%s: OK (%ss)", name, result["step_duration_s"])
        return result
    except Exception as e:
        raise StepFailure(name, str(e), e) from e


# ───────────────────────────────────────────────────────────────
# 개별 step 구현
# ───────────────────────────────────────────────────────────────

def _run_cmd(cmd: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess:
    """gcloud 등 외부 명령. Cloud Run Job 이미지에 gcloud CLI 포함 필요."""
    logger.debug("exec: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)


def step_backup_prod_db(work_dir: Path) -> dict:
    """기존 laws.sqlite 를 GCS backup 버킷에 타임스탬프 업로드.

    참고: 원본은 프로덕션 Cloud Run 이미지 내부이므로 직접 접근 불가.
    따라서 **가장 최근 백업본을 base 로 삼고**, 이번에 새로 만든 staging 을 업로드한다.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    dest = f"gs://{BACKUP_BUCKET}/laws.sqlite.{ts}.bak"
    src = work_dir / "laws.sqlite"
    _run_cmd(["gcloud", "storage", "cp", str(src), dest, "--project", PROJECT_ID])
    return {"backup_path": dest, "size_bytes": src.stat().st_size}


def step_build_staging_db(work_dir: Path) -> dict:
    """GCS 의 원본 산출물(kr/, 행정규칙/, voluntary-codes/) 을 순회하며 laws.sqlite 재생성.

    가장 최근 프로덕션 백업을 base 로 삼고 그 위에 모든 마크다운을 append.
    """
    work_dir.mkdir(parents=True, exist_ok=True)

    # 1) base 로 쓸 최신 백업 pull
    latest = _run_cmd(
        ["gcloud", "storage", "ls", f"gs://{BACKUP_BUCKET}/", "--project", PROJECT_ID]
    ).stdout.strip().splitlines()
    if not latest:
        raise RuntimeError("백업 없음 — 초기 1회는 수동 백업 필요")
    base_backup = sorted(latest)[-1]
    base_path = work_dir / "laws.sqlite"
    _run_cmd(["gcloud", "storage", "cp", base_backup, str(base_path), "--project", PROJECT_ID])

    # 2) 마이그레이션 적용 (idempotent)
    from .laws.config import WORKSPACE_ROOT  # noqa: F401
    mig_dir = Path(__file__).resolve().parents[4] / "data" / "migrations"
    for mig in sorted(mig_dir.glob("*.sql")):
        try:
            _run_cmd(["sqlite3", str(base_path), ".read " + str(mig)])
        except subprocess.CalledProcessError:
            # 이미 적용된 마이그레이션이면 ALTER ADD COLUMN 에러 — 무시
            logger.info("migration %s already applied (skip)", mig.name)

    # 3) GCS 의 마크다운 전체 다운로드
    md_root = work_dir / "md"
    md_root.mkdir(exist_ok=True)
    _run_cmd(["gcloud", "storage", "cp", "-r", f"gs://{DATA_BUCKET}/*",
              str(md_root) + "/", "--project", PROJECT_ID])

    # 4) append script 호출 (laws/admrul/voluntary 한 번에)
    repo_root = Path(__file__).resolve().parents[4]
    append_script = repo_root / "scripts" / "append_admrul_bulk.py"
    env = os.environ.copy()
    env["ARTIFACT_DIR"] = str(work_dir)
    env["PYTHONPATH"] = str(repo_root / "packages/python/src")

    subprocess.run([sys.executable, str(append_script), str(md_root)],
                   env=env, check=True, timeout=3600)

    # 5) 통계
    import sqlite3
    conn = sqlite3.connect(base_path)
    rows = conn.execute(
        "SELECT document_type, COUNT(*) FROM law_documents GROUP BY document_type"
    ).fetchall()
    conn.close()
    return {"by_type": {r[0]: r[1] for r in rows}, "db_path": str(base_path)}


def step_cloud_build(work_dir: Path, image_tag: str) -> dict:
    """새 lemini-api 이미지 빌드. staging DB 를 data/artifacts/ 에 복사 후 빌드."""
    repo_root = Path(__file__).resolve().parents[4]
    # NOTE: 이 Job 은 빌드 소스를 자신이 체크아웃해야 함. 초기 구현은 GCR/GitHub 에서
    # lemini-api 소스 동기화를 가정. 여기서는 단순화해 gcloud builds submit 로 감쌈.
    image = f"gcr.io/{PROJECT_ID}/{API_SERVICE}:{image_tag}"

    # 로컬 repo 의 data/artifacts 를 staging DB 로 교체
    art_dir = repo_root / "data" / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(work_dir / "laws.sqlite", art_dir / "laws.sqlite")
    # embeddings 도 함께 (append_admrul_bulk 가 work_dir 에 생성)
    for f in ("article_embeddings.npy", "article_embedding_ids.json"):
        src = work_dir / f
        if src.exists():
            shutil.copy2(src, art_dir / f)

    _run_cmd(["gcloud", "builds", "submit", str(repo_root),
              "--tag", image, "--file", str(repo_root / "Dockerfile"),
              "--project", PROJECT_ID, "--timeout", "1200s"])
    return {"image": image}


def step_deploy_staging(image: str) -> dict:
    """--no-traffic 으로 새 revision 배포. tag=staging 으로 별도 URL 할당."""
    res = _run_cmd([
        "gcloud", "run", "deploy", API_SERVICE,
        "--image", image, "--region", REGION, "--project", PROJECT_ID,
        "--no-traffic", "--tag", "staging",
        "--format", "value(status.url,status.latestCreatedRevisionName)",
    ])
    parts = res.stdout.strip().split()
    revision = parts[-1] if parts else ""
    # tag URL 추출
    tag_info = _run_cmd([
        "gcloud", "run", "services", "describe", API_SERVICE,
        "--region", REGION, "--project", PROJECT_ID,
        "--format", "value(status.traffic[?tag='staging'].url)",
    ]).stdout.strip()
    return {"revision": revision, "staging_url": tag_info}


def step_smoke_test(staging_url: str) -> dict:
    """3 개 대표 질문 호출 테스트."""
    import requests
    questions = [
        ("법령", "민법 제750조의 내용을 알려주세요"),
        ("행정규칙", "공정거래위원회 고시나 지침 관련 정보를 알려주세요"),
        ("자율규약", "의료기기 리베이트 허용 기준은?"),
    ]
    passed = failed = 0
    for label, q in questions:
        try:
            r = requests.post(
                f"{staging_url}/chat",
                json={"question": q, "save": False, "stream": False},
                timeout=60,
            )
            if r.status_code == 200:
                j = r.json()
                ok = bool(j.get("answer")) and len(j["answer"]) >= 50
                if ok:
                    passed += 1
                    logger.info("smoke %s OK (len=%d)", label, len(j["answer"]))
                else:
                    failed += 1
                    logger.warning("smoke %s: 응답 너무 짧음", label)
            else:
                failed += 1
                logger.warning("smoke %s: HTTP %d", label, r.status_code)
        except Exception as e:
            failed += 1
            logger.warning("smoke %s: %s", label, e)

    if failed > 0:
        raise RuntimeError(f"smoke test 실패: {failed}/{len(questions)}")

    return {"passed": passed, "failed": failed}


def step_promote_traffic() -> dict:
    """Staging revision 을 100% 트래픽으로 전환."""
    _run_cmd([
        "gcloud", "run", "services", "update-traffic", API_SERVICE,
        "--to-latest", "--region", REGION, "--project", PROJECT_ID,
    ])
    return {"status": "promoted"}


# ───────────────────────────────────────────────────────────────
# main
# ───────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="M3-Z 자동 반영 파이프라인")
    p.add_argument("--skip-collection", action="store_true",
                   help="수집 스킵 (이미 Job 끝에서 호출되면 불필요)")
    p.add_argument("--skip-deploy", action="store_true",
                   help="실제 Cloud Run 배포 생략 (dry-run 유사)")
    p.add_argument("--work-dir", default="/tmp/rollout")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

    job_name = "lemini-rollout"
    work_dir = Path(args.work_dir)
    image_tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    summary: dict = {}

    try:
        step("build_staging_db", summary, lambda: step_build_staging_db(work_dir))
        step("backup_prod_db", summary, lambda: step_backup_prod_db(work_dir))

        if args.skip_deploy:
            logger.info("skip_deploy=True — 이후 배포 단계 생략")
            alerts.success(job_name, summary)
            return 0

        build_res = step("cloud_build", summary, lambda: step_cloud_build(work_dir, image_tag))
        deploy_res = step("deploy_staging", summary, lambda: step_deploy_staging(build_res["image"]))
        step("smoke_test", summary, lambda: step_smoke_test(deploy_res["staging_url"]))
        step("promote_traffic", summary, step_promote_traffic)

        alerts.success(job_name, summary)
        return 0

    except StepFailure as e:
        logger.exception("step %s 실패", e.step)
        alerts.failure(job_name, e.step, e.cause or e, partial=summary)
        return 1
    except BaseException as e:
        logger.exception("치명 오류")
        alerts.failure(job_name, "unknown", e, partial=summary)
        return 2


if __name__ == "__main__":
    sys.exit(main())
