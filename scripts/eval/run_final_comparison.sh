#!/usr/bin/env bash
# eval v3 완료 후 자동 수행: 구조 지표 → baseline 비교 → 샘플 diff
# 사용: bash scripts/eval/run_final_comparison.sh <new_run_id>
set -e
NEW_RUN="${1:-2026-04-22T15-26-54}"
OLD_RUN="2026-04-22T11-07-34"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$ROOT/.venv/bin/python"

cd "$ROOT"

echo "=== 1. v3 structural_metrics ==="
"$PY" scripts/eval/structural_metrics.py "$NEW_RUN"
echo

echo "=== 2. baseline(v1) 재측정 (promulgation_no 컬럼 반영된 DB로) ==="
"$PY" scripts/eval/structural_metrics.py "$OLD_RUN"
echo

echo "=== 3. compare_runs_text — 구조 지표 + 샘플 5건 diff ==="
"$PY" scripts/eval/compare_runs_text.py "$OLD_RUN" "$NEW_RUN" --sample 5
