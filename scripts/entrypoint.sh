#!/usr/bin/env bash
# Cloud Run entrypoint — state.sqlite를 GCS에서 동기화하여 데이터 영속성 보장.
#
# 흐름:
# 1. 시작 시 GCS에서 state.sqlite 다운로드 (있으면)
# 2. 백그라운드에서 5분마다 GCS에 업로드
# 3. uvicorn 실행
# 4. SIGTERM 수신 시 마지막 업로드 후 종료

set -euo pipefail

STATE_BUCKET="${STATE_DB_BUCKET:-}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/app/data/artifacts}"
STATE_LOCAL="${ARTIFACT_DIR}/state.sqlite"

# GCS 동기화가 설정되지 않으면 (로컬 개발 등) 그냥 uvicorn 실행
if [ -z "$STATE_BUCKET" ]; then
  echo "[entrypoint] STATE_DB_BUCKET not set — skipping GCS sync (local mode)."
  exec uvicorn apps.api.main:app --host 0.0.0.0 --port "${PORT:-8080}"
fi

STATE_GCS="gs://${STATE_BUCKET}/state.sqlite"

# ── Python 기반 GCS 업로드/다운로드 함수 ──
gcs_download() {
  python3 -c "
from google.cloud import storage
import sys
try:
    client = storage.Client()
    bucket = client.bucket('${STATE_BUCKET}')
    blob = bucket.blob('state.sqlite')
    if blob.exists():
        blob.download_to_filename('${STATE_LOCAL}')
        print(f'[gcs] Downloaded state.sqlite ({blob.size} bytes)')
    else:
        print('[gcs] No state.sqlite in GCS — starting fresh.')
except Exception as e:
    print(f'[gcs] Download failed: {e}', file=sys.stderr)
"
}

gcs_upload() {
  python3 -c "
from google.cloud import storage
from pathlib import Path
import sys
try:
    p = Path('${STATE_LOCAL}')
    if not p.exists():
        sys.exit(0)
    client = storage.Client()
    bucket = client.bucket('${STATE_BUCKET}')
    blob = bucket.blob('state.sqlite')
    blob.upload_from_filename('${STATE_LOCAL}')
    print(f'[gcs] Uploaded state.sqlite ({p.stat().st_size} bytes)')
except Exception as e:
    print(f'[gcs] Upload failed: {e}', file=sys.stderr)
"
}

# ── 1. 시작 시 GCS에서 다운로드 ──
echo "[entrypoint] Downloading state.sqlite from GCS..."
gcs_download

# ── 2. 백그라운드 동기화 (5분마다 업로드) ──
(
  while true; do
    sleep 300
    gcs_upload
  done
) &
SYNC_PID=$!

# ── 3. SIGTERM 핸들러 ──
cleanup() {
  echo "[entrypoint] SIGTERM received — uploading state.sqlite before shutdown..."
  gcs_upload
  kill "$SYNC_PID" 2>/dev/null || true
  kill "$APP_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
  exit 0
}
trap cleanup SIGTERM SIGINT

# ── 4. uvicorn 실행 ──
echo "[entrypoint] Starting uvicorn..."
uvicorn apps.api.main:app --host 0.0.0.0 --port "${PORT:-8080}" &
APP_PID=$!
wait "$APP_PID"
