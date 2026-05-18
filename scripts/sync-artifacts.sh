#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="$REPO_ROOT/data/artifacts"
SOURCE_DIR="${1:-}"

if [[ -z "$SOURCE_DIR" ]]; then
  echo "Usage: bash scripts/sync-artifacts.sh /path/to/artifact-dir"
  exit 1
fi

if [[ ! -d "$SOURCE_DIR" ]]; then
  echo "Artifact source directory not found: $SOURCE_DIR"
  exit 1
fi

REQUIRED_FILES=(
  "laws.sqlite"
  "article_embeddings.npy"
  "article_embedding_ids.json"
  "manifest.json"
)

for file in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "$SOURCE_DIR/$file" ]]; then
    echo "Missing required artifact file: $SOURCE_DIR/$file"
    exit 1
  fi
done

mkdir -p "$TARGET_DIR"

for file in "${REQUIRED_FILES[@]}" "state.sqlite"; do
  if [[ -f "$SOURCE_DIR/$file" ]]; then
    cp "$SOURCE_DIR/$file" "$TARGET_DIR/$file"
  fi
done

echo "Synced artifacts into $TARGET_DIR"
ls -lh "$TARGET_DIR"
