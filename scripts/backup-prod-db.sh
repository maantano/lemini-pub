#!/usr/bin/env bash
#
# 프로덕션 laws.sqlite 를 GCS 에 타임스탬프 백업.
# 반영 전 반드시 실행. 중복 백업은 스킵.
#
# 사용법:
#   bash scripts/backup-prod-db.sh
#
# 결과:
#   gs://lemini-law-artifacts-backups/laws.sqlite.YYYY-MM-DD-HHmm.bak
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ID="${PROJECT_ID:-<your-gcp-project>}"
BACKUP_BUCKET="${BACKUP_BUCKET:-lemini-law-artifacts-backups}"
SRC="${SRC:-$REPO_ROOT/data/artifacts/laws.sqlite}"

TS=$(date -u +%Y-%m-%d-%H%M)
DEST="gs://${BACKUP_BUCKET}/laws.sqlite.${TS}.bak"

if [[ ! -f "$SRC" ]]; then
  echo "ERROR: 원본 없음: $SRC" >&2
  exit 1
fi

echo "[1/4] 백업 대상: $SRC ($(du -h "$SRC" | cut -f1))"

# 버킷 생성 (idempotent)
if ! gcloud storage buckets describe "gs://${BACKUP_BUCKET}" --project="$PROJECT_ID" &>/dev/null; then
  echo "[2/4] 버킷 생성: gs://${BACKUP_BUCKET}"
  gcloud storage buckets create "gs://${BACKUP_BUCKET}" \
    --project="$PROJECT_ID" \
    --location="asia-northeast3" \
    --uniform-bucket-level-access
else
  echo "[2/4] 버킷 이미 존재: gs://${BACKUP_BUCKET}"
fi

# 로컬 해시
local_hash=$(shasum -a 256 "$SRC" | awk '{print $1}')
echo "[3/4] 로컬 해시: ${local_hash:0:16}..."

# 최신 백업 해시와 비교 (중복 백업 방지)
latest=$(gcloud storage ls "gs://${BACKUP_BUCKET}/" 2>/dev/null | sort | tail -1 || true)
if [[ -n "${latest:-}" ]]; then
  tmp=$(mktemp)
  gcloud storage cp "$latest" "$tmp" --project="$PROJECT_ID" --quiet 2>/dev/null || true
  if [[ -s "$tmp" ]]; then
    remote_hash=$(shasum -a 256 "$tmp" | awk '{print $1}')
    if [[ "$local_hash" == "$remote_hash" ]]; then
      echo "[-] 동일한 DB 이미 백업되어 있음: $latest"
      rm -f "$tmp"
      exit 0
    fi
  fi
  rm -f "$tmp"
fi

# 업로드 + 해시를 메타데이터에 기록
echo "[4/4] 업로드: $DEST"
gcloud storage cp "$SRC" "$DEST" \
  --project="$PROJECT_ID" \
  --custom-metadata="sha256=${local_hash},backup-time=${TS}"

# retention: 10 개 초과분 삭제
all_backups=$(gcloud storage ls "gs://${BACKUP_BUCKET}/laws.sqlite.*.bak" --project="$PROJECT_ID" 2>/dev/null | sort)
total=$(echo "$all_backups" | wc -l | tr -d ' ')
if [[ "$total" -gt 10 ]]; then
  to_delete=$(echo "$all_backups" | head -n $((total - 10)))
  echo "[retention] 오래된 백업 $((total - 10))건 삭제 중..."
  echo "$to_delete" | while read -r b; do
    [[ -n "$b" ]] && gcloud storage rm "$b" --project="$PROJECT_ID" --quiet
  done
fi

echo "✅ 백업 완료: $DEST"
