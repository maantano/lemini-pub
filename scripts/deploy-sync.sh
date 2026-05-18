#!/usr/bin/env bash
#
# Lemini sync 파이프라인 배포 스크립트
#
# 사용법:
#   bash scripts/deploy-sync.sh all            # 전체 단계 순차 (처음 배포)
#   bash scripts/deploy-sync.sh apis           # API 활성화
#   bash scripts/deploy-sync.sh sa             # 서비스 계정 생성 + IAM
#   bash scripts/deploy-sync.sh buckets        # GCS 버킷 생성
#   bash scripts/deploy-sync.sh secrets        # Secret Manager 등록
#   bash scripts/deploy-sync.sh build          # Docker 이미지 빌드
#   bash scripts/deploy-sync.sh jobs           # Cloud Run Job 생성/업데이트
#   bash scripts/deploy-sync.sh scheduler      # Cloud Scheduler 등록
#   bash scripts/deploy-sync.sh test           # Job 수동 실행 (smoke test)
#   bash scripts/deploy-sync.sh status         # 현재 배포 상태 조회
#
# 환경변수:
#   PROJECT_ID              (기본: <your-gcp-project>)
#   REGION                  (기본: asia-northeast3)
#   LAW_OC_VALUE            Secret 등록 시 필요 (현재 값: .env 의 LAW_API_KEY)
#   SLACK_BOT_TOKEN         Secret 등록 시 필요 (기존 lemini-slack-bot 토큰 재사용)
#   SLACK_ALERT_CHANNEL     Job 환경변수 (예: #lemini-sync-alerts)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PROJECT_ID="${PROJECT_ID:-<your-gcp-project>}"
REGION="${REGION:-asia-northeast3}"
SA_NAME="lemini-data-sync"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

DATA_BUCKET="lemini-sync-data"
CACHE_BUCKET="lemini-sync-cache"

LAW_OC_SECRET="lemini-law-oc"
SLACK_TOKEN_SECRET="lemini-slack-bot-token"

IMAGE="gcr.io/${PROJECT_ID}/lemini-data-sync:latest"

JOB_DATA_SYNC="lemini-data-sync"
JOB_COLLECTOR_GREEN="lemini-collector-green"

SCHEDULER_DATA_SYNC="data-sync-weekly"
SCHEDULER_COLLECTOR="collector-green-monthly"

# 일요일 02:00 KST = 토 17:00 UTC
CRON_DATA_SYNC="0 17 * * 6"
# 매월 2일 02:00 KST = 매월 1일 17:00 UTC
CRON_COLLECTOR="0 17 1 * *"

log() { echo "[$(date +%H:%M:%S)] $*"; }

step_apis() {
  log "APIs 활성화 중 ..."
  gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    cloudscheduler.googleapis.com \
    storage.googleapis.com \
    --project "$PROJECT_ID"
  log "APIs 활성화 완료."
}

step_sa() {
  log "서비스 계정 확인/생성 ..."
  if ! gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" &>/dev/null; then
    gcloud iam service-accounts create "$SA_NAME" \
      --display-name="Lemini sync pipeline" \
      --project "$PROJECT_ID"
    log "서비스 계정 생성 완료: $SA_EMAIL"
  else
    log "서비스 계정 이미 존재: $SA_EMAIL"
  fi

  # IAM 역할 바인딩 (project-level)
  for role in \
    "roles/secretmanager.secretAccessor" \
    "roles/storage.objectAdmin" \
    "roles/run.invoker" \
    "roles/logging.logWriter"
  do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:${SA_EMAIL}" \
      --role="$role" \
      --condition=None \
      --quiet >/dev/null
    log "IAM 바인딩: $role"
  done
}

step_buckets() {
  log "GCS 버킷 확인/생성 ..."
  for bucket in "$DATA_BUCKET" "$CACHE_BUCKET"; do
    if ! gcloud storage buckets describe "gs://${bucket}" --project "$PROJECT_ID" &>/dev/null; then
      gcloud storage buckets create "gs://${bucket}" \
        --project "$PROJECT_ID" \
        --location "$REGION" \
        --uniform-bucket-level-access
      log "버킷 생성: gs://${bucket}"
    else
      log "버킷 이미 존재: gs://${bucket}"
    fi
    # Versioning OFF (SSOT 외부, 중복 방지 불필요)
    gcloud storage buckets update "gs://${bucket}" --no-versioning --project "$PROJECT_ID" --quiet >/dev/null
  done
}

step_secrets() {
  log "Secret Manager 확인/등록 ..."

  # LAW_OC
  if [[ -z "${LAW_OC_VALUE:-}" ]]; then
    # .env 에서 자동 로드 시도
    if [[ -f "$REPO_ROOT/.env" ]]; then
      LAW_OC_VALUE=$(grep -E '^LAW_OC=|^LAW_API_KEY=' "$REPO_ROOT/.env" | head -1 | cut -d'=' -f2 | tr -d '"'"'"'' | tr -d '\r\n' || true)
    fi
  fi
  if [[ -z "${LAW_OC_VALUE:-}" ]]; then
    echo "ERROR: LAW_OC_VALUE 환경변수 또는 .env 의 LAW_OC/LAW_API_KEY 를 찾을 수 없음"
    exit 1
  fi

  if ! gcloud secrets describe "$LAW_OC_SECRET" --project "$PROJECT_ID" &>/dev/null; then
    echo -n "$LAW_OC_VALUE" | gcloud secrets create "$LAW_OC_SECRET" \
      --data-file=- --replication-policy=automatic --project "$PROJECT_ID"
    log "Secret 생성: $LAW_OC_SECRET"
  else
    # 값이 다른 경우만 새 버전 추가
    current=$(gcloud secrets versions access latest --secret="$LAW_OC_SECRET" --project "$PROJECT_ID" 2>/dev/null || echo "")
    if [[ "$current" != "$LAW_OC_VALUE" ]]; then
      echo -n "$LAW_OC_VALUE" | gcloud secrets versions add "$LAW_OC_SECRET" \
        --data-file=- --project "$PROJECT_ID"
      log "Secret 업데이트: $LAW_OC_SECRET (new version)"
    else
      log "Secret 변경 없음: $LAW_OC_SECRET"
    fi
  fi

  # SLACK_BOT_TOKEN 은 기존 lemini-slack-bot 에서 이미 쓰고 있을 가능성 큼 — 검사만
  if gcloud secrets describe "$SLACK_TOKEN_SECRET" --project "$PROJECT_ID" &>/dev/null; then
    log "Slack token secret 이미 존재: $SLACK_TOKEN_SECRET (재사용)"
  else
    if [[ -z "${SLACK_BOT_TOKEN:-}" ]]; then
      echo "WARN: SLACK_BOT_TOKEN secret 없음. 환경변수 SLACK_BOT_TOKEN 제공하거나 추후 수동 등록 필요."
    else
      echo -n "$SLACK_BOT_TOKEN" | gcloud secrets create "$SLACK_TOKEN_SECRET" \
        --data-file=- --replication-policy=automatic --project "$PROJECT_ID"
      log "Secret 생성: $SLACK_TOKEN_SECRET"
    fi
  fi
}

step_build() {
  log "Docker 이미지 빌드 + 푸시 ..."
  gcloud builds submit \
    --config="$REPO_ROOT/cloudbuild.sync.yaml" \
    --project "$PROJECT_ID" \
    "$REPO_ROOT"
  log "이미지 푸시 완료: $IMAGE"
}

_upsert_job() {
  local job_name="$1"
  local target="$2"
  local timeout="$3"

  local env_vars="WORKSPACE_ROOT=/tmp/sync,GCS_DATA_BUCKET=${DATA_BUCKET},GCS_CACHE_BUCKET=${CACHE_BUCKET},SLACK_ALERT_CHANNEL=${SLACK_ALERT_CHANNEL:-#lemini-sync-alerts}"
  local secrets="LAW_OC=${LAW_OC_SECRET}:latest"
  if gcloud secrets describe "$SLACK_TOKEN_SECRET" --project "$PROJECT_ID" &>/dev/null; then
    secrets="${secrets},SLACK_BOT_TOKEN=${SLACK_TOKEN_SECRET}:latest"
  fi

  if gcloud run jobs describe "$job_name" --region "$REGION" --project "$PROJECT_ID" &>/dev/null; then
    log "Job update: $job_name"
    gcloud run jobs update "$job_name" \
      --image "$IMAGE" \
      --region "$REGION" \
      --project "$PROJECT_ID" \
      --service-account "$SA_EMAIL" \
      --cpu 2 --memory 4Gi \
      --max-retries 1 \
      --task-timeout "$timeout" \
      --args="--target=${target}" \
      --set-env-vars="$env_vars" \
      --set-secrets="$secrets"
  else
    log "Job create: $job_name"
    gcloud run jobs create "$job_name" \
      --image "$IMAGE" \
      --region "$REGION" \
      --project "$PROJECT_ID" \
      --service-account "$SA_EMAIL" \
      --cpu 2 --memory 4Gi \
      --max-retries 1 \
      --task-timeout "$timeout" \
      --args="--target=${target}" \
      --set-env-vars="$env_vars" \
      --set-secrets="$secrets"
  fi
}

step_jobs() {
  log "Cloud Run Jobs upsert ..."
  _upsert_job "$JOB_DATA_SYNC" "all" "3600s"
  _upsert_job "$JOB_COLLECTOR_GREEN" "voluntary" "1800s"
}

_upsert_scheduler() {
  local name="$1"
  local cron="$2"
  local job_name="$3"

  local uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${job_name}:run"
  local audience="https://${REGION}-run.googleapis.com/"

  if gcloud scheduler jobs describe "$name" --location "$REGION" --project "$PROJECT_ID" &>/dev/null; then
    log "Scheduler update: $name"
    gcloud scheduler jobs update http "$name" \
      --location "$REGION" --project "$PROJECT_ID" \
      --schedule="$cron" --time-zone="UTC" \
      --uri="$uri" --http-method=POST \
      --oauth-service-account-email "$SA_EMAIL" \
      --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
  else
    log "Scheduler create: $name"
    gcloud scheduler jobs create http "$name" \
      --location "$REGION" --project "$PROJECT_ID" \
      --schedule="$cron" --time-zone="UTC" \
      --uri="$uri" --http-method=POST \
      --oauth-service-account-email "$SA_EMAIL" \
      --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
  fi
}

step_scheduler() {
  log "Cloud Scheduler upsert ..."
  _upsert_scheduler "$SCHEDULER_DATA_SYNC" "$CRON_DATA_SYNC" "$JOB_DATA_SYNC"
  _upsert_scheduler "$SCHEDULER_COLLECTOR" "$CRON_COLLECTOR" "$JOB_COLLECTOR_GREEN"
}

step_test() {
  log "Cloud Run Job 수동 실행 테스트 ..."
  gcloud run jobs execute "$JOB_DATA_SYNC" \
    --region "$REGION" --project "$PROJECT_ID" \
    --wait --args="--target=laws,--days=3,--dry-run"
  log "테스트 실행 완료. Cloud Logging 에서 로그 확인."
}

step_status() {
  log "=== 배포 상태 ==="
  echo
  echo "[Project] $PROJECT_ID / Region: $REGION"
  echo
  echo "[Service Account]"
  gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" --format="value(email,disabled)" 2>/dev/null || echo "  ❌ 없음"
  echo
  echo "[Buckets]"
  for b in "$DATA_BUCKET" "$CACHE_BUCKET"; do
    gcloud storage buckets describe "gs://${b}" --project "$PROJECT_ID" --format="value(name)" 2>/dev/null || echo "  ❌ gs://${b} 없음"
  done
  echo
  echo "[Secrets]"
  for s in "$LAW_OC_SECRET" "$SLACK_TOKEN_SECRET"; do
    gcloud secrets describe "$s" --project "$PROJECT_ID" --format="value(name)" 2>/dev/null || echo "  ❌ $s 없음"
  done
  echo
  echo "[Cloud Run Jobs]"
  for j in "$JOB_DATA_SYNC" "$JOB_COLLECTOR_GREEN"; do
    gcloud run jobs describe "$j" --region "$REGION" --project "$PROJECT_ID" --format="value(name)" 2>/dev/null || echo "  ❌ $j 없음"
  done
  echo
  echo "[Schedulers]"
  for s in "$SCHEDULER_DATA_SYNC" "$SCHEDULER_COLLECTOR"; do
    gcloud scheduler jobs describe "$s" --location "$REGION" --project "$PROJECT_ID" --format="value(name,schedule)" 2>/dev/null || echo "  ❌ $s 없음"
  done
}

step_all() {
  step_apis
  step_sa
  step_buckets
  step_secrets
  step_build
  step_jobs
  step_scheduler
  step_status
  echo
  log "전체 배포 완료. 수동 테스트: bash $(basename "$0") test"
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    all)        step_all ;;
    apis)       step_apis ;;
    sa)         step_sa ;;
    buckets)    step_buckets ;;
    secrets)    step_secrets ;;
    build)      step_build ;;
    jobs)       step_jobs ;;
    scheduler)  step_scheduler ;;
    test)       step_test ;;
    status)     step_status ;;
    *)
      grep -E '^#' "$0" | head -25
      exit 1
      ;;
  esac
}

main "$@"
