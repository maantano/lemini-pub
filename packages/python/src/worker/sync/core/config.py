"""Shared configuration for sync pipelines.

Adapted from legalize-pipeline (MIT/Apache-2.0). See sync/NOTICES.md.
BOT_AUTHOR 제거(우리는 GCS에 쓰므로 git commit 불필요), 경로는 WORKSPACE_ROOT 환경변수로 추상화.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Paths
# 기본: 레포 루트 하위의 data/sync/ 를 작업 디렉토리로 사용.
# Cloud Run Job에서는 WORKSPACE_ROOT=/workspace로 덮어씀.
_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parents[6]  # config.py → core → sync → worker → src → python → packages → <repo>
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", str(_REPO_ROOT / "data" / "sync")))

# API
LAW_API_BASE = "http://www.law.go.kr/DRF"
LAW_API_KEY = os.environ.get("LAW_OC", os.environ.get("LAW_API_KEY", ""))

# Rate limiting
REQUEST_DELAY_SECONDS = 0.05
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 3.0
CONCURRENT_WORKERS = 20

# GCS (Cloud Run 배포 시 사용). 로컬에서는 빈 문자열이면 파일시스템 모드.
GCS_DATA_BUCKET = os.environ.get("GCS_DATA_BUCKET", "")
GCS_CACHE_BUCKET = os.environ.get("GCS_CACHE_BUCKET", "")
