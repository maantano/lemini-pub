"""
병렬 임베딩. 4개 worker로 동시 처리.
embed_progress.json에서 이어서 시작.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "python" / "src"))

from google import genai
from google.genai import types as genai_types
from law_rag_core.settings import get_settings

settings = get_settings()
BATCH_SIZE = 100
NUM_WORKERS = 4
SAVE_EVERY = 2000


def load_chunks():
    conn = sqlite3.connect(settings.law_db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT lc.id, lc.article_no, lc.article_title, lc.text, ld.title as law_title
        FROM law_chunks lc
        JOIN law_documents ld ON ld.law_id = lc.law_id
        WHERE lc.chunk_type = 'article'
        ORDER BY ld.title, lc.order_index
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def build_text(chunk):
    parts = []
    meta = f"{chunk['law_title']} {chunk['article_no'] or ''} {chunk['article_title'] or ''}".strip()
    if meta:
        parts.append(meta)
    parts.append(chunk["text"][:800])
    return " | ".join(parts)


def embed_batch(client, texts):
    resp = client.models.embed_content(
        model=settings.gemini_embedding_model,
        contents=texts,
        config=genai_types.EmbedContentConfig(
            output_dimensionality=settings.embedding_dim,
        ),
    )
    results = []
    for emb in (resp.embeddings or []):
        vec = np.asarray(emb.values, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        results.append(vec.tolist())
    return results


def process_batch(worker_id, client, batch_chunks):
    texts = [build_text(c) for c in batch_chunks]
    try:
        vectors = embed_batch(client, texts)
        return [(batch_chunks[i]["id"], vectors[i]) for i in range(len(vectors))]
    except Exception as e:
        if "429" in str(e):
            time.sleep(10)
            try:
                vectors = embed_batch(client, texts)
                return [(batch_chunks[i]["id"], vectors[i]) for i in range(len(vectors))]
            except:
                return []
        return []


def main():
    progress_path = settings.artifact_dir / "embed_progress.json"

    done_ids = {}
    if progress_path.exists():
        done_ids = json.loads(progress_path.read_text(encoding="utf-8"))
        print(f"Resuming: {len(done_ids)} already done", flush=True)

    chunks = load_chunks()
    remaining = [c for c in chunks if c["id"] not in done_ids]
    print(f"Total: {len(chunks)}, Remaining: {len(remaining)}", flush=True)

    if not remaining:
        print("All done!", flush=True)
        write_vectors(done_ids)
        return

    # Create worker clients
    clients = [genai.Client(api_key=settings.gemini_api_key) for _ in range(NUM_WORKERS)]

    # Split into batches
    batches = []
    for i in range(0, len(remaining), BATCH_SIZE):
        batches.append(remaining[i:i + BATCH_SIZE])

    print(f"Batches: {len(batches)}, Workers: {NUM_WORKERS}, Batch size: {BATCH_SIZE}", flush=True)

    start = time.time()
    processed = 0

    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = {}
        for i, batch in enumerate(batches):
            worker_id = i % NUM_WORKERS
            future = executor.submit(process_batch, worker_id, clients[worker_id], batch)
            futures[future] = i

        for future in as_completed(futures):
            results = future.result()
            for chunk_id, vector in results:
                done_ids[chunk_id] = vector
            processed += 1

            total_done = len(done_ids)
            if total_done % SAVE_EVERY < BATCH_SIZE:
                progress_path.write_text(json.dumps(done_ids), encoding="utf-8")
                elapsed = time.time() - start
                rate = (total_done - (len(done_ids) - len(results))) / elapsed * 60 if elapsed > 0 else 0
                remaining_count = len(chunks) - total_done
                eta = remaining_count / rate if rate > 0 else 0
                print(
                    f"  {total_done}/{len(chunks)} ({total_done*100//len(chunks)}%) "
                    f"rate={rate:.0f}/min ETA={eta:.0f}min",
                    flush=True,
                )

    # Final save
    progress_path.write_text(json.dumps(done_ids), encoding="utf-8")
    elapsed = time.time() - start
    print(f"\nDone: {len(done_ids)}/{len(chunks)} in {elapsed/60:.1f}min", flush=True)

    write_vectors(done_ids)


def write_vectors(done_ids):
    if not done_ids:
        return
    chunk_ids = list(done_ids.keys())
    matrix = np.array([done_ids[cid] for cid in chunk_ids], dtype=np.float32)
    np.save(settings.vector_matrix_path, matrix)
    settings.vector_ids_path.write_text(json.dumps(chunk_ids, ensure_ascii=False), encoding="utf-8")

    conn = sqlite3.connect(settings.law_db_path)
    c = conn.cursor()
    c.execute("UPDATE law_chunks SET has_embedding = 0")
    for cid in chunk_ids:
        c.execute("UPDATE law_chunks SET has_embedding = 1 WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    print(f"Wrote {len(chunk_ids)} vectors ({matrix.nbytes/1024/1024:.1f}MB)", flush=True)


if __name__ == "__main__":
    main()
