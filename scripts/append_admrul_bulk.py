"""다수의 admrul 마크다운을 기존 laws.sqlite 에 bulk append.

append_voluntary_code_test.py 의 확장 버전. 디렉토리를 순회하며 한 건씩 append.

사용:
  ARTIFACT_DIR=$(pwd)/data/test-artifacts \\
  PYTHONPATH=packages/python/src \\
  python scripts/append_admrul_bulk.py data/sync/kr/행정규칙
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


def _parse_fm_raw(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    raw = text[4:end]
    meta: dict = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip("'\"")
    return meta


def append_one(md_path: Path, conn: sqlite3.Connection, gemini: GeminiService) -> dict:
    raw = md_path.read_text(encoding="utf-8")
    parsed = parse_law_markdown(md_path, raw)
    fm = _parse_fm_raw(raw)

    document_type = fm.get("document_type", "statute")
    license_policy = fm.get("license_policy", "statute_public")
    citation_mode = fm.get("citation_mode", "full")
    issuer = fm.get("issuer", "")

    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    # 이미 있으면 제거 (멱등)
    conn.execute("DELETE FROM law_chunks WHERE law_id = ?", (parsed.document.law_id,))
    conn.execute("DELETE FROM law_documents WHERE law_id = ?", (parsed.document.law_id,))
    try:
        conn.execute("DELETE FROM law_search_fts WHERE law_id = ?", (parsed.document.law_id,))
    except sqlite3.OperationalError:
        pass  # FTS 테이블 없을 수도

    document_id = parsed.document.content_hash
    conn.execute(
        """
        INSERT INTO law_documents (
          id, law_id, law_mst, title, title_normalized, law_type, ministry,
          promulgation_date, effective_date, status, source_url, created_at, updated_at,
          document_type, license_policy, citation_mode, issuer,
          source, source_fetched_at, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id, parsed.document.law_id, parsed.document.law_mst,
            parsed.document.title,
            normalize_for_search(parsed.document.title).replace(" ", ""),
            parsed.document.law_type,
            json.dumps(parsed.document.ministry or [], ensure_ascii=False),
            parsed.document.promulgation_date.isoformat() if parsed.document.promulgation_date else None,
            parsed.document.effective_date.isoformat() if parsed.document.effective_date else None,
            parsed.document.status, parsed.document.source_url,
            now, now,
            document_type, license_policy, citation_mode, issuer,
            "drf-admrul", now, document_id,
        ),
    )

    new_embeddings: list[tuple[str, np.ndarray]] = []
    for chunk in parsed.chunks:
        conn.execute(
            """
            INSERT INTO law_chunks (
              id, document_id, law_id, chunk_type, chapter_title, section_title,
              article_no, article_title, text, order_index, token_count, has_embedding, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk.content_hash, document_id, parsed.document.law_id,
                chunk.chunk_type, chunk.chapter_title, chunk.section_title,
                chunk.article_no, chunk.article_title, chunk.text,
                chunk.order_index, chunk.token_count, 1, now,
            ),
        )
        if chunk.chunk_type == "article":
            try:
                conn.execute(
                    """
                    INSERT INTO law_search_fts (chunk_id, law_id, law_title, search_text, article_no, article_title)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.content_hash, parsed.document.law_id, parsed.document.title,
                        chunk.search_text, chunk.article_no, chunk.article_title,
                    ),
                )
            except sqlite3.OperationalError:
                pass

        embed_text = f"{parsed.document.title} {chunk.article_title or ''} {chunk.text}"[:2000]
        # 타임아웃/재시도 — 이전에 hang 한 이유 방지
        emb = None
        for attempt in range(3):
            try:
                import signal
                def _timeout_handler(signum, frame):
                    raise TimeoutError(f"embed_text timeout after 30s")
                signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(30)
                try:
                    emb = gemini.embed_text(embed_text)
                finally:
                    signal.alarm(0)
                break
            except (TimeoutError, Exception) as e:
                print(f"  embed retry {attempt+1}/3 ({type(e).__name__}: {str(e)[:50]})")
                if attempt == 2:
                    print(f"  임베딩 스킵: {chunk.content_hash[:8]}")
                    emb = None
                    break
        if emb:
            arr = np.asarray(emb, dtype=np.float32)
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr = arr / norm
            new_embeddings.append((chunk.content_hash, arr))

    return {"doc_id": parsed.document.law_id, "chunks": len(parsed.chunks), "new_embeddings": new_embeddings}


def main(input_dir: str) -> int:
    root = Path(input_dir)
    md_files = sorted(root.rglob("*.md"))
    print(f"대상 마크다운: {len(md_files)}개")

    settings = get_settings()
    gemini = GeminiService()

    conn = sqlite3.connect(settings.law_db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    all_new_embeddings: list[tuple[str, np.ndarray]] = []
    success = fail = 0

    try:
        for i, md in enumerate(md_files, 1):
            try:
                r = append_one(md, conn, gemini)
                all_new_embeddings.extend(r["new_embeddings"])
                success += 1
                if i % 20 == 0:
                    conn.commit()
                    print(f"  [{i}/{len(md_files)}] success={success} fail={fail}")
            except Exception as e:
                fail += 1
                print(f"  [{i}/{len(md_files)}] FAIL {md.name}: {e}")
        conn.commit()
    finally:
        conn.close()

    # 임베딩 append
    vec_path = settings.vector_matrix_path
    ids_path = settings.vector_ids_path
    existing_mat = np.load(vec_path) if vec_path.exists() and vec_path.stat().st_size > 128 else np.empty((0, settings.embedding_dim), dtype=np.float32)
    existing_ids = json.loads(ids_path.read_text(encoding="utf-8")) if ids_path.exists() else []

    ids_set = set(existing_ids)
    filtered = [(cid, arr) for cid, arr in all_new_embeddings if cid not in ids_set]
    if filtered:
        new_mat = np.vstack([existing_mat] + [arr[np.newaxis, :] for _, arr in filtered])
        new_ids = existing_ids + [cid for cid, _ in filtered]
        np.save(vec_path, new_mat)
        ids_path.write_text(json.dumps(new_ids, ensure_ascii=False), encoding="utf-8")
        print(f"embeddings append: +{len(filtered)} (total {len(new_ids)})")

    print()
    print(f"✓ admrul bulk append 완료: success={success} fail={fail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "data/sync/kr/행정규칙"))
