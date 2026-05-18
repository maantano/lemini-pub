from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

from .settings import get_settings


class ArtifactNotReadyError(RuntimeError):
    """Raised when the read-only law artifact has not been built yet."""


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        if not path.exists():
            raise ArtifactNotReadyError(
                f"Law artifact not found at {path}. Run the ingest CLI to build laws.sqlite first."
            )
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def _apply_state_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS saved_answers (
          id TEXT PRIMARY KEY,
          user_id TEXT,
          question TEXT NOT NULL,
          summary TEXT NOT NULL,
          answer TEXT NOT NULL,
          citations TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS saved_answers_user_created_idx
          ON saved_answers (user_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS feedback (
          id TEXT PRIMARY KEY,
          question_hash TEXT NOT NULL,
          rating TEXT NOT NULL,
          reason TEXT,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS feedback_question_hash_idx
          ON feedback (question_hash, created_at DESC);

        CREATE TABLE IF NOT EXISTS ingest_jobs (
          id TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          source_type TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          stats TEXT NOT NULL,
          error_log TEXT
        );

        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          kakao_id TEXT UNIQUE NOT NULL,
          nickname TEXT NOT NULL,
          profile_image TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS users_kakao_id_idx
          ON users (kakao_id);

        CREATE TABLE IF NOT EXISTS usage_counts (
          id TEXT PRIMARY KEY,
          user_id TEXT,
          session_id TEXT,
          date TEXT NOT NULL,
          count INTEGER NOT NULL DEFAULT 0,
          UNIQUE(user_id, date),
          UNIQUE(session_id, date)
        );

        CREATE INDEX IF NOT EXISTS usage_counts_user_date_idx
          ON usage_counts (user_id, date);
        CREATE INDEX IF NOT EXISTS usage_counts_session_date_idx
          ON usage_counts (session_id, date);

        CREATE TABLE IF NOT EXISTS response_cache (
          question_hash TEXT PRIMARY KEY,
          question TEXT NOT NULL,
          response TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          expires_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS response_cache_expires_idx
          ON response_cache (expires_at);

        CREATE TABLE IF NOT EXISTS precedent_cache (
          keyword TEXT PRIMARY KEY,
          results TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          expires_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS precedent_cache_expires_idx
          ON precedent_cache (expires_at);

        CREATE TABLE IF NOT EXISTS chat_history (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          session_id TEXT,
          thread_id TEXT,
          turn_index INTEGER NOT NULL DEFAULT 0,
          request_type TEXT NOT NULL DEFAULT 'chat',
          question TEXT NOT NULL,
          response TEXT NOT NULL,
          has_attachments INTEGER NOT NULL DEFAULT 0,
          attachment_names TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS chat_history_user_idx
          ON chat_history (user_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS request_events (
          id TEXT PRIMARY KEY,
          event_type TEXT NOT NULL,
          channel TEXT NOT NULL DEFAULT 'web',
          actor_kind TEXT NOT NULL,
          user_id TEXT,
          session_id TEXT,
          metadata TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS request_events_created_idx
          ON request_events (created_at DESC);
        CREATE INDEX IF NOT EXISTS request_events_type_created_idx
          ON request_events (event_type, created_at DESC);
        CREATE INDEX IF NOT EXISTS request_events_user_created_idx
          ON request_events (user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS request_events_session_created_idx
          ON request_events (session_id, created_at DESC);
        """
    )

    # 기존 chat_history 테이블에 새 컬럼이 없으면 추가 (마이그레이션)
    _migrate_cols = [
        ("session_id", "TEXT"),
        ("thread_id", "TEXT"),
        ("turn_index", "INTEGER DEFAULT 0"),
        ("request_type", "TEXT DEFAULT 'chat'"),
        ("has_attachments", "INTEGER DEFAULT 0"),
        ("attachment_names", "TEXT"),
    ]
    cursor = connection.cursor()
    cursor.execute("PRAGMA table_info(chat_history)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    for col_name, col_def in _migrate_cols:
        if col_name not in existing_cols:
            try:
                connection.execute(f"ALTER TABLE chat_history ADD COLUMN {col_name} {col_def}")
            except Exception:
                pass
    cursor.close()

    # 마이그레이션 후 인덱스 생성 (이미 있으면 무시)
    try:
        connection.execute("CREATE INDEX IF NOT EXISTS chat_history_thread_idx ON chat_history (thread_id, turn_index)")
        connection.execute("CREATE INDEX IF NOT EXISTS chat_history_type_idx ON chat_history (request_type, created_at DESC)")
    except Exception:
        pass
    connection.commit()


@contextmanager
def get_law_db_connection(*, readonly: bool = True) -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    connection = _connect(settings.law_db_path, readonly=readonly)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def get_state_db_connection() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    connection = _connect(settings.state_db_path, readonly=False)
    _apply_state_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


def sqlite_row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def sqlite_rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]
