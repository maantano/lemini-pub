#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-kr-law-rag-api}"
REGION="${REGION:-asia-northeast3}"
PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "Set PROJECT_ID or GOOGLE_CLOUD_PROJECT before running this script."
  exit 1
fi

if [[ ! -f "$REPO_ROOT/data/artifacts/laws.sqlite" ]]; then
  echo "Missing $REPO_ROOT/data/artifacts/laws.sqlite"
  echo "Run: bash scripts/sync-artifacts.sh /path/to/artifact-dir"
  exit 1
fi

IMAGE="${IMAGE:-gcr.io/$PROJECT_ID/$SERVICE_NAME}"

ENV_VARS=(
  "APP_ENV=prod"
  "ARTIFACT_DIR=/app/data/artifacts"
  "ENABLE_SERVER_CHAT_HISTORY=false"
  "ENABLE_FULL_RETRIEVAL_LOG=false"
  "ENABLE_ARTICLE_SEGMENT=false"
  "ENABLE_APPENDIX_SEARCH=false"
)

OPTIONAL_KEYS=(
  "ADMIN_API_KEY"
  "GEMINI_API_KEY"
  "GEMINI_MODEL"
  "GEMINI_EMBEDDING_MODEL"
  "EMBEDDING_DIM"
  "RETRIEVAL_TOP_K"
  "RETRIEVAL_MIN_SCORE"
  "CORS_ALLOW_ORIGINS"
)

for key in "${OPTIONAL_KEYS[@]}"; do
  value="${!key:-}"
  if [[ -n "$value" ]]; then
    ENV_VARS+=("$key=$value")
  fi
done

echo "Building container image: $IMAGE"
gcloud builds submit "$REPO_ROOT" --tag "$IMAGE" --file "$REPO_ROOT/apps/api/Dockerfile"

echo "Deploying Cloud Run service: $SERVICE_NAME ($REGION)"
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 3 \
  --set-env-vars "$(IFS=,; echo "${ENV_VARS[*]}")"
