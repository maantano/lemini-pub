from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .settings import get_settings


class VectorStore:
    """NumPy 기반 벡터 검색 엔진. memmap으로 벡터 행렬을 지연 로드하고 cosine similarity로 검색한다."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._matrix: np.ndarray | np.memmap | None = None
        self._chunk_ids: list[str] | None = None

    def is_available(self) -> bool:  # 벡터 검색 활성화 여부 + 아티팩트(.npy, .json) 존재 확인
        return (
            self.settings.enable_vector_search
            and self.settings.vector_matrix_path.exists()
            and self.settings.vector_ids_path.exists()
        )

    def search(self, query_embedding: list[float], *, limit: int) -> list[tuple[str, float]]:
        """쿼리 벡터와 전체 벡터 행렬의 cosine similarity를 계산하여 상위 limit개 (chunk_id, score) 반환."""
        if not self.is_available():
            return []
        matrix, chunk_ids = self._load()
        if matrix.size == 0:
            return []

        query = np.asarray(query_embedding, dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if norm == 0.0:
            return []
        query = query / norm

        scores = matrix @ query
        if scores.ndim != 1:
            scores = np.asarray(scores).reshape(-1)
        count = min(limit, scores.shape[0])
        if count <= 0:
            return []

        indices = np.argpartition(scores, -count)[-count:]
        indices = indices[np.argsort(scores[indices])[::-1]]
        return [(chunk_ids[index], float(scores[index])) for index in indices]

    def _load(self) -> tuple[np.ndarray | np.memmap, list[str]]:
        """벡터 행렬(.npy)과 chunk ID 목록(.json)을 지연 로드한다. 행 수 불일치 시 RuntimeError."""
        if self._matrix is None:
            self._matrix = np.load(self.settings.vector_matrix_path, mmap_mode="r")
        if self._chunk_ids is None:
            self._chunk_ids = json.loads(self.settings.vector_ids_path.read_text(encoding="utf-8"))
        if self._matrix.shape[0] != len(self._chunk_ids):
            raise RuntimeError(
                "Vector artifact mismatch: article_embeddings.npy row count does not match "
                "article_embedding_ids.json."
            )
        return self._matrix, self._chunk_ids
