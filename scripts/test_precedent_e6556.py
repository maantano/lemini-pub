"""E6556 (자가측정 심전도 / 수가청구 / 본인부담금 대납 / 원격의료) 질문에 대해
현재 쿼리 빌더 + LLM expansion 양측으로 law.go.kr 을 두드려 실제 판례 수집 가능성을 측정.
"""
from __future__ import annotations

import json, os, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "python" / "src"))

from law_rag_core.precedent import PrecedentService  # type: ignore
from law_rag_core.ai.gemini import _build_precedent_query, GeminiService  # type: ignore


QUESTION = """당사가 구상 중인 자가 사용 심전도 측정 의료기기 유통 및 진료 연계 프로세스가
의료기기법, 의료법, 국민건강보험법 및 관련 수가 기준에 따라 병원 측면에서 발생할 수 있는
법적 리스크를 검토하고 자문을 구하고자 합니다.

쟁점 1: 환자가 의사의 사전 처방 없이 자가 측정한 심전도 기록을 병원 의사가 진료에 참고 자료로
활용하는 것이 의료법상 적법한지.
쟁점 2: 환자가 처방 없이 구매하여 자가 측정한 기록을 가져온 경우, 병원이 이를 판독하고
E6556(심전도 감시) 수가를 청구하는 것이 국민건강보험법상 부당청구인지.
쟁점 3: 의료기기 제조사(휴이노)가 병원에 발생한 환자 본인부담금을 대납하는 행위가
의료법 제27조 제3항(영리목적 환자유인) 및 공정경쟁규약 위반인지.
쟁점 4: 시스템이 부정맥을 감지하고 병원을 안내하는 행위가 의료법상 원격의료에 해당하는지."""


# A) 현재 _build_precedent_query 가 뽑는 쿼리들 (실제 프로덕션 로직)
#    analyze_intent 가 LLM 호출이라 비용/시간 있으니 옵션 분기.
def collect_current_queries(with_llm: bool) -> list[str]:
    svc = GeminiService() if with_llm else None
    qs = _build_precedent_query(QUESTION, gemini_service=svc)
    return qs


# B) 제안 방식 — LLM 에 "판례 검색 DB 어휘로 확장" 지시 (도메인 힌트 없음)
def collect_expansion_queries() -> list[str]:
    """독립 LLM 호출로 이 질문에 대한 판례 검색 후보 8-12개 생성.
    도메인 힌트·예시 없음, 순수 지시만.
    """
    svc = GeminiService()
    prompt = f"""다음 법률 질문을 대한민국 판례 검색 DB(law.go.kr) 에서 검색 가능한
짧은 법률 쟁점 명사구 후보로 확장하라.

## 규칙
- 각 후보 2~10자 (조사·어미 제거)
- 동일 쟁점을 여러 표현으로: 구어체 / 공식 용어 / 죄명 / 조문 어휘
- 도메인 예시는 주지 않는다. 질문에서 스스로 도출
- JSON 배열만 출력. 8~12개
- 법령명(예: "의료법") 단독 금지 — 구체 쟁점명만

## 질문
{QUESTION}

## 출력 형식
["쟁점명1", "쟁점명2", ...]
"""
    try:
        # GeminiService 내부 helper 사용
        raw = svc._call_review(prompt)  # type: ignore[attr-defined]
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        if isinstance(raw, dict) and "queries" in raw:
            return [str(x).strip() for x in raw["queries"]]
        # 문자열로 오면 json 파싱
        if isinstance(raw, str):
            import re
            m = re.search(r"\[.*?\]", raw, flags=re.DOTALL)
            if m:
                return [str(x).strip() for x in json.loads(m.group(0))]
        return []
    except Exception as e:
        print(f"  (LLM expansion 실패: {e})")
        return []


def search_one(svc: PrecedentService, q: str) -> dict:
    t0 = time.time()
    try:
        resp = svc.search(q, display=5)
        results = list(getattr(resp, "results", []) or [])
        samples = []
        for p in results[:3]:
            samples.append({
                "case_no": getattr(p, "case_number", ""),
                "case_name": (getattr(p, "case_name", "") or "")[:70],
                "court": getattr(p, "court_name", ""),
                "date": getattr(p, "judgment_date", ""),
            })
        return dict(query=q, ok=True,
                    total=getattr(resp, "total_count", 0),
                    returned=len(results),
                    samples=samples,
                    ms=int((time.time() - t0) * 1000))
    except Exception as exc:
        return dict(query=q, ok=False, error=f"{type(exc).__name__}: {exc}",
                    ms=int((time.time() - t0) * 1000))


def run_set(label: str, queries: list[str]) -> list[dict]:
    print(f"\n{'='*72}\n{label}  ({len(queries)}개)\n{'='*72}")
    if not queries:
        print("  (쿼리 없음)")
        return []
    svc = PrecedentService()
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(search_one, svc, q): q for q in queries}
        for f in as_completed(futs):
            out.append(f.result())
    order = {q: i for i, q in enumerate(queries)}
    out.sort(key=lambda r: order.get(r["query"], 999))

    for r in out:
        q = (r["query"][:42]).ljust(42)
        if not r["ok"]:
            print(f"  ❌ {q} | {r.get('error','')[:60]}")
            continue
        tag = "✅" if r["returned"] > 0 else "  "
        print(f"  {tag} {q} | ret={r['returned']:>2} tot={r['total']:>5} ({r['ms']}ms)")
        for s in r["samples"]:
            print(f"       · {s['case_no']} {s['court']} {s['date']} — {s['case_name']}")
    return out


def union_cases(all_results: list[list[dict]]) -> dict[str, dict]:
    union: dict[str, dict] = {}
    for rs in all_results:
        for r in rs:
            for s in r.get("samples", []) or []:
                cn = s.get("case_no")
                if cn and cn not in union:
                    union[cn] = s
    return union


def main() -> None:
    t_start = time.time()

    # 1) 현재 프로덕션 쿼리 빌더 (LLM 포함)
    print("\n[1/3] 현재 _build_precedent_query (LLM analyze_intent 포함) 로딩 중...")
    current_qs = collect_current_queries(with_llm=True)
    print(f"  생성된 쿼리: {current_qs}")

    # 2) 제안 방식 — 범용 LLM expansion
    print("\n[2/3] 제안 LLM expansion 생성 중...")
    expansion_qs = collect_expansion_queries()
    print(f"  생성된 쿼리({len(expansion_qs)}): {expansion_qs}")

    # 3) 각각 law.go.kr 에 쿼리
    r1 = run_set("A. 현재 프로덕션 쿼리", current_qs)
    r2 = run_set("B. 제안 LLM expansion",  expansion_qs)

    # 4) 비교 요약
    print(f"\n{'='*72}\n요약\n{'='*72}")
    u1 = union_cases([r1])
    u2 = union_cases([r2])
    h1 = sum(1 for r in r1 if r.get("returned", 0) > 0)
    h2 = sum(1 for r in r2 if r.get("returned", 0) > 0)
    print(f"  A 현재 프로덕션  : hit={h1}/{len(r1)}  unique_cases={len(u1)}")
    print(f"  B 제안 expansion : hit={h2}/{len(r2)}  unique_cases={len(u2)}")
    overlap = set(u1) & set(u2)
    a_only = set(u1) - set(u2)
    b_only = set(u2) - set(u1)
    print(f"  교집합: {len(overlap)}건, A전용: {len(a_only)}, B전용: {len(b_only)}")
    print(f"\n  총 유니크 판례: {len(set(u1) | set(u2))}")
    print(f"  소요: {int(time.time() - t_start)}초")

    out_path = ROOT / "scripts" / "out" / "prec_e6556_test.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "current_queries": current_qs,
        "expansion_queries": expansion_qs,
        "current_results": r1,
        "expansion_results": r2,
        "union_cases_current": list(u1),
        "union_cases_expansion": list(u2),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  결과 저장: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
