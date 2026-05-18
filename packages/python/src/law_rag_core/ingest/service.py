from __future__ import annotations

from collections import Counter
from datetime import date
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import uuid
from zipfile import ZipFile

import numpy as np

from ..ai import GeminiService
from ..db import get_state_db_connection, sqlite_row_to_dict
from ..estimates import estimate_ingest_bytes
from ..logging import get_logger
from ..normalization import normalize_for_search
from ..parser import parse_law_markdown
from ..settings import get_settings
from ..types import IngestJobRecord, IngestedLaw, LawChunkRecord


LOGGER = get_logger(__name__)


class IngestService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.gemini = GeminiService()

    def apply_schema(self) -> None:
        self.settings.artifact_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.settings.law_db_path.with_suffix(".tmp")
        if temp_path.exists():
            temp_path.unlink()
        connection = sqlite3.connect(temp_path)
        try:
            connection.executescript(self.settings.sqlite_schema_path.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()
        temp_path.replace(self.settings.law_db_path)

    def ingest_path(
        self,
        input_path: str | Path,
        *,
        mode: str = "minimal",
        apply_schema: bool = False,
        reindex: bool = False,
    ) -> IngestJobRecord:
        if apply_schema and self.settings.law_db_path.exists():
            # Backup existing DB and vectors before destroying
            backup_dir = self.settings.artifact_dir / "backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            from shutil import copy2
            for path in [self.settings.law_db_path, self.settings.vector_matrix_path, self.settings.vector_ids_path]:
                if path.exists():
                    copy2(path, backup_dir / path.name)
                    LOGGER.info("Backed up %s to %s", path.name, backup_dir)
            self.settings.law_db_path.unlink()
        if apply_schema or not self.settings.law_db_path.exists():
            self.apply_schema()

        source = Path(input_path)
        job_id = self._create_job(source_type="zip" if source.suffix == ".zip" else "path")
        parsed_laws: list[IngestedLaw] = []
        embeddings_skipped = 0
        duplicate_stats = {"duplicate_law_ids": 0, "duplicate_rows_dropped": 0}

        try:
            if source.is_file() and source.suffix.lower() == ".zip":
                parsed_laws.extend(self._parse_zip_markdown_files(source, mode=mode))
            else:
                parsed_laws.extend(self._parse_markdown_files(self._iter_markdown_files(source), mode=mode))

            parsed_laws, duplicate_stats = self._dedupe_laws(parsed_laws)

            # Load pre-generated questions if available
            chunk_questions = self._load_chunk_questions()

            vector_rows: list[np.ndarray] = []
            vector_chunk_ids: list[str] = []
            for law in parsed_laws:
                law.chunks = self._filter_chunks_for_mode(law.chunks, mode=mode)
                for chunk in law.chunks:
                    if not self._should_vectorize(chunk):
                        continue
                    if reindex or chunk.embedding is None:
                        # Build enriched text for embedding
                        embed_text = self._build_embed_text(chunk, law.document.title, chunk_questions)
                        embedding = self.gemini.embed_text(embed_text)
                        if embedding:
                            normalized = self._normalize_embedding(embedding)
                            chunk.embedding = normalized.tolist()
                            vector_rows.append(normalized)
                            vector_chunk_ids.append(chunk.content_hash)
                        else:
                            embeddings_skipped += 1

            self._write_artifacts(parsed_laws)
            self._write_vector_artifacts(vector_rows, vector_chunk_ids)

            estimate = estimate_ingest_bytes(parsed_laws, self.settings.embedding_dim)
            stats = {
                **estimate,
                "mode": mode,
                "files": len(parsed_laws),
                "embeddings_skipped": embeddings_skipped,
                "vector_rows": len(vector_chunk_ids),
                **duplicate_stats,
                "artifact_bytes": {
                    "laws.sqlite": self._safe_file_size(self.settings.law_db_path),
                    "article_embeddings.npy": self._safe_file_size(self.settings.vector_matrix_path),
                    "article_embedding_ids.json": self._safe_file_size(self.settings.vector_ids_path),
                },
            }
            self.settings.artifact_manifest_path.write_text(
                json.dumps(stats, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._finish_job(job_id, status="completed", stats=stats)
            LOGGER.info("Artifact ingest completed: %s", json.dumps(stats, ensure_ascii=False))
            return self.get_job(job_id)
        except Exception as exc:  # pragma: no cover
            LOGGER.exception("Artifact ingest failed.")
            self._finish_job(job_id, status="failed", stats={}, error_log=str(exc))
            raise

    def get_job(self, job_id: str) -> IngestJobRecord:
        with get_state_db_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT id, status, source_type, started_at, finished_at, stats, error_log
                FROM ingest_jobs
                WHERE id = ?
                """,
                (job_id,),
            )
            row = sqlite_row_to_dict(cursor.fetchone())
            cursor.close()
        if row is None:
            raise ValueError(f"Ingest job not found: {job_id}")
        row["stats"] = json.loads(row["stats"])
        return IngestJobRecord.model_validate(row)

    def _iter_markdown_files(self, source: Path) -> list[Path]:
        if source.is_file() and source.suffix.lower() == ".md":
            return [source]
        if source.is_dir():
            return sorted(source.rglob("*.md"))
        raise FileNotFoundError(f"Unsupported ingest source: {source}")

    def _parse_markdown_files(self, files: list[Path], *, mode: str) -> list[IngestedLaw]:
        parsed_laws: list[IngestedLaw] = []
        for markdown_file in files:
            raw_text = markdown_file.read_text(encoding="utf-8")
            parsed_laws.append(
                parse_law_markdown(
                    markdown_file,
                    raw_text,
                    minimal_mode=mode == "minimal",
                    enable_article_segment=self.settings.enable_article_segment and mode != "minimal",
                )
            )
        return parsed_laws

    def _parse_zip_markdown_files(self, source: Path, *, mode: str) -> list[IngestedLaw]:
        parsed_laws: list[IngestedLaw] = []
        with ZipFile(source) as archive:
            names = sorted(name for name in archive.namelist() if name.lower().endswith(".md"))
            for name in names:
                raw_text = archive.read(name).decode("utf-8")
                parsed_laws.append(
                    parse_law_markdown(
                        Path(self._recover_zip_name(name)),
                        raw_text,
                        minimal_mode=mode == "minimal",
                        enable_article_segment=self.settings.enable_article_segment and mode != "minimal",
                    )
                )
        return parsed_laws

    def _dedupe_laws(self, laws: list[IngestedLaw]) -> tuple[list[IngestedLaw], dict[str, int]]:
        counts = Counter(law.document.law_id for law in laws)
        selected: dict[str, IngestedLaw] = {}
        for law in laws:
            law_id = law.document.law_id
            existing = selected.get(law_id)
            if existing is None or self._law_priority(law) > self._law_priority(existing):
                selected[law_id] = law

        deduped = sorted(selected.values(), key=lambda item: (item.document.title, item.document.law_id))
        stats = {
            "duplicate_law_ids": sum(1 for count in counts.values() if count > 1),
            "duplicate_rows_dropped": max(0, len(laws) - len(deduped)),
        }
        return deduped, stats

    @staticmethod
    def _law_priority(law: IngestedLaw) -> tuple[date, date, int, int, str]:
        min_date = date.min
        status = (law.document.status or "").strip()
        return (
            law.document.effective_date or law.document.promulgation_date or min_date,
            law.document.promulgation_date or min_date,
            1 if status in {"시행", "active", "현행"} else 0,
            len(law.chunks),
            law.document.content_hash,
        )

    @staticmethod
    def _recover_zip_name(name: str) -> str:
        for encoding in ("utf-8", "cp949", "euc-kr"):
            try:
                return name.encode("cp437").decode(encoding)
            except Exception:
                continue
        return name

    def _filter_chunks_for_mode(self, chunks: list[LawChunkRecord], *, mode: str) -> list[LawChunkRecord]:
        if mode != "minimal":
            return chunks
        filtered = [chunk for chunk in chunks if chunk.chunk_type == "article"]
        return filtered if filtered else chunks

    def _load_chunk_questions(self) -> dict[str, list[str]]:
        questions_path = self.settings.artifact_dir / "chunk_questions.json"
        if questions_path.exists():
            try:
                data = json.loads(questions_path.read_text(encoding="utf-8"))
                LOGGER.info("Loaded %d chunk questions for enriched embedding", len(data))
                return data
            except Exception:
                LOGGER.warning("Failed to load chunk_questions.json, proceeding without questions")
        return {}

    @staticmethod
    def _build_embed_text(
        chunk: LawChunkRecord,
        law_title: str,
        chunk_questions: dict[str, list[str]],
    ) -> str:
        parts = []
        # 1. Law title + article metadata
        meta = f"{law_title} {chunk.article_no or ''} {chunk.article_title or ''}".strip()
        if meta:
            parts.append(meta)
        # 2. Pre-generated questions (if available)
        questions = chunk_questions.get(chunk.content_hash, [])
        if questions:
            parts.append("관련 질문: " + " / ".join(questions[:3]))
        # 3. Article text
        parts.append(chunk.text[:800])
        return " | ".join(parts)

    def _should_vectorize(self, chunk: LawChunkRecord) -> bool:
        return self.settings.enable_vector_search and chunk.chunk_type == "article"

    @staticmethod
    def _normalize_embedding(values: list[float]) -> np.ndarray:
        vector = np.asarray(values, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            return vector
        return vector / norm

    def _write_artifacts(self, laws: list[IngestedLaw]) -> None:
        temp_path = self.settings.law_db_path.with_suffix(".build.sqlite")
        if temp_path.exists():
            temp_path.unlink()
        connection = sqlite3.connect(temp_path)
        try:
            connection.executescript(self.settings.sqlite_schema_path.read_text(encoding="utf-8"))
            cursor = connection.cursor()
            now = datetime.now(UTC).isoformat()

            for law in laws:
                document_id = law.document.content_hash
                cursor.execute(
                    """
                    INSERT INTO law_documents (
                      id, law_id, law_mst, title, title_normalized, law_type, ministry,
                      promulgation_date, effective_date, status, source_url, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        law.document.law_id,
                        law.document.law_mst,
                        law.document.title,
                        normalize_for_search(law.document.title).replace(" ", ""),
                        law.document.law_type,
                        json.dumps(law.document.ministry or {}, ensure_ascii=False),
                        law.document.promulgation_date.isoformat() if law.document.promulgation_date else None,
                        law.document.effective_date.isoformat() if law.document.effective_date else None,
                        law.document.status,
                        law.document.source_url,
                        now,
                        now,
                    ),
                )

                for alias in law.document.aliases:
                    cursor.execute(
                        """
                        INSERT INTO law_aliases (id, law_id, alias, alias_normalized, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            f"{law.document.law_id}:{normalize_for_search(alias).replace(' ', '')}",
                            law.document.law_id,
                            alias,
                            normalize_for_search(alias).replace(" ", ""),
                            now,
                        ),
                    )

                for chunk in law.chunks:
                    cursor.execute(
                        """
                        INSERT INTO law_chunks (
                          id, document_id, law_id, chunk_type, chapter_title, section_title, article_no,
                          article_title, text, order_index, token_count, has_embedding, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk.content_hash,
                            document_id,
                            law.document.law_id,
                            chunk.chunk_type,
                            chunk.chapter_title,
                            chunk.section_title,
                            chunk.article_no,
                            chunk.article_title,
                            chunk.text,
                            chunk.order_index,
                            chunk.token_count,
                            1 if chunk.embedding else 0,
                            now,
                        ),
                    )
                    if chunk.chunk_type == "article" or self.settings.enable_appendix_search:
                        cursor.execute(
                            """
                            INSERT INTO law_search_fts (chunk_id, law_id, law_title, search_text, article_no, article_title)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                chunk.content_hash,
                                law.document.law_id,
                                law.document.title,
                                chunk.search_text,
                                chunk.article_no,
                                chunk.article_title,
                            ),
                        )

            connection.commit()
        finally:
            connection.close()
        temp_path.replace(self.settings.law_db_path)

    def _write_vector_artifacts(self, rows: list[np.ndarray], chunk_ids: list[str]) -> None:
        self.settings.artifact_dir.mkdir(parents=True, exist_ok=True)
        if rows:
            matrix = np.stack(rows).astype(np.float32)
            np.save(self.settings.vector_matrix_path, matrix)
            self.settings.vector_ids_path.write_text(
                json.dumps(chunk_ids, ensure_ascii=False),
                encoding="utf-8",
            )
            return

        empty = np.empty((0, self.settings.embedding_dim), dtype=np.float32)
        np.save(self.settings.vector_matrix_path, empty)
        self.settings.vector_ids_path.write_text("[]", encoding="utf-8")

    def _create_job(self, *, source_type: str) -> str:
        job_id = str(uuid.uuid4())
        started_at = datetime.now(UTC).isoformat()
        with get_state_db_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO ingest_jobs (id, status, source_type, started_at, stats)
                VALUES (?, 'running', ?, ?, ?)
                """,
                (job_id, source_type, started_at, "{}"),
            )
            cursor.close()
            connection.commit()
        return job_id

    def _finish_job(
        self,
        job_id: str,
        *,
        status: str,
        stats: dict[str, object],
        error_log: str | None = None,
    ) -> None:
        with get_state_db_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE ingest_jobs
                SET status = ?, finished_at = ?, stats = ?, error_log = ?
                WHERE id = ?
                """,
                (status, datetime.now(UTC).isoformat(), json.dumps(stats, ensure_ascii=False), error_log, job_id),
            )
            cursor.close()
            connection.commit()

    @staticmethod
    def _safe_file_size(path: Path) -> int:
        return path.stat().st_size if path.exists() else 0
