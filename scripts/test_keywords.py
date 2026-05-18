"""
키워드 추출 테스트 도구.
질문 → 벡터 키워드 + LLM 키워드 + 판례 검색 결과를 확인.

사용법:
  .venv/bin/python scripts/test_keywords.py "식대 통상임금"
  .venv/bin/python scripts/test_keywords.py  # 기본 테스트셋
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "python" / "src"))

from law_rag_core.retrieval.service import RetrievalService
from law_rag_core.ai import GeminiService
from law_rag_core.precedent import PrecedentService

DEFAULT_QUESTIONS = [
    "식대 18만원 통상임금 포함 여부",
    "전세금 안 돌려주면 어떻게 해?",
    "음주운전 처벌 기준이 뭐야?",
    "부당해고 당하면 어떻게 해?",
    "이혼할 때 재산분할은?",
    "층간소음 해결 방법",
    "온라인 쇼핑 환불 규정",
    "직장 내 성희롱 신고",
]


def test_question(question: str, retrieval: RetrievalService, gemini: GeminiService, precedent: PrecedentService):
    print(f"\n{'='*60}")
    print(f"질문: {question}")
    print(f"{'='*60}")

    # Vector keywords
    chunks = retrieval.retrieve(question)
    vector_laws = list(dict.fromkeys(c.law_title for c in chunks[:6]))
    vector_articles = list(dict.fromkeys(c.article_title for c in chunks[:6] if c.article_title))
    print(f"\n[벡터] 법령: {vector_laws[:3]}")
    print(f"[벡터] 조문: {vector_articles[:3]}")

    # LLM keywords
    llm_kw = gemini.expand_query(question)
    print(f"[LLM]  키워드: {llm_kw}")

    # Merged search keywords
    search_kw = [kw for kw in llm_kw if not kw.endswith("법") and not kw.endswith("령") and not kw.endswith("판례")][:3]
    print(f"[검색] 판례 키워드: {search_kw}")

    # Precedent search
    total = 0
    for kw in search_kw[:3]:
        try:
            r = precedent.search(kw, display=3)
            total += len(r.results)
            if r.results:
                print(f"  \"{kw}\" → {len(r.results)}건: {r.results[0].case_name[:40]}")
            else:
                print(f"  \"{kw}\" → 0건")
        except Exception as e:
            print(f"  \"{kw}\" → Error: {e}")

    print(f"\n총 판례: {total}건")


def main():
    retrieval = RetrievalService()
    gemini = GeminiService()
    precedent = PrecedentService()

    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        test_question(question, retrieval, gemini, precedent)
    else:
        for q in DEFAULT_QUESTIONS:
            test_question(q, retrieval, gemini, precedent)


if __name__ == "__main__":
    main()
