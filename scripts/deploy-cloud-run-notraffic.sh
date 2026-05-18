#!/usr/bin/env bash
#
# 프로덕션 lemini-api 를 --no-traffic 으로 배포 (새 revision 생성, 트래픽 0%).
# deploy-cloud-run.sh 와 동일한 빌드·배포 로직이지만 --no-traffic + --tag staging 추가.
#
# 사용:
#   bash scripts/deploy-cloud-run-notraffic.sh
#
# 결과:
#   - 새 revision 생성 (예: lemini-api-00033-xxx)
#   - 기존 revision 이 100% traffic 유지
#   - 새 revision 은 staging 태그 URL 로 접근 가능
#
# 롤아웃:
#   gcloud run services update-traffic lemini-api --to-latest --region asia-northeast3
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-lemini-api}"
REGION="${REGION:-asia-northeast3}"
PROJECT_ID="${PROJECT_ID:-<your-gcp-project>}"
TAG_NAME="${TAG_NAME:-staging}"

if [[ ! -f "$REPO_ROOT/data/artifacts/laws.sqlite" ]]; then
  echo "ERROR: data/artifacts/laws.sqlite 없음" >&2
  exit 1
fi

# 기존 lemini-api 서비스의 image registry 경로 재사용
EXISTING=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" \
  --project "$PROJECT_ID" \
  --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)
if [[ -z "$EXISTING" ]]; then
  echo "ERROR: $SERVICE_NAME 서비스 없음" >&2
  exit 1
fi

# sha256 digest 및 기존 :tag 제거 → 새 :tag 로 푸시
IMAGE_BASE=$(echo "$EXISTING" | sed 's/@sha256:.*//' | sed 's/:[^/]*$//')
TS=$(date -u +%Y%m%d-%H%M)
IMAGE="${IMAGE_BASE}:v${TS}"

echo "[1/3] Docker 이미지 빌드 + 푸시: $IMAGE"
gcloud builds submit "$REPO_ROOT" \
  --tag "$IMAGE" \
  --project "$PROJECT_ID" \
  --timeout 1800s

echo
echo "[2/3] Cloud Run 새 revision 배포 (--no-traffic, tag=$TAG_NAME)"
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --platform managed \
  --no-traffic \
  --tag "$TAG_NAME"

echo
echo "[3/3] 스테이징 URL 조회"
STAGING_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" --project "$PROJECT_ID" \
  --format="value(status.traffic[?(@.tag=='$TAG_NAME')].url)" 2>/dev/null | head -1)
echo
echo "✅ 배포 완료 (트래픽 0%)"
echo
echo "   스테이징 URL: $STAGING_URL"
