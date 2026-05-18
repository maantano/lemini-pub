"""precedent_doc_cache 읽기/쓰기 헬퍼.

IngestService 가 쓰는 laws.sqlite 와 같은 파일을 공유하며,
DB 파일 위치는 ``law_rag_core.settings.law_db_path`` 를 따른다.

Cloud Run Job 실행 시: WORKSPACE_ROOT/artifacts/laws.sqlite 를 직접 연다.
로컬 개발 시: ARTIFACT_DIR 환경변수에 맞춰 동작.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


def _db_path() -> Path:
    """laws.sqlite 경로 — 기존 law_rag_core.settings 와 동일한 로직."""
    artifact_dir = os.environ.get("ARTIFACT_DIR")
    if artifact_dir:
        return Path(artifact_dir) / "laws.sqlite"
    # 레포 기본값
    repo_root = Path(__file__).resolve().parents[6]
    return repo_root / "data" / "artifacts" / "laws.sqlite"


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    if not path.exists():
        raise RuntimeError(
            f"laws.sqlite not found at {path}. "
            "먼저 IngestService 로 스키마를 생성하거나 프로덕션 DB 를 준비하라."
        )
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def exists(precedent_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM precedent_doc_cache WHERE precedent_id = ? LIMIT 1",
            (str(precedent_id),),
        ).fetchone()
    return row is not None


def upsert(detail: dict[str, Any], *, source: str = "drf-api") -> bool:
    """판례 상세를 DB 에 저장/갱신. 변경 감지는 content_hash 기반.

    Returns:
        True if inserted or updated, False if unchanged.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    source_url = f"https://www.law.go.kr/판례/({detail['precedent_id']})"

    # content_hash 계산 (변경 감지용)
    hash_src = "|".join([
        detail.get("case_no", ""),
        detail.get("case_name", ""),
        detail.get("holding", ""),
        detail.get("summary", ""),
        detail.get("body", ""),
    ])
    content_hash = _hash(hash_src)

    with _conn() as c:
        existing = c.execute(
            "SELECT content_hash FROM precedent_doc_cache WHERE precedent_id = ?",
            (detail["precedent_id"],),
        ).fetchone()

        if existing and existing[0] == content_hash:
            return False  # unchanged

        c.execute(
            """
            INSERT INTO precedent_doc_cache (
                precedent_id, title, court, case_no, judgment_date, case_type,
                body, fetched_at, source_url,
                holding, summary, referenced_statutes, referenced_cases,
                judgment_type, court_type_code, case_type_code, source, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(precedent_id) DO UPDATE SET
                title = excluded.title,
                court = excluded.court,
                case_no = excluded.case_no,
                judgment_date = excluded.judgment_date,
                case_type = excluded.case_type,
                body = excluded.body,
                fetched_at = excluded.fetched_at,
                source_url = excluded.source_url,
                holding = excluded.holding,
                summary = excluded.summary,
                referenced_statutes = excluded.referenced_statutes,
                referenced_cases = excluded.referenced_cases,
                judgment_type = excluded.judgment_type,
                court_type_code = excluded.court_type_code,
                case_type_code = excluded.case_type_code,
                source = excluded.source,
                content_hash = excluded.content_hash
            """,
            (
                detail["precedent_id"],
                detail.get("case_name", ""),
                detail.get("court", ""),
                detail.get("case_no", ""),
                detail.get("judgment_date", ""),
                detail.get("case_type", ""),
                detail.get("body", ""),
                now,
                source_url,
                detail.get("holding", ""),
                detail.get("summary", ""),
                detail.get("referenced_statutes", ""),
                detail.get("referenced_cases", ""),
                detail.get("judgment_type", ""),
                detail.get("court_type_code", ""),
                detail.get("case_type_code", ""),
                source,
                content_hash,
            ),
        )
    return True


def get(precedent_id: str) -> dict[str, Any] | None:
    with _conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM precedent_doc_cache WHERE precedent_id = ?",
            (str(precedent_id),),
        ).fetchone()
    return dict(row) if row else None


def count(source: str | None = None) -> int:
    with _conn() as c:
        if source:
            row = c.execute(
                "SELECT COUNT(*) FROM precedent_doc_cache WHERE source = ?",
                (source,),
            ).fetchone()
        else:
            row = c.execute("SELECT COUNT(*) FROM precedent_doc_cache").fetchone()
    return int(row[0]) if row else 0
