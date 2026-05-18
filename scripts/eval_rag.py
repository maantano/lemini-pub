"""
RAG 정확도 평가 스크립트.

사용법:
  .venv/bin/python scripts/eval_rag.py                    # retrieval만 평가 (API 불필요)
  .venv/bin/python scripts/eval_rag.py --with-llm         # retrieval + LLM 답변 평가 (API 필요)
  .venv/bin/python scripts/eval_rag.py --with-llm --api   # API 엔드포인트로 평가
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "python" / "src"))

from law_rag_core.retrieval.service import RetrievalService


def load_eval_dataset() -> list[dict]:
    path = REPO_ROOT / "scripts" / "eval_dataset.json"
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_law_name(name: str) -> str:
    return name.replace(" ", "").replace("·", "").lower()


def normalize_article(article: str) -> str:
    return article.replace(" ", "").replace("제", "").replace("조", "조")


def evaluate_retrieval(dataset: list[dict], top_k: int = 12) -> dict:
    retrieval = RetrievalService()
    results = []

    for item in dataset:
        question = item["question"]
        expected_laws = [normalize_law_name(l) for l in item["expected_laws"]]
        expected_articles = [normalize_article(a) for a in item.get("expected_articles", [])]

        start = time.time()
        chunks = retrieval.retrieve(question)
        elapsed = time.time() - start

        retrieved_laws = set()
        retrieved_articles = set()
        for chunk in chunks[:top_k]:
            retrieved_laws.add(normalize_law_name(chunk.law_title))
            if chunk.article_no:
                retrieved_articles.add(normalize_article(chunk.article_no))

        # Law-level recall (exact match or expected is suffix of retrieved)
        def law_matches(expected: str, retrieved: str) -> bool:
            if expected == retrieved:
                return True
            # "형법" should match "형법" but not "군형법"
            # "민법" should match "민법" but not "난민법"
            return retrieved == expected or (retrieved.endswith(expected) and len(retrieved) > len(expected) and not retrieved[-(len(expected)+1)].isalnum()) and (
                len(retrieved) == len(expected) or not retrieved[-(len(expected)+1)].isalpha()
            )

        law_hits = sum(1 for el in expected_laws if any(law_matches(el, rl) for rl in retrieved_laws))
        law_recall = law_hits / len(expected_laws) if expected_laws else 0

        # Article-level recall
        article_hits = sum(1 for ea in expected_articles if any(ea in ra for ra in retrieved_articles))
        article_recall = article_hits / len(expected_articles) if expected_articles else 0

        # Precision (how many retrieved chunks are from expected laws)
        relevant_chunks = sum(
            1 for chunk in chunks[:top_k]
            if any(law_matches(el, normalize_law_name(chunk.law_title)) for el in expected_laws)
        )
        precision = relevant_chunks / min(len(chunks), top_k) if chunks else 0

        result = {
            "id": item["id"],
            "question": question[:40],
            "law_recall": law_recall,
            "article_recall": article_recall,
            "precision": precision,
            "retrieved_count": len(chunks),
            "latency_ms": int(elapsed * 1000),
            "retrieved_laws": sorted(retrieved_laws)[:5],
            "expected_laws": expected_laws,
        }
        results.append(result)

        status = "OK" if law_recall > 0 else "MISS"
        print(
            f"  [{status}] Q{item['id']:02d}: law_recall={law_recall:.0%} "
            f"article_recall={article_recall:.0%} precision={precision:.0%} "
            f"({len(chunks)} chunks, {int(elapsed*1000)}ms)"
        )

    # Aggregate
    avg_law_recall = sum(r["law_recall"] for r in results) / len(results)
    avg_article_recall = sum(r["article_recall"] for r in results) / len(results)
    avg_precision = sum(r["precision"] for r in results) / len(results)
    avg_latency = sum(r["latency_ms"] for r in results) / len(results)
    perfect_law = sum(1 for r in results if r["law_recall"] == 1.0)
    zero_law = sum(1 for r in results if r["law_recall"] == 0)

    summary = {
        "total_questions": len(results),
        "avg_law_recall": round(avg_law_recall, 3),
        "avg_article_recall": round(avg_article_recall, 3),
        "avg_precision": round(avg_precision, 3),
        "avg_latency_ms": int(avg_latency),
        "perfect_law_recall": perfect_law,
        "zero_law_recall": zero_law,
    }

    return {"summary": summary, "details": results}


def evaluate_with_llm(dataset: list[dict]) -> dict:
    """Full pipeline evaluation via API."""
    import urllib.request

    results = []
    for item in dataset:
        question = item["question"]
        expected_laws = [normalize_law_name(l) for l in item["expected_laws"]]

        start = time.time()
        try:
            req = urllib.request.Request(
                "http://localhost:8000/v1/chat",
                data=json.dumps({
                    "question": question,
                    "stream": False,
                    "save": False,
                    "channel": "web",
                }).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"  [ERR] Q{item['id']:02d}: {e}")
            results.append({"id": item["id"], "error": str(e)})
            continue
        elapsed = time.time() - start

        grounded = data.get("grounded", False)
        citations = data.get("citations", [])
        cited_laws = set(normalize_law_name(c.get("law_title", "")) for c in citations)

        law_hits = sum(1 for el in expected_laws if any(el in cl for cl in cited_laws))
        citation_recall = law_hits / len(expected_laws) if expected_laws else 0

        result = {
            "id": item["id"],
            "question": question[:40],
            "grounded": grounded,
            "citation_count": len(citations),
            "citation_recall": citation_recall,
            "decision_factors": len(data.get("decision_factors", [])),
            "action_items": len(data.get("action_items", [])),
            "latency_ms": int(elapsed * 1000),
        }
        results.append(result)

        status = "OK" if grounded and citation_recall > 0 else "MISS"
        print(
            f"  [{status}] Q{item['id']:02d}: grounded={grounded} "
            f"citations={len(citations)} recall={citation_recall:.0%} "
            f"({int(elapsed*1000)}ms)"
        )

    valid = [r for r in results if "error" not in r]
    summary = {
        "total": len(results),
        "errors": len(results) - len(valid),
        "grounded_rate": round(sum(1 for r in valid if r["grounded"]) / len(valid), 3) if valid else 0,
        "avg_citation_recall": round(sum(r["citation_recall"] for r in valid) / len(valid), 3) if valid else 0,
        "avg_citations": round(sum(r["citation_count"] for r in valid) / len(valid), 1) if valid else 0,
        "avg_latency_ms": int(sum(r["latency_ms"] for r in valid) / len(valid)) if valid else 0,
    }

    return {"summary": summary, "details": results}


if __name__ == "__main__":
    dataset = load_eval_dataset()
    use_llm = "--with-llm" in sys.argv
    use_api = "--api" in sys.argv

    print(f"\n=== RAG Evaluation ({len(dataset)} questions) ===\n")

    print("--- Retrieval Evaluation ---")
    retrieval_result = evaluate_retrieval(dataset)
    s = retrieval_result["summary"]
    print(f"\n  Law Recall:     {s['avg_law_recall']:.1%}")
    print(f"  Article Recall: {s['avg_article_recall']:.1%}")
    print(f"  Precision:      {s['avg_precision']:.1%}")
    print(f"  Avg Latency:    {s['avg_latency_ms']}ms")
    print(f"  Perfect/Zero:   {s['perfect_law_recall']}/{s['zero_law_recall']} out of {s['total_questions']}")

    if use_llm:
        print("\n--- LLM (Full Pipeline) Evaluation ---")
        llm_result = evaluate_with_llm(dataset)
        ls = llm_result["summary"]
        print(f"\n  Grounded Rate:     {ls['grounded_rate']:.1%}")
        print(f"  Citation Recall:   {ls['avg_citation_recall']:.1%}")
        print(f"  Avg Citations:     {ls['avg_citations']}")
        print(f"  Avg Latency:       {ls['avg_latency_ms']}ms")
        print(f"  Errors:            {ls['errors']}")

    # Save results
    output = {"retrieval": retrieval_result}
    if use_llm:
        output["llm"] = llm_result
    out_path = REPO_ROOT / "scripts" / "eval_results.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nResults saved to {out_path}")
