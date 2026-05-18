"""Central configuration for laws pipeline.

Adapted from legalize-pipeline (MIT/Apache-2.0). See sync/NOTICES.md.
"""

from ..core.config import (  # noqa: F401 — re-exported
    BACKOFF_BASE_SECONDS,
    CONCURRENT_WORKERS,
    LAW_API_BASE,
    LAW_API_KEY,
    MAX_RETRIES,
    REQUEST_DELAY_SECONDS,
    WORKSPACE_ROOT,
)

# Laws-specific paths
KR_DIR = WORKSPACE_ROOT / "kr"

# Suffixes that indicate a child law (order matters: longest first)
CHILD_SUFFIXES = [
    (" 시행규칙", "시행규칙"),
    (" 시행령", "시행령"),
]

# Fallback filename by 법령구분
TYPE_TO_FILENAME = {
    "헌법": "헌법",
    "법률": "법률",
    "대통령령": "대통령령",
    "총리령": "총리령",
    "부령": "부령",
    "대법원규칙": "대법원규칙",
    "국회규칙": "국회규칙",
    "헌법재판소규칙": "헌법재판소규칙",
    "감사원규칙": "감사원규칙",
    "선거관리위원회규칙": "선거관리위원회규칙",
}
