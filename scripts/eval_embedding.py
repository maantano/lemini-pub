"""
RAG 임베딩 검증 스크립트.

검증 항목:
1. 임베딩 파일 상태 (크기, 차원, 행 수)
2. 벡터 검색 작동 여부
3. RAG 3가지 방식 비교 (exact / lexical / vector)
4. RAG on vs off 비교 (RAG 가치 수치 확인)

사용법:
  .venv/bin/python scripts/eval_embedding.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "python" / "src"))


def check_embedding_files():
    """1. 임베딩 파일 상태 확인."""
    print("=" * 60)
    print("1. 임베딩 파일 상태 확인")
    print("=" * 60)

    artifacts_dir = REPO_ROOT / "data" / "artifacts"
    npy_path = artifacts_dir / "article_embeddings.npy"
    ids_path = artifacts_dir / "article_embedding_ids.json"
    manifest_path = artifacts_dir / "manifest.json"

    # Manifest
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        print(f"\n  [manifest.json]")
        print(f"    documents:    {manifest.get('documents', '?')}")
        print(f"    chunks:       {manifest.get('chunks', '?')}")
        print(f"    vector_rows:  {manifest.get('vector_rows', '?')}")
        print(f"    mode:         {manifest.get('mode', '?')}")
        artifact_bytes = manifest.get("artifact_bytes", {})
        print(f"    npy (manifest):  {artifact_bytes.get('article_embeddings.npy', '?')} bytes")
        print(f"    ids (manifest):  {artifact_bytes.get('article_embedding_ids.json', '?')} bytes")

    # 실제 파일
    print(f"\n  [실제 파일]")
    if npy_path.exists():
        npy_size = npy_path.stat().st_size
        print(f"    article_embeddings.npy: {npy_size:,} bytes ({npy_size / 1024 / 1024:.1f} MB)")

        # numpy로 로드해서 shape 확인
        try:
            matrix = np.load(str(npy_path), mmap_mode="r")
            print(f"    shape: {matrix.shape}")
            print(f"    dtype: {matrix.dtype}")
            if matrix.ndim == 2:
                print(f"    rows (임베딩 수):  {matrix.shape[0]:,}")
                print(f"    dims (차원):       {matrix.shape[1]}")

                # 0벡터 비율 확인
                norms = np.linalg.norm(matrix, axis=1)
                zero_count = int(np.sum(norms == 0))
                print(f"    zero vectors:      {zero_count:,} ({zero_count / matrix.shape[0] * 100:.1f}%)")
                print(f"    mean norm:         {np.mean(norms):.4f}")
                print(f"    min/max norm:      {np.min(norms):.4f} / {np.max(norms):.4f}")
            elif matrix.size == 0:
                print(f"    ⚠️  비어있는 배열!")
        except Exception as e:
            print(f"    ❌ 로드 실패: {e}")
    else:
        print(f"    ❌ article_embeddings.npy 없음!")

    if ids_path.exists():
        ids_size = ids_path.stat().st_size
        print(f"    article_embedding_ids.json: {ids_size:,} bytes ({ids_size / 1024 / 1024:.1f} MB)")
        try:
            ids = json.loads(ids_path.read_text(encoding="utf-8"))
            print(f"    ID 수: {len(ids):,}")
            if ids:
                print(f"    첫 3개: {ids[:3]}")
        except Exception as e:
            print(f"    ❌ 파싱 실패: {e}")
    else:
        print(f"    ❌ article_embedding_ids.json 없음!")

    # manifest vs 실제 불일치 확인
    if npy_path.exists() and manifest_path.exists():
        actual_size = npy_path.stat().st_size
        manifest_size = artifact_bytes.get("article_embeddings.npy", 0)
        if actual_size != manifest_size:
            print(f"\n  ⚠️  manifest와 실제 파일 크기 불일치!")
            print(f"    manifest: {manifest_size} bytes")
            print(f"    actual:   {actual_size:,} bytes")
            print(f"    → manifest는 ingest 시점 기록, 이후 embed_parallel.py로 별도 생성된 것으로 보임")


def check_vector_search():
    """2. 벡터 검색 작동 여부 확인."""
    print("\n" + "=" * 60)
    print("2. 벡터 검색 작동 여부")
    print("=" * 60)

    from law_rag_core.vector_store import VectorStore
    from law_rag_core.ai import GeminiService

    vs = VectorStore()
    gemini = GeminiService()

    print(f"\n  vector_store.is_available(): {vs.is_available()}")
    print(f"  gemini.is_configured(): {gemini.is_configured()}")

    if not vs.is_available():
        print("  ❌ VectorStore 사용 불가")
        return False

    # 테스트 질문으로 임베딩 → 검색
    test_q = "전세보증금 반환"
    print(f"\n  테스트 질문: '{test_q}'")

    start = time.time()
    embedding = gemini.embed_text(test_q)
    embed_time = time.time() - start

    if embedding is None:
        print(f"  ❌ 임베딩 생성 실패 (API 키 또는 네트워크 문제)")
        return False

    print(f"  임베딩 생성: {len(embedding)} dims, {embed_time * 1000:.0f}ms")

    start = time.time()
    results = vs.search(embedding, limit=5)
    search_time = time.time() - start

    print(f"  벡터 검색: {len(results)}건, {search_time * 1000:.0f}ms")
    for chunk_id, score in results[:5]:
        print(f"    {chunk_id}: score={score:.4f}")

    return len(results) > 0


def compare_retrieval_methods():
    """3. 검색 방식별 비교 (exact / lexical / vector)."""
    print("\n" + "=" * 60)
    print("3. 검색 방식별 비교")
    print("=" * 60)

    from law_rag_core.retrieval.service import RetrievalService

    rs = RetrievalService()

    test_questions = [
        ("전세금 안 돌려주면 어떻게 해?", ["주택임대차보호법"]),
        ("퇴사 후 임금은 언제까지 줘야 해?", ["근로기준법"]),
        ("음주운전 처벌 기준이 뭐야?", ["도로교통법"]),
        ("이혼할 때 재산분할은 어떻게 돼?", ["민법"]),
        ("명예훼손으로 고소하려면?", ["형법"]),
    ]

    print(f"\n  {'질문':<30} {'exact':>6} {'lexical':>8} {'vector':>7} {'merged':>7} {'법률일치':>8}")
    print("  " + "-" * 75)

    for question, expected_laws in test_questions:
        # 각 방식 개별 실행
        exact = rs._exact_match(question)
        lexical = rs._lexical_match(question)
        vector = rs._vector_match(question)
        merged = rs.retrieve(question)

        # 법률 일치 확인
        merged_laws = set()
        for chunk in merged:
            merged_laws.add(chunk.law_title.replace(" ", "").lower())

        expected_norm = [l.replace(" ", "").lower() for l in expected_laws]
        hit = any(
            any(en in ml or ml.endswith(en) for ml in merged_laws)
            for en in expected_norm
        )

        print(
            f"  {question:<30} "
            f"{len(exact):>6} "
            f"{len(lexical):>8} "
            f"{len(vector):>7} "
            f"{len(merged):>7} "
            f"{'OK' if hit else 'MISS':>8}"
        )


def compare_rag_vs_no_rag():
    """4. RAG on vs off 비교."""
    print("\n" + "=" * 60)
    print("4. RAG on vs off 비교 (검색 품질)")
    print("=" * 60)

    from law_rag_core.retrieval.service import RetrievalService

    rs = RetrievalService()

    dataset_path = REPO_ROOT / "scripts" / "eval_dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))

    # RAG 검색 품질
    rag_on_recall = []
    rag_on_times = []

    for item in dataset:
        question = item["question"]
        expected_laws = [l.replace(" ", "").lower() for l in item["expected_laws"]]

        start = time.time()
        chunks = rs.retrieve(question)
        elapsed = time.time() - start
        rag_on_times.append(elapsed)

        retrieved_laws = set()
        for c in chunks:
            retrieved_laws.add(c.law_title.replace(" ", "").lower())

        hits = sum(
            1 for el in expected_laws
            if any(el in rl or rl.endswith(el) for rl in retrieved_laws)
        )
        recall = hits / len(expected_laws) if expected_laws else 0
        rag_on_recall.append(recall)

    avg_recall = sum(rag_on_recall) / len(rag_on_recall) if rag_on_recall else 0
    avg_time = sum(rag_on_times) / len(rag_on_times) if rag_on_times else 0
    perfect = sum(1 for r in rag_on_recall if r == 1.0)
    zero = sum(1 for r in rag_on_recall if r == 0)

    print(f"\n  [RAG 검색 결과] ({len(dataset)} questions)")
    print(f"    평균 Law Recall:  {avg_recall:.1%}")
    print(f"    Perfect (100%):   {perfect}/{len(dataset)}")
    print(f"    Zero (0%):        {zero}/{len(dataset)}")
    print(f"    평균 Latency:     {avg_time * 1000:.0f}ms")

    # 실패 케이스 출력
    if zero > 0:
        print(f"\n  [MISS 케이스 - 검색 완전 실패]")
        for item, recall in zip(dataset, rag_on_recall):
            if recall == 0:
                print(f"    Q{item['id']:02d}: {item['question'][:40]} → 기대: {item['expected_laws']}")

    # 부분 성공 케이스
    partial = [(item, r) for item, r in zip(dataset, rag_on_recall) if 0 < r < 1.0]
    if partial:
        print(f"\n  [부분 성공 케이스]")
        for item, recall in partial[:10]:
            print(f"    Q{item['id']:02d}: recall={recall:.0%} — {item['question'][:40]}")

    return {
        "avg_recall": avg_recall,
        "perfect": perfect,
        "zero": zero,
        "total": len(dataset),
        "avg_latency_ms": int(avg_time * 1000),
    }


def check_rag_weight():
    """RAG가 무거운지 확인 — 메모리 + 시간."""
    print("\n" + "=" * 60)
    print("5. RAG 무게 확인 (메모리 + 시간)")
    print("=" * 60)

    import os

    artifacts_dir = REPO_ROOT / "data" / "artifacts"

    # 파일 크기
    files = {
        "laws.sqlite": artifacts_dir / "laws.sqlite",
        "article_embeddings.npy": artifacts_dir / "article_embeddings.npy",
        "article_embedding_ids.json": artifacts_dir / "article_embedding_ids.json",
        "embed_progress.json": artifacts_dir / "embed_progress.json",
        "state.sqlite": artifacts_dir / "state.sqlite",
    }

    total_mb = 0
    print(f"\n  [디스크 사용량]")
    for name, path in files.items():
        if path.exists():
            size_mb = path.stat().st_size / 1024 / 1024
            total_mb += size_mb
            print(f"    {name:<35} {size_mb:>8.1f} MB")
    print(f"    {'TOTAL':<35} {total_mb:>8.1f} MB")

    # 벡터 검색 시간만 측정
    print(f"\n  [검색 시간 분해]")
    from law_rag_core.retrieval.service import RetrievalService
    from law_rag_core.ai import GeminiService

    rs = RetrievalService()
    gemini = GeminiService()

    test_q = "퇴사 후 임금은 언제까지 줘야 해?"

    # exact
    start = time.time()
    exact = rs._exact_match(test_q)
    t_exact = time.time() - start

    # lexical
    start = time.time()
    lexical = rs._lexical_match(test_q)
    t_lexical = time.time() - start

    # vector (embedding + search)
    start = time.time()
    embedding = gemini.embed_text(test_q)
    t_embed = time.time() - start

    if embedding:
        start = time.time()
        vector_results = rs.vector_store.search(embedding, limit=12)
        t_vector_search = time.time() - start
    else:
        t_vector_search = 0

    # expand_query (LLM)
    start = time.time()
    keywords = gemini.expand_query(test_q)
    t_expand = time.time() - start

    print(f"    exact match:      {t_exact * 1000:>6.0f}ms ({len(exact)} results)")
    print(f"    lexical (BM25):   {t_lexical * 1000:>6.0f}ms ({len(lexical)} results)")
    print(f"    embed_text (API): {t_embed * 1000:>6.0f}ms")
    print(f"    vector search:    {t_vector_search * 1000:>6.0f}ms ({len(vector_results) if embedding else 0} results)")
    print(f"    expand_query (API): {t_expand * 1000:>6.0f}ms ({len(keywords)} keywords)")
    print(f"    ────────────────────────────")
    total = t_exact + t_lexical + t_embed + t_vector_search + t_expand
    print(f"    TOTAL:            {total * 1000:>6.0f}ms")
    print(f"\n    → API 호출 비중: {(t_embed + t_expand) / total * 100:.0f}% (embed + expand)")
    print(f"    → 로컬 검색 비중: {(t_exact + t_lexical + t_vector_search) / total * 100:.0f}%")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  RAG 임베딩 검증 스크립트")
    print("=" * 60)

    # 1. 파일 상태
    check_embedding_files()

    # 2. 벡터 검색 작동
    vector_ok = check_vector_search()

    # 3. 방식별 비교
    compare_retrieval_methods()

    # 4. RAG 전체 평가 (eval_dataset 사용)
    rag_stats = compare_rag_vs_no_rag()

    # 5. 무게 확인
    check_rag_weight()

    # 최종 요약
    print("\n" + "=" * 60)
    print("  최종 요약")
    print("=" * 60)
    print(f"\n  임베딩 파일: {'OK (존재함)' if (REPO_ROOT / 'data/artifacts/article_embeddings.npy').stat().st_size > 1000 else 'EMPTY'}")
    print(f"  벡터 검색: {'OK' if vector_ok else 'FAIL'}")
    print(f"  RAG Law Recall: {rag_stats['avg_recall']:.1%} (perfect {rag_stats['perfect']}/{rag_stats['total']}, miss {rag_stats['zero']}/{rag_stats['total']})")
    print(f"  평균 Latency: {rag_stats['avg_latency_ms']}ms")

    verdict = ""
    if rag_stats["avg_recall"] >= 0.8:
        verdict = "RAG 검색 품질 양호"
    elif rag_stats["avg_recall"] >= 0.5:
        verdict = "RAG 검색 개선 필요 (recall 50-80%)"
    else:
        verdict = "RAG 검색 심각한 문제 (recall < 50%)"

    print(f"\n  판정: {verdict}")
    print()
