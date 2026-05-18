"""설문조사 질문으로 실제 review_document 파이프라인 전체 실행.
LLM 이 실제 생성한 응답 + Verify 후 남은 판례(basis.cases) 출력.
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "python" / "src"))

from law_rag_core.ai import GeminiService  # type: ignore
from law_rag_core.retrieval import RetrievalService  # type: ignore


QUESTION = """당사가 진행하려는 사용자 평가 설문 조사가 「의료기기 거래에 관한 공정경쟁규약」(이하 '규약') 제14조(시장조사)에 의거하여 적법한지 여부를 확인하고자 합니다.

1. 설문조사 개요 및 진행 방식

조사 대상 제품: 메모 패치 2 (당사 제품)
조사 대상자: 메모 패치 2를 실제로 사용 중인 병원 임상병리사 (보건의료전문가)
조사 목적: 제품(메모 패치 2)의 사용자 경험 및 고객 센터 서비스에 대한 순수한 사용자 평가와 유용한 정보 수집

진행 방법: 사전 동의를 얻은 임상병리사의 이메일 주소로 구글 스프레드시트 형태의 설문 링크를 발송하여 비대면으로 진행. 조사 과정의 객관성 및 독립성 확보를 위해, 대상자 선정 및 설문조사 기획/수행은 영업/마케팅 활동과 분리된 조직인 품질 서비스 조직에서 진행할 예정입니다.

답례품 제공 계획: 설문 참여자에 대한 사례(답례)로서 1인당 10,000원 상당의 답례품을 제공할 예정입니다.

2. 규약 적용 및 법률 검토 요청 사항

규약 제14조 적용의 타당성: 본 사용자 평가 설문조사가 규약 제14조 (시장조사)로 적절하게 인정될 수 있는지 여부. 만약 제14조가 아니거나 보충적으로 적용될 수 있는 다른 규약 조항(예: 제8조 학술대회 지원 등)이 있는지 여부

대상자 한정 및 선정의 독립성: 조사 대상을 실제 당사 제품을 사용하는 임상병리사로 한정하는 것이 규약에 위배되는지 여부. 사전 동의를 얻은 이메일로 전달하는 방식이 판촉 유인으로 오해받지 않도록, 대상자 선정의 독립성 및 객관성을 확보하기 위해 필수적으로 준수해야 할 법적 요건 및 문서화 지침.
"""


def main() -> None:
    print("=" * 76)
    print("파이프라인 전체 실행 (review_document)")
    print("=" * 76)

    retrieval = RetrievalService()
    gemini = GeminiService()

    # 1) 문서 전달 (retrieve용 초기 chunks)
    print("\n[1] 초기 retrieve …")
    chunks = retrieval.retrieve(QUESTION)
    print(f"    → {len(chunks)} chunks")

    # 2) review_document 실행
    print("\n[2] review_document 실행 (Chain 1→1.5→2 …) — 2~3분 소요")
    result = gemini.review_document(
        document_text=QUESTION,
        question="이 설문조사 계획이 공정경쟁규약에 위배되는지, 어떤 조항이 적용되는지 검토해 주세요.",
        chunks=chunks,
    )

    # 3) 결과 저장
    out = ROOT / "scripts" / "out" / "review_survey_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4) 요약 출력
    print("\n" + "=" * 76)
    print("응답 요약")
    print("=" * 76)
    print(f"\n▸ summary: {result.get('summary','(없음)')[:600]}")
    print(f"\n▸ overall_risk: {result.get('overall_risk','n/a')}")
    print(f"\n▸ review_axes: {result.get('review_axes', [])}")

    print("\n" + "=" * 76)
    print("Observations (issues 별 basis.cases)")
    print("=" * 76)
    total_cases = 0
    for obs in result.get("observations", []) or []:
        loc = obs.get("locator", "?")
        sev = obs.get("severity", "?")
        for issue in obs.get("issues", []) or []:
            basis = issue.get("basis", {}) or {}
            cases = basis.get("cases", []) or []
            if cases:
                print(f"\n[{sev.upper()}] {loc} — {issue.get('concern','')[:80]}")
                for c in cases:
                    court = c.get("court", "")
                    date = c.get("date", "")
                    no = c.get("case_no", "")
                    hold = (c.get("holding_excerpt", "") or "")[:120]
                    print(f"   · {court} {date} 선고 {no}")
                    if hold:
                        print(f"     “{hold}…”")
                    total_cases += 1

    print(f"\n총 인용 판례: {total_cases}건")

    # 5) warnings.dropped_basis
    warnings = result.get("warnings", {}) or {}
    dropped = warnings.get("dropped_basis", []) or []
    print(f"\n▸ Verify 에서 drop 된 basis: {len(dropped)}건")
    for d in dropped[:8]:
        kind = d.get("kind", "")
        reason = d.get("reason", "")
        print(f"   · [{kind}] {reason}")

    print(f"\n전체 결과 저장: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
