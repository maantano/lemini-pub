"""자율규약 마크다운 1건을 기존 laws.sqlite 에 append (테스트용).

IngestService 는 파괴적 재생성 방식이라 기존 5,568 법령이 사라진다.
이 스크립트는 **기존 DB/임베딩을 보존**하고 자율규약 1건만 추가한다.

사용법:
    ARTIFACT_DIR=data/test-artifacts \\
    GEMINI_API_KEY=... \\
    python scripts/append_voluntary_code_test.py data/test-voluntary/kr/의료기기공정경쟁규약/자율규약.md

검증 목적:
    자율규약이 law_documents + law_chunks + 임베딩에 들어간 후
    ChatService.answer 가 해당 규약을 인용하는지 확인.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/python/src"))

from law_rag_core.ai import GeminiService
from law_rag_core.normalization import normalize_for_search
from law_rag_core.parser import parse_law_markdown
from law_rag_core.settings import get_settings


def main(md_path: str) -> None:
    path = Path(md_path)
    raw = path.read_text(encoding="utf-8")
    parsed = parse_law_markdown(path, raw)

    settings = get_settings()
    gemini = GeminiService()

    db_path = settings.law_db_path
    if not db_path.exists():
        raise RuntimeError(f"laws.sqlite 없음: {db_path}")

    # 1) front-matter 의 자율규약 메타 추출 (parser 가 모르는 필드)
    metadata, _ = _parse_front_matter_raw(raw)
    document_type = metadata.get("document_type", "statute")
    license_policy = metadata.get("license_policy", "statute_public")
    citation_mode = metadata.get("citation_mode", "full")
    issuer = metadata.get("issuer", "")
    authority_endorsement = metadata.get("authority_endorsement", "")

    print(f"[1] parsed: title={parsed.document.title} document_type={document_type} chunks={len(parsed.chunks)}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        cur = conn.cursor()
        now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

        # 이미 있으면 먼저 지움 (멱등)
        cur.execute(
            "DELETE FROM law_chunks WHERE law_id = ?",
            (parsed.document.law_id,),
        )
        cur.execute(
            "DELETE FROM law_documents WHERE law_id = ?",
            (parsed.document.law_id,),
        )

        document_id = parsed.document.content_hash
        cur.execute(
            """
            INSERT INTO law_documents (
              id, law_id, law_mst, title, title_normalized, law_type, ministry,
              promulgation_date, effective_date, status, source_url, created_at, updated_at,
              document_type, license_policy, citation_mode, issuer, authority_endorsement,
              source, source_fetched_at, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                parsed.document.law_id,
                parsed.document.law_mst,
                parsed.document.title,
                normalize_for_search(parsed.document.title).replace(" ", ""),
                parsed.document.law_type,
                json.dumps(parsed.document.ministry or [], ensure_ascii=False),
                parsed.document.promulgation_date.isoformat() if parsed.document.promulgation_date else None,
                parsed.document.effective_date.isoformat() if parsed.document.effective_date else None,
                parsed.document.status,
                parsed.document.source_url,
                now, now,
                document_type,
                license_policy,
                citation_mode,
                issuer,
                authority_endorsement,
                "manual-test",
                now,
                document_id,
            ),
        )
        print(f"[2] law_documents insert OK (law_id={parsed.document.law_id})")

        # 2) chunks insert
        inserted = 0
        new_embeddings: list[tuple[str, np.ndarray]] = []
        for chunk in parsed.chunks:
            cur.execute(
                """
                INSERT INTO law_chunks (
                  id, document_id, law_id, chunk_type, chapter_title, section_title,
                  article_no, article_title, text, order_index, token_count, has_embedding, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk.content_hash,
                    document_id,
                    parsed.document.law_id,
                    chunk.chunk_type,
                    chunk.chapter_title,
                    chunk.section_title,
                    chunk.article_no,
                    chunk.article_title,
                    chunk.text,
                    chunk.order_index,
                    chunk.token_count,
                    1,
                    now,
                ),
            )

            # FTS
            if chunk.chunk_type == "article":
                cur.execute(
                    """
                    INSERT INTO law_search_fts (chunk_id, law_id, law_title, search_text, article_no, article_title)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.content_hash,
                        parsed.document.law_id,
                        parsed.document.title,
                        chunk.search_text,
                        chunk.article_no,
                        chunk.article_title,
                    ),
                )
            inserted += 1

            # 임베딩
            embed_text = f"{parsed.document.title} {chunk.article_title or ''} {chunk.text}"[:2000]
            emb = gemini.embed_text(embed_text)
            if emb:
                arr = np.asarray(emb, dtype=np.float32)
                norm = np.linalg.norm(arr)
                if norm > 0:
                    arr = arr / norm
                new_embeddings.append((chunk.content_hash, arr))

        conn.commit()
        print(f"[3] {inserted} chunks inserted")

    finally:
        conn.close()

    # 3) 벡터 파일에 append
    vec_path = settings.vector_matrix_path
    ids_path = settings.vector_ids_path

    existing_mat = np.load(vec_path) if vec_path.exists() and vec_path.stat().st_size > 128 else np.empty((0, settings.embedding_dim), dtype=np.float32)
    existing_ids = json.loads(ids_path.read_text(encoding="utf-8")) if ids_path.exists() else []

    ids_set = set(existing_ids)
    filtered = [(cid, arr) for cid, arr in new_embeddings if cid not in ids_set]
    if filtered:
        new_mat = np.vstack([existing_mat] + [arr[np.newaxis, :] for _, arr in filtered])
        new_ids = existing_ids + [cid for cid, _ in filtered]
        np.save(vec_path, new_mat)
        ids_path.write_text(json.dumps(new_ids, ensure_ascii=False), encoding="utf-8")
        print(f"[4] embeddings appended: {len(filtered)} new (total {len(new_ids)})")
    else:
        print("[4] embeddings: 추가된 것 없음 (이미 있음)")

    print()
    print(f"✓ 자율규약 '{parsed.document.title}' 투입 완료")


def _parse_front_matter_raw(text: str) -> tuple[dict, str]:
    """한글 필드 + 자율규약 신규 필드를 모두 추출 (기존 parser 가 모르는 document_type 등)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    meta: dict = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip("'\"")
    return meta, text[end+5:]


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/test-voluntary/kr/의료기기공정경쟁규약/자율규약.md")
