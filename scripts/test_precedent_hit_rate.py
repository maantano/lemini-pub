"""실제 law.go.kr 판례 API 를 다양한 쿼리로 두드려 hit rate 측정.

사용자 질문: 공정경쟁규약 제14조 시장조사 / 답례품 / 임상병리사 설문조사

목적:
1) 현재 _build_precedent_query 룰 vs LLM expansion 결과물 vs 원문 질문
   세 방식 모두 law.go.kr 에 직접 쿼리하여 건수·샘플을 비교.
2) "어떤 쿼리가 실제로 판례를 물어오는가" 를 범용 파이프라인 설계의 근거로 삼음.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "python" / "src"))

from law_rag_core.precedent import PrecedentService  # type: ignore


QUESTION = """당사가 진행하려는 사용자 평가 설문 조사가 「의료기기 거래에 관한 공정경쟁규약」(이하 '규약') 제14조(시장조사)에 의거하여 적법한지 여부를 확인하고자 합니다.

조사 대상 제품: 메모 패치 2 (당사 제품)
조사 대상자: 메모 패치 2를 실제로 사용 중인 병원 임상병리사
답례품: 1인당 10,000원 상당

규약 제14조 적용의 타당성, 대상자 한정 및 선정의 독립성,
판촉 유인으로 오해받지 않도록 하는 법적 요건."""

# ─── 테스트할 쿼리 세트들 ───────────────────────────────────────
# A. 현재 하드코딩 룰이 뽑을 법한 것 (의료 도메인 편향)
CURRENT_RULE_QUERIES = [
    "경제적 이익",
    "리베이트",
]

# B. 순수 LLM 이 뽑아낼 만한 "판례 DB 어휘" 후보 (수동 시뮬레이션)
#    실제 로직에선 analyze_intent 프롬프트가 이런 것들을 뽑도록 유도해야 함
LLM_EXPANSION_QUERIES = [
    # 공식 법률 용어
    "공정경쟁규약",
    "경제적 이익 제공",
    "리베이트",
    "판촉",
    "환자 유인",
    # 쟁점 명사구 (질문에서 도출)
    "시장조사",
    "의료기기 리베이트",
    "의료기기 판매업자",
    # 조문 기반 가능성
    "의료법 제23조의5",
    "약사법 리베이트",
    "공정거래법 부당한 고객유인",
    # 죄명/결정 유형
    "부당한 고객유인행위",
    "경품류 제공",
    "약식명령 리베이트",
]

# C. 원문 질문 (긴 문장 그대로) — law.go.kr 413/0건 재현용
RAW_QUESTION_QUERIES = [
    QUESTION[:100],
    QUESTION[:60],
]

# D. 단순 명사 fallback
NOUN_FALLBACK_QUERIES = [
    "설문조사",
    "답례품",
    "임상병리사",
    "메모패치",
]


def search_one(svc: PrecedentService, q: str) -> dict:
    """단일 쿼리 실행. 건수와 상위 3개 사건번호·제목 반환."""
    t0 = time.time()
    try:
        resp = svc.search(q, display=5)
        results = list(getattr(resp, "results", []) or [])
        samples = []
        for p in results[:3]:
            samples.append({
                "case_no": getattr(p, "case_number", ""),
                "case_name": getattr(p, "case_name", "")[:60],
                "court": getattr(p, "court_name", ""),
                "date": getattr(p, "judgment_date", ""),
            })
        return {
            "query": q,
            "ok": True,
            "total_count": getattr(resp, "total_count", 0),
            "returned": len(results),
            "samples": samples,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "errors": getattr(resp, "errors", {}) or {},
        }
    except Exception as exc:
        return {
            "query": q,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.time() - t0) * 1000),
        }


def run_set(label: str, queries: list[str]) -> list[dict]:
    print(f"\n{'='*70}\n{label}  ({len(queries)}개 쿼리)\n{'='*70}")
    svc = PrecedentService()

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        future_map = {ex.submit(search_one, svc, q): q for q in queries}
        for fut in as_completed(future_map):
            results.append(fut.result())

    # 원래 순서 유지
    order = {q: i for i, q in enumerate(queries)}
    results.sort(key=lambda r: order.get(r["query"], 999))

    for r in results:
        q_disp = r["query"][:40].ljust(40)
        if not r["ok"]:
            print(f"  ❌ {q_disp} | ERROR: {r.get('error','')}")
            continue
        n = r.get("returned", 0)
        total = r.get("total_count", 0)
        tag = "✅" if n > 0 else "  "
        print(f"  {tag} {q_disp} | returned={n:>2} total={total:>5} ({r['elapsed_ms']}ms)")
        for s in r.get("samples", []):
            print(f"       · {s.get('case_no','')} {s.get('court','')} {s.get('date','')} — {s.get('case_name','')}")
    return results


def summary(all_sets: dict[str, list[dict]]) -> None:
    print(f"\n{'='*70}\n요약\n{'='*70}")
    grand_unique: set[str] = set()
    for label, rs in all_sets.items():
        hits = sum(1 for r in rs if r.get("ok") and r.get("returned", 0) > 0)
        unique_cases: set[str] = set()
        for r in rs:
            for s in r.get("samples", []) or []:
                cn = s.get("case_no")
                if cn:
                    unique_cases.add(cn)
                    grand_unique.add(cn)
        print(f"  {label:45s} hit={hits:>2}/{len(rs)}  unique_cases={len(unique_cases)}")
    print(f"\n  전체 유니크 판례 수: {len(grand_unique)}")


def main() -> None:
    if not os.environ.get("LAW_API_KEY"):
        # 저장된 값이 있을 수 있으니 경고만
        print("⚠ LAW_API_KEY 환경변수 없음. settings 기본값으로 진행.")

    all_sets = {
        "A. 현재 룰(의료 도메인 편향)":   run_set("A. 현재 하드코딩 룰", CURRENT_RULE_QUERIES),
        "B. LLM expansion 시뮬":          run_set("B. LLM expansion 후보", LLM_EXPANSION_QUERIES),
        "C. 원문 질문 (긴 문장)":         run_set("C. 원문 질문 (긴 문장)", RAW_QUESTION_QUERIES),
        "D. 단순 명사 fallback":          run_set("D. 단순 명사 fallback", NOUN_FALLBACK_QUERIES),
    }
    summary(all_sets)

    # JSON 덤프 저장
    out = ROOT / "scripts" / "out" / "prec_hit_test.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(all_sets, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n상세 결과 저장: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
