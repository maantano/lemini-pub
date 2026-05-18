#!/usr/bin/env bash
# Upload law artifacts to GCS so that CI/CD can include them in Docker builds.
# Usage: bash scripts/upload-artifacts-gcs.sh [BUCKET_NAME]
#
# Prerequisites: gcloud auth login && gcloud config set project <PROJECT_ID>

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_DIR="$REPO_ROOT/data/artifacts"
BUCKET="${1:-lemini-law-artifacts}"
PROJECT_ID="${PROJECT_ID:-<your-gcp-project>}"

REQUIRED_FILES=(
  "laws.sqlite"
  "article_embeddings.npy"
  "article_embedding_ids.json"
  "manifest.json"
)

# Validate local artifacts exist
for file in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "$ARTIFACT_DIR/$file" ]]; then
    echo "ERROR: Missing $ARTIFACT_DIR/$file — run ingest first."
    exit 1
  fi
done

# Create bucket if it doesn't exist
if ! gsutil ls "gs://$BUCKET" &>/dev/null; then
  echo "Creating GCS bucket: gs://$BUCKET"
  gsutil mb -p "$PROJECT_ID" -l asia-northeast3 "gs://$BUCKET"
fi

# Upload artifacts
echo "Uploading artifacts to gs://$BUCKET/ ..."
for file in "${REQUIRED_FILES[@]}"; do
  echo "  $file ($(du -h "$ARTIFACT_DIR/$file" | cut -f1))"
  gsutil -m cp "$ARTIFACT_DIR/$file" "gs://$BUCKET/$file"
done

echo ""
echo "Upload complete. GCS contents:"
gsutil ls -lh "gs://$BUCKET/"
