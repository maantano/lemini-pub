from __future__ import annotations

from .types import IngestedLaw


def estimate_ingest_bytes(laws: list[IngestedLaw], embedding_dim: int) -> dict[str, int]:
    documents = len(laws)
    chunks = sum(len(law.chunks) for law in laws)
    text_bytes = 0
    search_bytes = 0
    vector_rows = 0
    for law in laws:
        text_bytes += len(law.document.title.encode("utf-8"))
        for chunk in law.chunks:
            text_bytes += len(chunk.text.encode("utf-8"))
            search_bytes += len(chunk.search_text.encode("utf-8"))
            if chunk.embedding:
                vector_rows += 1

    vector_bytes = vector_rows * embedding_dim * 4
    return {
        "documents": documents,
        "chunks": chunks,
        "vector_rows": vector_rows,
        "estimated_text_bytes": text_bytes,
        "estimated_search_bytes": search_bytes,
        "estimated_vector_bytes": vector_bytes,
        "estimated_total_bytes": text_bytes + search_bytes + vector_bytes,
    }
