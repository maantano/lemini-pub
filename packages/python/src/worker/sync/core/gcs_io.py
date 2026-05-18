"""GCS 어댑터 — 로컬 파일시스템과 동일한 인터페이스로 GCS 에 접근.

설계 원칙:
- 로컬 개발은 그냥 `WORKSPACE_ROOT` 디렉토리를 쓴다 (GCS 설정 없어도 동작).
- Cloud Run Job 에서만 ``GCS_DATA_BUCKET`` / ``GCS_CACHE_BUCKET`` 환경변수로 GCS 를 사용.
- 기존 `atomic_io` · `cache.py` 가 로컬 FS 로 읽고 쓴 뒤, Job 마지막에
  `upload_workspace_to_gcs()` 로 한 번에 올리는 방식. (개별 파일마다 GCS API
  호출하면 비용·throttle 폭증하므로 batch 업로드)

사용 예:
    from worker.sync.core import gcs_io
    gcs_io.upload_workspace_to_gcs()  # WORKSPACE_ROOT → gs://$GCS_DATA_BUCKET/
    gcs_io.download_cache_from_gcs()  # gs://$GCS_CACHE_BUCKET/ → WORKSPACE_ROOT/.cache
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .config import GCS_CACHE_BUCKET, GCS_DATA_BUCKET, WORKSPACE_ROOT

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(GCS_DATA_BUCKET or GCS_CACHE_BUCKET)


def _client():
    """GCS client (lazy import — local dev doesn't need google-cloud-storage)."""
    try:
        from google.cloud import storage  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "google-cloud-storage 미설치. "
            "`pip install -e '.[sync]'` 실행 필요"
        ) from e
    return storage.Client()


def _upload_dir(client, bucket_name: str, local_root: Path, gcs_prefix: str = "") -> tuple[int, int]:
    """디렉토리 전체를 GCS 에 업로드. (파일 수, 바이트 수) 반환."""
    bucket = client.bucket(bucket_name)
    files = 0
    bytes_total = 0

    for path in local_root.rglob("*"):
        if not path.is_file():
            continue
        # 숨김 파일은 data 버킷 제외 (cache 쪽은 .checkpoint 등 필요)
        rel = path.relative_to(local_root)
        blob_name = f"{gcs_prefix}{rel}".lstrip("/") if gcs_prefix else str(rel)
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(str(path))
        size = path.stat().st_size
        files += 1
        bytes_total += size

    logger.info("Uploaded %d files (%d bytes) to gs://%s/%s", files, bytes_total, bucket_name, gcs_prefix)
    return files, bytes_total


def _download_dir(client, bucket_name: str, local_root: Path, gcs_prefix: str = "") -> int:
    """GCS 버킷 → 로컬 디렉토리. 파일 수 반환."""
    bucket = client.bucket(bucket_name)
    count = 0
    for blob in bucket.list_blobs(prefix=gcs_prefix):
        rel = blob.name[len(gcs_prefix):].lstrip("/") if gcs_prefix else blob.name
        if not rel:
            continue
        target = local_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(target))
        count += 1
    logger.info("Downloaded %d files from gs://%s/%s", count, bucket_name, gcs_prefix)
    return count


def upload_workspace_to_gcs() -> dict[str, int]:
    """수집 결과(kr/, precedent-kr/ ...)를 data 버킷에, cache 를 cache 버킷에 업로드."""
    if not _enabled():
        logger.info("GCS disabled — skipping upload (local dev mode)")
        return {"files": 0, "bytes": 0}

    client = _client()
    total_files, total_bytes = 0, 0

    if GCS_DATA_BUCKET:
        # data 버킷: 마크다운만 (kr/, precedent-kr/, voluntary-codes/)
        for subdir in ("kr", "precedent-kr", "voluntary-codes"):
            src = WORKSPACE_ROOT / subdir
            if not src.exists():
                continue
            f, b = _upload_dir(client, GCS_DATA_BUCKET, src, gcs_prefix=f"{subdir}/")
            total_files += f
            total_bytes += b

    if GCS_CACHE_BUCKET:
        cache_dir = WORKSPACE_ROOT / ".cache"
        if cache_dir.exists():
            f, b = _upload_dir(client, GCS_CACHE_BUCKET, cache_dir)
            total_files += f
            total_bytes += b

        # checkpoint / failures 도 cache 버킷으로
        for name in (".checkpoint.json", ".failed_msts.json"):
            src = WORKSPACE_ROOT / name
            if src.exists():
                client.bucket(GCS_CACHE_BUCKET).blob(name).upload_from_filename(str(src))
                total_files += 1
                total_bytes += src.stat().st_size

    return {"files": total_files, "bytes": total_bytes}


def download_cache_from_gcs() -> int:
    """다음 실행을 위해 기존 cache + checkpoint 를 로컬로 내려받음."""
    if not _enabled() or not GCS_CACHE_BUCKET:
        return 0

    client = _client()
    count = _download_dir(client, GCS_CACHE_BUCKET, WORKSPACE_ROOT / ".cache", gcs_prefix="")

    # top-level checkpoint 파일들
    for name in (".checkpoint.json", ".failed_msts.json"):
        blob = client.bucket(GCS_CACHE_BUCKET).blob(name)
        if blob.exists():
            target = WORKSPACE_ROOT / name
            blob.download_to_filename(str(target))
            count += 1
    return count


def ensure_buckets() -> None:
    """배포 시 한 번 호출. 버킷이 없으면 생성 (idempotent)."""
    if not _enabled():
        return
    client = _client()
    for bucket_name in (GCS_DATA_BUCKET, GCS_CACHE_BUCKET):
        if not bucket_name:
            continue
        bucket = client.bucket(bucket_name)
        if not bucket.exists():
            new_bucket = client.create_bucket(bucket_name, location="asia-northeast3")
            # Versioning OFF (SSOT 가 외부라 불필요)
            new_bucket.versioning_enabled = False
            new_bucket.patch()
            logger.info("Created bucket gs://%s (versioning off)", bucket_name)
