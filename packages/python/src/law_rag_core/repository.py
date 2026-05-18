from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any

from .db import (
    get_law_db_connection,
    get_state_db_connection,
    sqlite_row_to_dict,
    sqlite_rows_to_dicts,
)
from .normalization import normalize_for_search
from .settings import get_settings
from .types import AdminStats, FeedbackCreate, SavedAnswerCreate, SavedAnswerRecord, UserRecord


REQUEST_EVENT_LABELS: dict[str, str] = {
    "auth_login": "로그인",
    "chat_answer": "일반 답변",
    "deep_dive_follow_up": "심층 추가질문",
    "deep_dive_analysis": "심층 분석",
    "review_text": "텍스트 검토",
    "review_file": "파일 검토",
    "document_draft": "서류 초안",
    "feedback": "피드백",
    "search_laws": "법령 검색",
    "search_precedents": "판례 검색",
}

REQUEST_EVENT_ORDER = [
    "chat_answer",
    "deep_dive_follow_up",
    "deep_dive_analysis",
    "review_text",
    "review_file",
    "document_draft",
    "search_laws",
    "search_precedents",
    "feedback",
    "auth_login",
]


def _request_event_summary(event_type: str, metadata: dict[str, Any]) -> str:
    if event_type == "auth_login":
        return "신규 가입" if metadata.get("is_new") else "기존 사용자 로그인"
    if event_type == "chat_answer":
        parts = []
        if metadata.get("grounded"):
            parts.append("근거 답변")
        if metadata.get("citations"):
            parts.append(f"인용 {metadata['citations']}건")
        if metadata.get("precedents"):
            parts.append(f"판례 {metadata['precedents']}건")
        return " · ".join(parts)
    if event_type == "deep_dive_follow_up":
        questions = metadata.get("question_count")
        return f"후속 질문 {questions}개" if isinstance(questions, int) and questions > 0 else "추가 사실관계 수집"
    if event_type == "deep_dive_analysis":
        return f"인용 {metadata.get('citations', 0)}건 · 판례 {metadata.get('precedents', 0)}건"
    if event_type == "review_text":
        text_length = metadata.get("text_length")
        return f"본문 {text_length:,}자" if isinstance(text_length, int) else "텍스트 검토 완료"
    if event_type == "review_file":
        file_count = metadata.get("file_count")
        extracted_length = metadata.get("total_extracted_length")
        parts = []
        if isinstance(file_count, int):
            parts.append(f"파일 {file_count}개")
        if isinstance(extracted_length, int):
            parts.append(f"추출 {extracted_length:,}자")
        return " · ".join(parts)
    if event_type == "document_draft":
        return str(metadata.get("document_type") or "서류 초안 생성")
    if event_type == "feedback":
        return f"평가: {metadata.get('rating') or '-'}"
    if event_type in {"search_laws", "search_precedents"}:
        result_count = metadata.get("result_count")
        return f"결과 {result_count}건" if isinstance(result_count, int) else "검색 실행"
    return ""


class Repository:
    """데이터 접근 계층 — 법령 DB(laws.sqlite)와 상태 DB(state.sqlite)에 대한 모든 CRUD를 담당한다."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def _local_now(self) -> datetime:
        return datetime.now(self.settings.timezone_info)

    def _local_date_key(self, value: datetime | None = None) -> str:
        return (value or self._local_now()).strftime("%Y-%m-%d")

    def _parse_timestamp(self, value: str | None) -> datetime | None:
        if not value:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(self.settings.timezone_info)

    def _timestamp_date_key(self, value: str | None) -> str | None:
        parsed = self._parse_timestamp(value)
        return parsed.strftime("%Y-%m-%d") if parsed else None

    def _local_date_to_iso(self, value: str | None) -> str | None:
        if not value:
            return None
        return datetime.fromisoformat(f"{value}T00:00:00").replace(
            tzinfo=self.settings.timezone_info,
        ).isoformat()

    def search_laws(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """법령 검색. Phase 1: 제목/별칭 매칭 → Phase 2: FTS 본문 검색 → 점수 계산 후 정렬."""
        normalized = normalize_for_search(query)
        compact = normalized.replace(" ", "")
        tokens = [token for token in normalized.split() if token]
        candidate_limit = max(100, limit * 8)

        with get_law_db_connection() as connection:
            cursor = connection.cursor()

            # --- Phase 1: title / alias match (existing logic) ---
            params: list[Any] = []
            conditions: list[str] = []
            for token in tokens[:6]:
                pattern = f"%{token}%"
                conditions.append(
                    """
                    ld.title_normalized LIKE ?
                    OR EXISTS (
                      SELECT 1
                      FROM law_aliases la
                      WHERE la.law_id = ld.law_id
                        AND la.alias_normalized LIKE ?
                    )
                    """
                )
                params.extend([pattern, pattern])

            if not conditions:
                conditions.append("ld.title_normalized LIKE ?")
                params.append(f"%{compact}%")

            cursor.execute(
                f"""
                SELECT
                  ld.id,
                  ld.law_id,
                  ld.title,
                  ld.law_type,
                  ld.status,
                  ld.effective_date
                FROM law_documents ld
                WHERE {' OR '.join(f'({condition})' for condition in conditions)}
                ORDER BY ld.title
                LIMIT ?
                """,
                [*params, candidate_limit],
            )
            title_rows = sqlite_rows_to_dicts(cursor.fetchall())

            # --- Phase 2: FTS content search (keyword in article text) ---
            fts_rows: list[dict[str, Any]] = []
            fts_query = " OR ".join(tokens[:4]) if tokens else normalized
            try:
                cursor.execute(
                    """
                    SELECT DISTINCT
                      ld.id,
                      ld.law_id,
                      ld.title,
                      ld.law_type,
                      ld.status,
                      ld.effective_date
                    FROM law_search_fts fts
                    JOIN law_documents ld ON ld.law_id = fts.law_id
                    WHERE law_search_fts MATCH ?
                    LIMIT ?
                    """,
                    (fts_query, candidate_limit),
                )
                fts_rows = sqlite_rows_to_dicts(cursor.fetchall())
            except Exception:
                pass  # FTS table might not exist in minimal ingest

            cursor.close()

        # --- Merge and deduplicate ---
        seen_ids: set[str] = set()
        all_rows: list[dict[str, Any]] = []
        for row in title_rows:
            if row["law_id"] not in seen_ids:
                seen_ids.add(row["law_id"])
                all_rows.append(row)
        for row in fts_rows:
            if row["law_id"] not in seen_ids:
                seen_ids.add(row["law_id"])
                all_rows.append(row)

        # --- Score ---
        scored: list[dict[str, Any]] = []
        for row in all_rows:
            title_normalized = normalize_for_search(row["title"]).replace(" ", "")
            score = 0.0
            if compact and compact == title_normalized:
                score += 2.4
            if compact and compact in title_normalized:
                score += 1.6
            for token in tokens[:6]:
                if token in title_normalized:
                    score += 0.28
            # FTS-only results get a base score
            if score == 0.0 and row["law_id"] in seen_ids:
                score = 0.5
            row["score"] = score
            scored.append(row)

        scored.sort(key=lambda item: (-item["score"], item["title"]))
        return scored[:limit]

    def get_law_detail(self, law_id: str) -> dict[str, Any] | None:
        """법령 상세 조회 — 문서 메타데이터 + 전체 조문(chunks)을 반환한다."""
        with get_law_db_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT
                  id,
                  law_id,
                  law_mst,
                  title,
                  law_type,
                  ministry,
                  promulgation_date,
                  effective_date,
                  status,
                  source_url,
                  created_at
                FROM law_documents
                WHERE law_id = ?
                """,
                (law_id,),
            )
            document = sqlite_row_to_dict(cursor.fetchone())
            if document is None:
                cursor.close()
                return None
            if document.get("ministry"):
                document["ministry"] = json.loads(document["ministry"])
            cursor.execute(
                """
                SELECT
                  id,
                  chunk_type,
                  chapter_title,
                  section_title,
                  article_no,
                  article_title,
                  text,
                  order_index,
                  token_count,
                  created_at
                FROM law_chunks
                WHERE document_id = ?
                ORDER BY order_index ASC, article_no ASC
                """,
                (document["id"],),
            )
            chunks = sqlite_rows_to_dicts(cursor.fetchall())
            cursor.close()
        return {"document": document, "chunks": chunks}

    def get_article(self, law_id: str, article_no: str) -> dict[str, Any] | None:
        """특정 법령의 특정 조문을 조회한다. 조문번호를 정규화하여 검색."""
        normalized_article = article_no.replace(" ", "").replace("제", "")
        if not normalized_article.endswith("조"):
            normalized_article = f"{normalized_article}조"
        with get_law_db_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT
                  lc.id,
                  ld.law_id,
                  ld.title AS law_title,
                  lc.chunk_type,
                  lc.chapter_title,
                  lc.section_title,
                  lc.article_no,
                  lc.article_title,
                  lc.text,
                  lc.order_index
                FROM law_chunks lc
                JOIN law_documents ld ON ld.id = lc.document_id
                WHERE ld.law_id = ? AND lc.article_no = ?
                ORDER BY lc.order_index ASC
                LIMIT 1
                """,
                (law_id, normalized_article),
            )
            row = sqlite_row_to_dict(cursor.fetchone())
            cursor.close()
            return row

    def create_saved_answer(self, payload: SavedAnswerCreate) -> SavedAnswerRecord:
        """답변을 state DB에 저장한다. 중복 검사 후 INSERT."""
        created_at = datetime.now(UTC).isoformat()
        saved_id = str(uuid.uuid4())
        with get_state_db_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO saved_answers (id, user_id, question, summary, answer, citations, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    saved_id,
                    payload.user_id,
                    payload.question,
                    payload.summary,
                    payload.answer,
                    json.dumps(payload.citations, ensure_ascii=False),
                    created_at,
                ),
            )
            cursor.close()
            connection.commit()
        return SavedAnswerRecord(
            id=saved_id,
            user_id=payload.user_id,
            question=payload.question,
            summary=payload.summary,
            answer=payload.answer,
            citations=payload.citations,
            created_at=created_at,
        )

    def list_saved_answers(self, *, user_id: str | None = None, limit: int = 50) -> list[SavedAnswerRecord]:
        """저장된 답변 목록 조회. user_id가 있으면 해당 사용자만, 없으면 전체."""
        with get_state_db_connection() as connection:
            cursor = connection.cursor()
            if user_id:
                cursor.execute(
                    """
                    SELECT id, user_id, question, summary, answer, citations, created_at
                    FROM saved_answers
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, user_id, question, summary, answer, citations, created_at
                    FROM saved_answers
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = sqlite_rows_to_dicts(cursor.fetchall())
            cursor.close()

        records: list[SavedAnswerRecord] = []
        for row in rows:
            row["citations"] = json.loads(row["citations"])
            records.append(SavedAnswerRecord.model_validate(row))
        return records

    def create_feedback(self, payload: FeedbackCreate) -> dict[str, Any]:
        """사용자 피드백(좋아요/싫어요)을 저장한다."""
        created_at = datetime.now(UTC).isoformat()
        feedback_id = str(uuid.uuid4())
        question_hash = payload.question_hash or hashlib.sha256(
            (payload.question or "").encode("utf-8")
        ).hexdigest()
        with get_state_db_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO feedback (id, question_hash, rating, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (feedback_id, question_hash, payload.rating, payload.reason, created_at),
            )
            cursor.close()
            connection.commit()
        return {
            "id": feedback_id,
            "question_hash": question_hash,
            "rating": payload.rating,
            "reason": payload.reason,
            "created_at": created_at,
        }

    def get_admin_stats(self) -> AdminStats:
        """관리자 통계 — 문서 수, 청크 수, 저장 답변 수, 피드백 수, 용량 정보."""
        with get_law_db_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM law_documents) AS documents,
                  (SELECT COUNT(*) FROM law_chunks) AS chunks,
                  (SELECT COALESCE(SUM(length(text)), 0) FROM law_chunks) AS estimated_text_bytes,
                  (SELECT COUNT(*) FROM law_chunks WHERE has_embedding = 1) AS vector_rows
                """
            )
            base = sqlite_row_to_dict(cursor.fetchone()) or {}
            cursor.close()

        with get_state_db_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM saved_answers) AS saved_answers,
                  (SELECT COUNT(*) FROM feedback) AS feedback
                """
            )
            state = sqlite_row_to_dict(cursor.fetchone()) or {}
            cursor.close()

        relation_bytes = {
            "laws.sqlite": self._safe_file_size(self.settings.law_db_path),
            "state.sqlite": self._safe_file_size(self.settings.state_db_path),
            "article_embeddings.npy": self._safe_file_size(self.settings.vector_matrix_path),
            "article_embedding_ids.json": self._safe_file_size(self.settings.vector_ids_path),
            "manifest.json": self._safe_file_size(self.settings.artifact_manifest_path),
        }
        return AdminStats(
            documents=int(base.get("documents", 0)),
            chunks=int(base.get("chunks", 0)),
            saved_answers=int(state.get("saved_answers", 0)),
            feedback=int(state.get("feedback", 0)),
            estimated_text_bytes=int(base.get("estimated_text_bytes", 0)),
            estimated_vector_bytes=int(base.get("vector_rows", 0)) * self.settings.embedding_dim * 4,
            relation_bytes=relation_bytes,
        )

    def upsert_user(  # 카카오 로그인 시 사용자 생성 또는 갱신. is_new 플래그로 신규 여부 반환.
        self, kakao_id: str, nickname: str, profile_image: str | None
    ) -> tuple[UserRecord, bool]:
        with get_state_db_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT id, kakao_id, nickname, profile_image, created_at FROM users WHERE kakao_id = ?",
                (kakao_id,),
            )
            existing = sqlite_row_to_dict(cursor.fetchone())
            if existing:
                cursor.execute(
                    "UPDATE users SET nickname = ?, profile_image = ? WHERE kakao_id = ?",
                    (nickname, profile_image, kakao_id),
                )
                connection.commit()
                existing["nickname"] = nickname
                existing["profile_image"] = profile_image
                cursor.close()
                return UserRecord.model_validate(existing), False
            else:
                user_id = str(uuid.uuid4())
                created_at = datetime.now(UTC).isoformat()
                cursor.execute(
                    "INSERT INTO users (id, kakao_id, nickname, profile_image, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, kakao_id, nickname, profile_image, created_at),
                )
                connection.commit()
                cursor.close()
                return UserRecord(
                    id=user_id,
                    kakao_id=kakao_id,
                    nickname=nickname,
                    profile_image=profile_image,
                    created_at=created_at,
                ), True

    def get_user(self, user_id: str) -> UserRecord | None:  # ID로 사용자 조회
        with get_state_db_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT id, kakao_id, nickname, profile_image, created_at FROM users WHERE id = ?",
                (user_id,),
            )
            row = sqlite_row_to_dict(cursor.fetchone())
            cursor.close()
        if row is None:
            return None
        return UserRecord.model_validate(row)

    def get_usage_count(self, *, user_id: str | None = None, session_id: str | None = None) -> int:
        """오늘 사용 횟수 조회. user_id(로그인) 또는 session_id(비로그인) 기준."""
        today = self._local_date_key()
        with get_state_db_connection() as connection:
            cursor = connection.cursor()
            if user_id:
                # Logged-in users: daily limit (changed from permanent to daily for data accumulation)
                cursor.execute(
                    "SELECT SUM(count) FROM usage_counts WHERE user_id = ? AND date = ?",
                    (user_id, today),
                )
            elif session_id:
                # Guests: daily limit
                cursor.execute(
                    "SELECT count FROM usage_counts WHERE session_id = ? AND date = ?",
                    (session_id, today),
                )
            else:
                return 0
            row = cursor.fetchone()
            cursor.close()
        return int((row[0] or 0) if row else 0)

    def increment_usage(self, *, user_id: str | None = None, session_id: str | None = None) -> int:
        """사용 횟수를 1 증가시킨다. 오늘 레코드가 없으면 새로 생성."""
        today = self._local_date_key()
        with get_state_db_connection() as connection:
            cursor = connection.cursor()
            if user_id:
                cursor.execute(
                    "SELECT count FROM usage_counts WHERE user_id = ? AND date = ?",
                    (user_id, today),
                )
                row = cursor.fetchone()
                if row:
                    new_count = row[0] + 1
                    cursor.execute(
                        "UPDATE usage_counts SET count = ? WHERE user_id = ? AND date = ?",
                        (new_count, user_id, today),
                    )
                else:
                    new_count = 1
                    cursor.execute(
                        "INSERT INTO usage_counts (id, user_id, date, count) VALUES (?, ?, ?, ?)",
                        (str(uuid.uuid4()), user_id, today, new_count),
                    )
            elif session_id:
                cursor.execute(
                    "SELECT count FROM usage_counts WHERE session_id = ? AND date = ?",
                    (session_id, today),
                )
                row = cursor.fetchone()
                if row:
                    new_count = row[0] + 1
                    cursor.execute(
                        "UPDATE usage_counts SET count = ? WHERE session_id = ? AND date = ?",
                        (new_count, session_id, today),
                    )
                else:
                    new_count = 1
                    cursor.execute(
                        "INSERT INTO usage_counts (id, session_id, date, count) VALUES (?, ?, ?, ?)",
                        (str(uuid.uuid4()), session_id, today, new_count),
                    )
            else:
                cursor.close()
                return 0
            connection.commit()
            cursor.close()
        return new_count

    def record_request_event(
        self,
        event_type: str,
        *,
        channel: str = "web",
        user_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """운영 대시보드용 요청 이벤트를 저장한다.

        질문/검토/검색/초안 생성 같은 사용자 액션을 endpoint 단위로 추적한다.
        raw question text는 저장하지 않고, 최소한의 메타데이터만 JSON으로 기록한다.
        """
        actor_kind = "authenticated" if user_id else "guest" if session_id else "system"
        clean_channel = channel if channel in {"web", "mobile", "admin"} else "unknown"
        created_at = datetime.now(UTC).isoformat()
        with get_state_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO request_events (
                  id, event_type, channel, actor_kind, user_id, session_id, metadata, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    event_type,
                    clean_channel,
                    actor_kind,
                    user_id,
                    session_id,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    created_at,
                ),
            )
            conn.commit()

    def get_admin_dashboard_snapshot(self) -> dict[str, Any]:
        """운영 대시보드용 집계 스냅샷을 생성한다.

        - request_events: 현재 운영 이벤트 기준 activity
        - usage_counts: 기존 한도/세션 기준 legacy 카운터
        - users/feedback/chat_history/cache/storage: 운영 보조 지표
        """
        now_local = self._local_now()
        today = self._local_date_key(now_local)
        trend_dates = [
            self._local_date_key(now_local - timedelta(days=offset))
            for offset in range(6, -1, -1)
        ]
        window_start_local = (now_local - timedelta(days=6)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        window_start_utc = window_start_local.astimezone(UTC).isoformat()

        def _zero_event_trend(date_key: str) -> dict[str, Any]:
            return {
                "date": date_key,
                "total": 0,
                "chat": 0,
                "deep_dive": 0,
                "reviews": 0,
                "drafts": 0,
                "searches": 0,
                "feedback": 0,
                "logins": 0,
            }

        def _zero_answer_trend(date_key: str) -> dict[str, Any]:
            return {"date": date_key, "total": 0}

        with get_state_db_connection() as connection:
            cursor = connection.cursor()

            def _scalar(query: str, params: tuple[Any, ...] = ()) -> int:
                cursor.execute(query, params)
                row = cursor.fetchone()
                return int((row[0] or 0) if row else 0)

            def _table_exists(table_name: str) -> bool:
                cursor.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table_name,),
                )
                return cursor.fetchone() is not None

            total_users = _scalar("SELECT COUNT(*) FROM users")
            cursor.execute("SELECT nickname, created_at FROM users ORDER BY created_at DESC LIMIT 10")
            recent_users = [{"nickname": r[0], "created_at": r[1]} for r in cursor.fetchall()]
            cursor.execute("SELECT created_at FROM users")
            new_users_today = sum(
                1
                for row in cursor.fetchall()
                if self._timestamp_date_key(row[0]) == today
            )

            # 전체 사용자 수: 로그인 유저 + 비로그인 고유 세션
            # session_id가 있으면 세션 단위, 없으면 anonymous 1명으로 카운트
            unique_guest_sessions = _scalar(
                "SELECT COUNT(DISTINCT session_id) FROM chat_history "
                "WHERE user_id = 'anonymous' AND session_id IS NOT NULL",
            )
            has_anonymous_without_session = _scalar(
                "SELECT COUNT(*) FROM chat_history "
                "WHERE user_id = 'anonymous' AND session_id IS NULL",
            )
            unique_guests = unique_guest_sessions + (1 if has_anonymous_without_session > 0 else 0)
            total_all_users = total_users + unique_guests

            legacy_active_users = _scalar(
                "SELECT COUNT(DISTINCT user_id) FROM usage_counts WHERE user_id IS NOT NULL AND date = ?",
                (today,),
            )
            legacy_today_questions = _scalar(
                "SELECT SUM(count) FROM usage_counts WHERE date = ? AND session_id NOT LIKE 'ip:%'",
                (today,),
            )
            legacy_total_active_users = _scalar(
                "SELECT COUNT(DISTINCT user_id) FROM usage_counts WHERE user_id IS NOT NULL",
            )
            legacy_guest_sessions = _scalar(
                "SELECT COUNT(DISTINCT session_id) FROM usage_counts WHERE session_id IS NOT NULL AND session_id NOT LIKE 'ip:%'",
            )
            legacy_user_questions = _scalar(
                "SELECT SUM(count) FROM usage_counts WHERE user_id IS NOT NULL",
            )
            legacy_guest_questions = _scalar(
                "SELECT SUM(count) FROM usage_counts WHERE session_id IS NOT NULL AND session_id NOT LIKE 'ip:%'",
            )
            legacy_usage_rows = _scalar(
                "SELECT COUNT(*) FROM usage_counts WHERE session_id IS NULL OR session_id NOT LIKE 'ip:%'",
            )
            cursor.execute(
                "SELECT MAX(date) FROM usage_counts WHERE session_id IS NULL OR session_id NOT LIKE 'ip:%'",
            )
            row = cursor.fetchone()
            usage_last_seen_at = self._local_date_to_iso(row[0] if row else None)

            cursor.execute("SELECT created_at FROM feedback")
            feedback_today = sum(
                1
                for row in cursor.fetchall()
                if self._timestamp_date_key(row[0]) == today
            )

            cursor.execute(
                "SELECT ch.question, ch.created_at, ch.user_id, ch.session_id, u.nickname "
                "FROM chat_history ch "
                "LEFT JOIN users u ON ch.user_id = u.id "
                "ORDER BY ch.created_at DESC LIMIT 10"
            )
            recent_questions = [
                {
                    "question": r[0],
                    "created_at": r[1],
                    "actor": r[4] if r[4] else (
                        "게스트" if r[2] == "anonymous" else r[2]
                    ),
                    "actor_type": "user" if r[2] != "anonymous" else "guest",
                    "session_id": r[3][:8] + "…" if r[3] else None,
                }
                for r in cursor.fetchall()
            ]
            cursor.execute("SELECT created_at FROM chat_history")
            chat_created_rows = [row[0] for row in cursor.fetchall()]
            chat_history_rows = len(chat_created_rows)
            chat_history_today = 0
            chat_last_seen_at = None
            answer_trend_map = {date_key: _zero_answer_trend(date_key) for date_key in trend_dates}
            for created_at in chat_created_rows:
                date_key = self._timestamp_date_key(created_at)
                if date_key == today:
                    chat_history_today += 1
                if date_key in answer_trend_map:
                    answer_trend_map[date_key]["total"] += 1
                if chat_last_seen_at is None or (created_at and created_at > chat_last_seen_at):
                    chat_last_seen_at = created_at

            operations: dict[str, dict[str, Any]] = {
                event_type: {
                    "event_type": event_type,
                    "label": REQUEST_EVENT_LABELS.get(event_type, event_type),
                    "today": 0,
                    "last_7d": 0,
                    "last_seen_at": None,
                }
                for event_type in REQUEST_EVENT_ORDER
            }
            event_total_today = 0
            event_active_users_today = 0
            event_guest_sessions_today = 0
            review_total_today = 0
            draft_total_today = 0
            login_total_today = 0
            request_event_rows = 0
            event_tracking_started_at = None
            request_events_last_seen_at = None
            trend_map = {date_key: _zero_event_trend(date_key) for date_key in trend_dates}
            recent_activity = []

            if _table_exists("request_events"):
                request_event_rows = _scalar("SELECT COUNT(*) FROM request_events")
                cursor.execute("SELECT MIN(created_at), MAX(created_at) FROM request_events")
                row = cursor.fetchone()
                if row:
                    event_tracking_started_at = row[0]
                    request_events_last_seen_at = row[1]

                cursor.execute(
                    """
                    SELECT event_type, channel, actor_kind, metadata, created_at
                    FROM request_events
                    ORDER BY created_at DESC
                    LIMIT 12
                    """
                )
                for row in sqlite_rows_to_dicts(cursor.fetchall()):
                    metadata = {}
                    try:
                        metadata = json.loads(row.get("metadata") or "{}")
                    except Exception:
                        metadata = {}
                    event_type = row.get("event_type") or "unknown"
                    recent_activity.append({
                        "event_type": event_type,
                        "label": REQUEST_EVENT_LABELS.get(event_type, event_type),
                        "channel": row.get("channel") or "unknown",
                        "actor_kind": row.get("actor_kind") or "unknown",
                        "created_at": row.get("created_at"),
                        "summary": _request_event_summary(event_type, metadata),
                    })

                cursor.execute(
                    """
                    SELECT event_type, channel, actor_kind, user_id, session_id, metadata, created_at
                    FROM request_events
                    WHERE created_at >= ?
                    ORDER BY created_at DESC
                    """,
                    (window_start_utc,),
                )
                event_window_rows = sqlite_rows_to_dicts(cursor.fetchall())
                today_users: set[str] = set()
                today_guest_sessions: set[str] = set()
                for row in event_window_rows:
                    created_at = row.get("created_at")
                    date_key = self._timestamp_date_key(created_at)
                    if date_key not in trend_map:
                        continue
                    event_type = row.get("event_type") or "unknown"
                    if event_type not in operations:
                        operations[event_type] = {
                            "event_type": event_type,
                            "label": REQUEST_EVENT_LABELS.get(event_type, event_type),
                            "today": 0,
                            "last_7d": 0,
                            "last_seen_at": None,
                        }
                    operations[event_type]["last_7d"] += 1
                    if operations[event_type]["last_seen_at"] is None or (
                        created_at and created_at > operations[event_type]["last_seen_at"]
                    ):
                        operations[event_type]["last_seen_at"] = created_at

                    bucket = trend_map[date_key]
                    bucket["total"] += 1
                    if event_type == "chat_answer":
                        bucket["chat"] += 1
                    elif event_type in {"deep_dive_follow_up", "deep_dive_analysis"}:
                        bucket["deep_dive"] += 1
                    elif event_type in {"review_text", "review_file"}:
                        bucket["reviews"] += 1
                    elif event_type == "document_draft":
                        bucket["drafts"] += 1
                    elif event_type in {"search_laws", "search_precedents"}:
                        bucket["searches"] += 1
                    elif event_type == "feedback":
                        bucket["feedback"] += 1
                    elif event_type == "auth_login":
                        bucket["logins"] += 1

                    if date_key != today:
                        continue
                    operations[event_type]["today"] += 1
                    event_total_today += 1
                    if row.get("user_id"):
                        today_users.add(row["user_id"])
                    elif row.get("session_id"):
                        today_guest_sessions.add(row["session_id"])
                    if event_type in {"review_text", "review_file"}:
                        review_total_today += 1
                    elif event_type == "document_draft":
                        draft_total_today += 1
                    elif event_type == "auth_login":
                        login_total_today += 1

                event_active_users_today = len(today_users)
                event_guest_sessions_today = len(today_guest_sessions)

            ordered_operations = [operations[event_type] for event_type in REQUEST_EVENT_ORDER if event_type in operations]
            ordered_operations.extend(
                operations[event_type]
                for event_type in operations
                if event_type not in REQUEST_EVENT_ORDER
            )
            daily_trend = [trend_map[date_key] for date_key in trend_dates]

            response_cache_rows = _scalar("SELECT COUNT(*) FROM response_cache")
            precedent_cache_rows = _scalar("SELECT COUNT(*) FROM precedent_cache")
            feedback_total = _scalar("SELECT COUNT(*) FROM feedback")
            saved_answers_total = _scalar("SELECT COUNT(*) FROM saved_answers")
            cursor.close()

        law_db_size = self._safe_file_size(self.settings.law_db_path)
        vector_size = self._safe_file_size(self.settings.vector_matrix_path)
        state_db_size = self._safe_file_size(self.settings.state_db_path)

        count_gap = legacy_today_questions - chat_history_today
        answer_daily_trend = [answer_trend_map[date_key] for date_key in trend_dates]
        request_events_status = "trusted" if request_event_rows > 0 else "missing"
        chat_history_status = "trusted" if chat_history_rows > 0 else "missing"
        usage_status = "partial"

        notes = [
            f"모든 오늘/7일 집계는 {self.settings.app_timezone} 기준으로 계산됩니다.",
            "chat_history 최근 질문은 /v1/chat, /v1/deep-dive 분석 완료 경로 위주로만 저장됩니다.",
        ]
        if request_event_rows == 0:
            notes.insert(0, "request_events 테이블은 준비됐지만 아직 누적된 운영 이벤트가 없습니다. API가 새 계측 코드로 재시작된 뒤 들어온 요청부터 보입니다.")
        else:
            notes.insert(0, "운영 이벤트는 request_events 기준으로 집계됩니다. 이 상세 분해는 계측 배포 이후부터 정확히 누적됩니다.")
        if count_gap != 0:
            notes.append(
                f"오늘 usage_counts는 {legacy_today_questions}건, chat_history는 {chat_history_today}건입니다. 차이 {abs(count_gap)}건은 검토/세션 카운트 등 범위 차이에서 생길 수 있습니다.",
            )
        else:
            notes.append("오늘 usage_counts와 chat_history 답변 완료 수는 같은 수준으로 보입니다.")
        if request_event_rows == 0 and chat_history_today > 0:
            notes.append(
                f"오늘 chat_history에는 {chat_history_today}건의 답변 완료 기록이 있으나 request_events는 비어 있습니다. 현재 운영 이벤트 추적은 아직 활성화되지 않은 상태로 보는 것이 안전합니다.",
            )

        return {
            "generated_at": now_local.isoformat(),
            "event_tracking_started_at": event_tracking_started_at,
            "today": {
                "date": today,
                "events": event_total_today,
                "answered_conversations": chat_history_today,
                "counted_requests": legacy_today_questions,
                "count_gap": count_gap,
                "active_users": event_active_users_today,
                "guest_sessions": event_guest_sessions_today,
                "reviews": review_total_today,
                "drafts": draft_total_today,
                "feedback": feedback_today,
                "logins": login_total_today,
                "new_users": new_users_today,
            },
            "operations": ordered_operations,
            "questions": {
                "daily_trend": daily_trend,
            },
            "conversations": {
                "today_completed": chat_history_today,
                "last_7d_completed": sum(item["total"] for item in answer_daily_trend),
                "daily_trend": answer_daily_trend,
                "last_completed_at": chat_last_seen_at,
            },
            "runtime": {
                "env": self.settings.app_env,
                "api_base_url": self.settings.api_base_url,
                "app_base_url": self.settings.app_base_url,
                "timezone": self.settings.app_timezone,
                "daily_question_limit": self.settings.daily_question_limit,
                "feature_flags": {
                    "vector_search": self.settings.enable_vector_search,
                    "server_chat_history": self.settings.enable_server_chat_history,
                    "admin_upload_ui": self.settings.enable_admin_upload_ui,
                    "article_segment": self.settings.enable_article_segment,
                    "appendix_search": self.settings.enable_appendix_search,
                },
            },
            "users": {
                "total_registered": total_users,
                "total_all": total_all_users,
                "total_active": legacy_total_active_users,
                "guest_sessions": unique_guests,
                "recent_signups": recent_users,
            },
            "legacy_usage": {
                "today_counted_requests": legacy_today_questions,
                "today_active_users": legacy_active_users,
                "by_users": legacy_user_questions,
                "by_guests": legacy_guest_questions,
                "total": legacy_user_questions + legacy_guest_questions,
            },
            "cache": {
                "response_cache_rows": response_cache_rows,
                "precedent_cache_rows": precedent_cache_rows,
                "chat_history_rows": chat_history_rows,
            },
            "quality": {
                "feedback_total": feedback_total,
                "saved_answers_total": saved_answers_total,
            },
            "data_quality": {
                "request_events": {
                    "label": "운영 이벤트",
                    "status": request_events_status,
                    "detail": "검색, 검토, 로그인, 서류 초안까지 endpoint 성공 이벤트를 수집합니다."
                    if request_event_rows > 0
                    else "테이블은 생성됐지만 아직 실제 누적 데이터가 없습니다. 재시작 후 새 요청부터 채워집니다.",
                    "total_rows": request_event_rows,
                    "today_count": event_total_today,
                    "last_seen_at": request_events_last_seen_at,
                },
                "chat_history": {
                    "label": "답변 완료 기록",
                    "status": chat_history_status,
                    "detail": "실제 답변 완료 기록입니다. /v1/chat과 완료된 /v1/deep-dive만 저장합니다.",
                    "total_rows": chat_history_rows,
                    "today_count": chat_history_today,
                    "last_seen_at": chat_last_seen_at,
                },
                "usage_counts": {
                    "label": "한도 카운터",
                    "status": usage_status,
                    "detail": "한도/세션 추적용 카운터입니다. 답변 완료 수와 1:1로 맞지 않을 수 있습니다.",
                    "total_rows": legacy_usage_rows,
                    "today_count": legacy_today_questions,
                    "last_seen_at": usage_last_seen_at,
                },
            },
            "storage": {
                "law_db_mb": round(law_db_size / 1024 / 1024, 1),
                "vector_mb": round(vector_size / 1024 / 1024, 1),
                "state_db_mb": round(state_db_size / 1024 / 1024, 1),
            },
            "recent_activity": recent_activity,
            "recent_questions": recent_questions,
            "notes": notes,
        }

    # ── Cache methods ────────────────────────────────────────

    def get_response_cache(self, question: str) -> dict | None:  # 질문 해시로 응답 캐시 조회 (TTL 만료 확인)
        question_hash = hashlib.sha256(question.strip().lower().encode()).hexdigest()
        now = datetime.now(UTC).isoformat()
        with get_state_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT response FROM response_cache WHERE question_hash = ? AND expires_at > ?",
                (question_hash, now),
            )
            row = cursor.fetchone()
            cursor.close()
        return json.loads(row[0]) if row else None

    def set_response_cache(self, question: str, response: dict, ttl_hours: int = 1) -> None:  # 응답 캐시 저장 (기본 1시간 TTL)
        question_hash = hashlib.sha256(question.strip().lower().encode()).hexdigest()
        now = datetime.now(UTC)
        expires = datetime(now.year, now.month, now.day, now.hour + ttl_hours, tzinfo=UTC).isoformat()
        with get_state_db_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO response_cache (question_hash, question, response, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (question_hash, question.strip(), json.dumps(response, ensure_ascii=False), now.isoformat(), expires),
            )
            conn.commit()

    def get_precedent_cache(self, keyword: str) -> list | None:  # 키워드로 판례 캐시 조회 (24시간 TTL)
        now = datetime.now(UTC).isoformat()
        with get_state_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT results FROM precedent_cache WHERE keyword = ? AND expires_at > ?",
                (keyword, now),
            )
            row = cursor.fetchone()
            cursor.close()
        return json.loads(row[0]) if row else None

    def set_precedent_cache(self, keyword: str, results: list, ttl_hours: int = 24) -> None:  # 판례 캐시 저장
        now = datetime.now(UTC)
        expires = datetime(now.year, now.month, now.day + (ttl_hours // 24), tzinfo=UTC).isoformat()
        with get_state_db_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO precedent_cache (keyword, results, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (keyword, json.dumps(results, ensure_ascii=False), now.isoformat(), expires),
            )
            conn.commit()

    def save_chat_history(
        self,
        user_id: str,
        question: str,
        response: dict,
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        turn_index: int = 0,
        request_type: str = "chat",
        has_attachments: bool = False,
        attachment_names: list[str] | None = None,
    ) -> str:
        """통합 대화 히스토리 저장. 모든 유형(chat/deep_dive/review/draft)을 한 테이블에 저장하되
        request_type과 thread_id로 구분한다. 생성된 row의 id를 반환."""
        row_id = str(uuid.uuid4())
        with get_state_db_connection() as conn:
            conn.execute(
                """INSERT INTO chat_history
                   (id, user_id, session_id, thread_id, turn_index, request_type,
                    question, response, has_attachments, attachment_names, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row_id,
                    user_id,
                    session_id,
                    thread_id,
                    turn_index,
                    request_type,
                    question,
                    json.dumps(response, ensure_ascii=False),
                    1 if has_attachments else 0,
                    json.dumps(attachment_names, ensure_ascii=False) if attachment_names else None,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
        return row_id

    def get_chat_history(self, user_id: str, limit: int = 20, *, request_type: str | None = None) -> list[dict]:
        """사용자 채팅 히스토리 조회 (최신순). request_type으로 필터링 가능."""
        with get_state_db_connection() as conn:
            cursor = conn.cursor()
            if request_type:
                if request_type == "chat":
                    type_clause = "request_type IN ('chat', 'deep_dive_follow_up', 'deep_dive_analysis')"
                    params = (user_id, limit)
                elif request_type == "review":
                    type_clause = "request_type IN ('review_file', 'review_text')"
                    params = (user_id, limit)
                else:
                    type_clause = "request_type = ?"
                    params = (user_id, request_type, limit)
                cursor.execute(
                    f"""SELECT id, thread_id, turn_index, request_type, question, response,
                              has_attachments, attachment_names, created_at
                       FROM chat_history WHERE user_id = ? AND {type_clause}
                       ORDER BY created_at DESC LIMIT ?""",
                    params,
                )
            else:
                cursor.execute(
                    """SELECT id, thread_id, turn_index, request_type, question, response,
                              has_attachments, attachment_names, created_at
                       FROM chat_history WHERE user_id = ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (user_id, limit),
                )
            rows = []
            for r in cursor.fetchall():
                rows.append({
                    "id": r[0], "thread_id": r[1], "turn_index": r[2],
                    "request_type": r[3], "question": r[4],
                    "response": json.loads(r[5]),
                    "has_attachments": bool(r[6]),
                    "attachment_names": json.loads(r[7]) if r[7] else [],
                    "created_at": r[8],
                })
            cursor.close()
        return rows

    def get_thread_history(self, thread_id: str) -> list[dict]:
        """스레드 ID로 대화 전체 턴을 순서대로 조회한다."""
        with get_state_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT id, user_id, turn_index, request_type, question, response,
                          has_attachments, attachment_names, created_at
                   FROM chat_history WHERE thread_id = ?
                   ORDER BY turn_index ASC""",
                (thread_id,),
            )
            rows = []
            for r in cursor.fetchall():
                rows.append({
                    "id": r[0], "user_id": r[1], "turn_index": r[2],
                    "request_type": r[3], "question": r[4],
                    "response": json.loads(r[5]),
                    "has_attachments": bool(r[6]),
                    "attachment_names": json.loads(r[7]) if r[7] else [],
                    "created_at": r[8],
                })
            cursor.close()
        return rows

    def cleanup_expired_caches(self) -> int:  # 만료된 응답/판례 캐시를 삭제한다. 삭제된 행 수 반환.
        now = datetime.now(UTC).isoformat()
        total = 0
        with get_state_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM response_cache WHERE expires_at <= ?", (now,))
            total += cursor.rowcount
            cursor.execute("DELETE FROM precedent_cache WHERE expires_at <= ?", (now,))
            total += cursor.rowcount
            conn.commit()
            cursor.close()
        return total

    @staticmethod
    def _safe_file_size(path: Path) -> int:
        return path.stat().st_size if path.exists() else 0
