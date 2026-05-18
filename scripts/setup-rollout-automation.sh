#!/usr/bin/env bash
#
# M3-Z 자동 반영 파이프라인 셋업.
# Cloud Build Trigger + Cloud Scheduler 로 주간 자동 rollout.
#
# 실행 흐름 (배포 후):
#   매주 일요일 03:00 KST
#     → Scheduler 트리거
#     → Cloud Build rollout (cloudbuild.rollout.yaml)
#     → GCS 수집 결과 → DB 재생성 → 새 이미지 빌드 → --no-traffic → smoke → 100%
#     → 실패 시 Slack 알림 (기존 slack-bot 재사용)
#
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-<your-gcp-project>}"
REGION="${REGION:-asia-northeast3}"
TRIGGER_NAME="${TRIGGER_NAME:-lemini-rollout}"
SCHEDULER_NAME="${SCHEDULER_NAME:-lemini-rollout-weekly}"
SA_EMAIL="${SA_EMAIL:-lemini-data-sync@${PROJECT_ID}.iam.gserviceaccount.com}"

# 매주 일요일 03:00 KST = 토 18:00 UTC (sync Job 02:00 KST 끝난 1시간 후)
CRON="0 18 * * 6"

echo "=== M3-Z 자동 반영 파이프라인 셋업 ==="

echo
echo "[1/3] Cloud Build Trigger (manual)"
if gcloud builds triggers describe "$TRIGGER_NAME" --project="$PROJECT_ID" --region=global 2>/dev/null; then
  echo "  trigger 이미 존재 — config 만 업데이트"
  gcloud builds triggers update manual "$TRIGGER_NAME" \
    --project="$PROJECT_ID" --region=global \
    --build-config=cloudbuild.rollout.yaml \
    --repo-type=GITHUB \
    --repo=https://github.com/maantano/kr-law-rag-mvp 2>/dev/null || true
else
  # GitHub 연동 없이 로컬 빌드 사용할 경우 — inline source (간단히 trigger 만 만듦)
  echo "  trigger 새로 생성..."
  gcloud builds triggers create manual \
    --project="$PROJECT_ID" --region=global \
    --name="$TRIGGER_NAME" \
    --build-config=cloudbuild.rollout.yaml \
    --inline-config 2>&1 || {
      echo "  NOTE: manual trigger 직접 생성이 실패한 경우, GitHub 연동 후 재시도하거나"
      echo "        로컬에서 'gcloud builds submit --config=cloudbuild.rollout.yaml' 로 수동 실행"
    }
fi

echo
echo "[2/3] Cloud Scheduler — 주 1회 트리거"

# Cloud Build trigger 호출 API
TRIGGER_URI="https://cloudbuild.googleapis.com/v1/projects/${PROJECT_ID}/triggers/${TRIGGER_NAME}:run"

if gcloud scheduler jobs describe "$SCHEDULER_NAME" --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
  echo "  scheduler update"
  gcloud scheduler jobs update http "$SCHEDULER_NAME" \
    --location="$REGION" --project="$PROJECT_ID" \
    --schedule="$CRON" --time-zone="UTC" \
    --uri="$TRIGGER_URI" --http-method=POST \
    --oauth-service-account-email="$SA_EMAIL" \
    --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform" \
    --message-body='{"branchName":"master"}'
else
  echo "  scheduler create"
  gcloud scheduler jobs create http "$SCHEDULER_NAME" \
    --location="$REGION" --project="$PROJECT_ID" \
    --schedule="$CRON" --time-zone="UTC" \
    --uri="$TRIGGER_URI" --http-method=POST \
    --oauth-service-account-email="$SA_EMAIL" \
    --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform" \
    --message-body='{"branchName":"master"}'
fi

echo
echo "[3/3] 권한 확인"
# lemini-data-sync SA 에 cloudbuild.builds.editor 권한 부여
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/cloudbuild.builds.editor" \
  --condition=None --quiet > /dev/null

# Cloud Run Admin (revision 배포)
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/run.admin" \
  --condition=None --quiet > /dev/null

echo
echo "✅ M3-Z 자동화 셋업 완료"
echo
echo "수동 실행 테스트:"
echo "  gcloud builds triggers run $TRIGGER_NAME --project=$PROJECT_ID --region=global"
echo
echo "다음 자동 실행:"
echo "  매주 일요일 03:00 KST (토 18:00 UTC)"
echo
echo "scheduler 상태 확인:"
echo "  gcloud scheduler jobs describe $SCHEDULER_NAME --location=$REGION --project=$PROJECT_ID"
