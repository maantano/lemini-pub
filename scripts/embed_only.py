"""
임베딩만 별도로 실행. DB를 건드리지 않음.
진행 상황을 저장해서 중단 후 이어서 할 수 있음.

사용법:
  .venv/bin/python scripts/embed_only.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "python" / "src"))

from law_rag_core.ai import GeminiService
from law_rag_core.settings import get_settings


def main():
    settings = get_settings()
    gemini = GeminiService()

    progress_path = settings.artifact_dir / "embed_progress.json"
    vector_path = settings.vector_matrix_path
    ids_path = settings.vector_ids_path

    # Load progress
    done_ids: dict[str, list[float]] = {}
    if progress_path.exists():
        done_ids = json.loads(progress_path.read_text(encoding="utf-8"))
        print(f"Resuming: {len(done_ids)} already embedded", flush=True)

    # Load chunks from DB
    conn = sqlite3.connect(settings.law_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT lc.id, lc.article_no, lc.article_title, lc.text, ld.title as law_title
        FROM law_chunks lc
        JOIN law_documents ld ON ld.law_id = lc.law_id
        WHERE lc.chunk_type = 'article'
        ORDER BY ld.title, lc.order_index
    """)
    chunks = [dict(row) for row in cursor.fetchall()]
    conn.close()

    print(f"Total article chunks: {len(chunks)}", flush=True)

    # Filter out already done
    remaining = [c for c in chunks if c["id"] not in done_ids]
    print(f"Remaining: {len(remaining)}", flush=True)

    if not remaining:
        print("All done! Writing final files.", flush=True)
        _write_vectors(done_ids, settings)
        return

    # Load chunk_questions if available
    questions: dict[str, list[str]] = {}
    q_path = settings.artifact_dir / "chunk_questions.json"
    if q_path.exists():
        questions = json.loads(q_path.read_text(encoding="utf-8"))
        print(f"Loaded {len(questions)} chunk questions", flush=True)

    start = time.time()
    errors = 0
    save_interval = 500
    batch_size = 50  # Embed 50 chunks per API call

    from google.genai import types as genai_types

    for batch_start in range(0, len(remaining), batch_size):
        batch = remaining[batch_start:batch_start + batch_size]

        # Build enriched texts for batch
        texts = []
        for chunk in batch:
            parts = []
            meta = f"{chunk['law_title']} {chunk['article_no'] or ''} {chunk['article_title'] or ''}".strip()
            if meta:
                parts.append(meta)
            qs = questions.get(chunk["id"], [])
            if qs:
                parts.append("관련 질문: " + " / ".join(qs[:3]))
            parts.append(chunk["text"][:800])
            texts.append(" | ".join(parts))

        try:
            response = gemini.client.models.embed_content(
                model=settings.gemini_embedding_model,
                contents=texts,
                config=genai_types.EmbedContentConfig(
                    output_dimensionality=settings.embedding_dim,
                ),
            )
            embeddings = response.embeddings or []
            for j, emb in enumerate(embeddings):
                vec = np.asarray(emb.values, dtype=np.float32)
                norm = float(np.linalg.norm(vec))
                if norm > 0:
                    vec = vec / norm
                done_ids[batch[j]["id"]] = vec.tolist()
        except Exception as e:
            errors += 1
            err_str = str(e)
            if "429" in err_str or "quota" in err_str.lower():
                print(f"  Rate limit at {batch_start}, waiting 30s...", flush=True)
                time.sleep(30)
            elif errors > 50:
                print(f"  Too many errors ({errors}), saving and stopping.", flush=True)
                break

        done_count = batch_start + len(batch)
        if done_count % save_interval < batch_size:
            progress_path.write_text(json.dumps(done_ids), encoding="utf-8")
            elapsed = time.time() - start
            rate = done_count / elapsed * 60
            eta = (len(remaining) - done_count) / rate if rate > 0 else 0
            print(
                f"  {len(done_ids)}/{len(chunks)} embedded "
                f"({done_count}/{len(remaining)} this run, "
                f"{rate:.0f}/min, ETA {eta:.0f}min, errors={errors})",
                flush=True,
            )

    # Final save
    progress_path.write_text(json.dumps(done_ids), encoding="utf-8")
    elapsed = time.time() - start
    print(f"\nEmbedding done: {len(done_ids)}/{len(chunks)} in {elapsed/60:.1f}min", flush=True)
    print(f"Errors: {errors}", flush=True)

    _write_vectors(done_ids, settings)


def _write_vectors(done_ids: dict[str, list[float]], settings):
    """Write numpy matrix + ids json from done_ids dict."""
    if not done_ids:
        print("No vectors to write.", flush=True)
        return

    chunk_ids = list(done_ids.keys())
    matrix = np.array([done_ids[cid] for cid in chunk_ids], dtype=np.float32)

    np.save(settings.vector_matrix_path, matrix)
    settings.vector_ids_path.write_text(
        json.dumps(chunk_ids, ensure_ascii=False), encoding="utf-8"
    )

    # Update DB has_embedding flags
    conn = sqlite3.connect(settings.law_db_path)
    cursor = conn.cursor()
    cursor.execute("UPDATE law_chunks SET has_embedding = 0")
    for cid in chunk_ids:
        cursor.execute("UPDATE law_chunks SET has_embedding = 1 WHERE id = ?", (cid,))
    conn.commit()
    conn.close()

    print(f"Wrote {len(chunk_ids)} vectors ({matrix.nbytes / 1024 / 1024:.1f}MB)", flush=True)
    print(f"  {settings.vector_matrix_path}", flush=True)
    print(f"  {settings.vector_ids_path}", flush=True)


if __name__ == "__main__":
    main()
