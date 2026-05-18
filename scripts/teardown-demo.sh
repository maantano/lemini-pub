#!/usr/bin/env bash
# =============================================================================
# Lemini Demo Teardown — 데모 환경 일괄 종료
# =============================================================================
# 이 스크립트는 데모 환경의 GCP 리소스를 모두 정리합니다.
# Cloud Run Job 또는 로컬에서 실행 가능.
#
# 사용:
#   PROJECT_ID=<your-project> REGION=asia-northeast3 ./scripts/teardown-demo.sh
#
# DRY_RUN=1 ./scripts/teardown-demo.sh   # 미리 보기만 (실제 삭제 안 함)
# KEEP_GCS=1 ./scripts/teardown-demo.sh  # GCS 버킷은 보존 (데이터 살림)
# =============================================================================

set -uo pipefail

PROJECT_ID="${PROJECT_ID:?PROJECT_ID required}"
REGION="${REGION:-asia-northeast3}"
DRY_RUN="${DRY_RUN:-0}"
KEEP_GCS="${KEEP_GCS:-0}"

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "[DRY-RUN] $*"
  else
    echo "▶ $*"
    eval "$@" || echo "  ⚠️ failed (계속 진행)"
  fi
}

echo "==================================================="
echo "Lemini Demo Teardown"
echo "Project: $PROJECT_ID"
echo "Region:  $REGION"
echo "Dry run: $DRY_RUN"
echo "Keep GCS: $KEEP_GCS"
echo "==================================================="

echo ""
echo "### 1. Cloud Run 서비스 삭제"
run "gcloud run services delete lemini-api --region=$REGION --quiet --project=$PROJECT_ID"
run "gcloud run services delete lemini-slack-bot --region=$REGION --quiet --project=$PROJECT_ID"

echo ""
echo "### 2. Cloud Run Jobs 삭제"
run "gcloud run jobs delete lemini-data-sync --region=$REGION --quiet --project=$PROJECT_ID"
run "gcloud run jobs delete lemini-collector-green --region=$REGION --quiet --project=$PROJECT_ID"

echo ""
echo "### 3. Cloud Scheduler 삭제"
run "gcloud scheduler jobs delete data-sync-weekly --location=$REGION --quiet --project=$PROJECT_ID"
run "gcloud scheduler jobs delete collector-green-monthly --location=$REGION --quiet --project=$PROJECT_ID"
run "gcloud scheduler jobs delete lemini-rollout-weekly --location=$REGION --quiet --project=$PROJECT_ID"

echo ""
echo "### 4. 네트워크 리소스 (VPC Connector, NAT, Router, IP)"
run "gcloud compute routers nats delete lemini-nat --router=lemini-router --region=$REGION --quiet --project=$PROJECT_ID"
run "gcloud compute routers delete lemini-router --region=$REGION --quiet --project=$PROJECT_ID"
run "gcloud compute networks vpc-access connectors delete lemini-connector --region=$REGION --quiet --project=$PROJECT_ID"
run "gcloud compute addresses delete lemini-static-ip --region=$REGION --quiet --project=$PROJECT_ID"

echo ""
echo "### 5. Secret Manager"
run "gcloud secrets delete lemini-law-oc --quiet --project=$PROJECT_ID"
run "gcloud secrets delete lemini-slack-bot-token --quiet --project=$PROJECT_ID"

echo ""
echo "### 6. Artifact Registry 이미지"
run "gcloud artifacts repositories delete cloud-run-source-deploy --location=$REGION --quiet --project=$PROJECT_ID"

if [ "$KEEP_GCS" = "1" ]; then
  echo ""
  echo "### 7. GCS 버킷 — 보존 (KEEP_GCS=1)"
else
  echo ""
  echo "### 7. GCS 버킷 삭제"
  for bucket in lemini-law-artifacts lemini-state-db lemini-sync-cache lemini-sync-data; do
    run "gcloud storage rm -r gs://$bucket --quiet --project=$PROJECT_ID"
  done
fi

echo ""
echo "### 8. 마지막 — 자기 자신(이 스케줄러 잡) 삭제"
run "gcloud scheduler jobs delete lemini-auto-teardown --location=$REGION --quiet --project=$PROJECT_ID"

echo ""
echo "==================================================="
echo "✅ Teardown complete."
echo "남은 비용: GCS 보존 시 월 ~100원, 전부 삭제 시 0원"
echo "==================================================="
