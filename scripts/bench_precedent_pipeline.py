"""판례 수집 파이프라인 범용성 벤치마크.

4개 도메인 질문에 대해 `_search_precedents_as_chunks` 가
top-5 판례로 무엇을 반환하는지 측정. 사람이 관련성 판단.
"""
from __future__ import annotations

import os, sys, time, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "python" / "src"))

from law_rag_core.ai.gemini import _search_precedents_as_chunks, GeminiService  # type: ignore


SCENARIOS = {
    "의료_E6556": """당사가 구상 중인 자가 사용 심전도 측정 의료기기 유통 및 진료 연계 프로세스.
쟁점 2: 환자가 처방 없이 자가측정한 기록을 가져와 병원이 E6556 수가 청구 — 국민건강보험법상 부당청구인지
쟁점 3: 의료기기 제조사가 본인부담금 대납 — 의료법 제27조 제3항 환자 유인 금지 위반인지
쟁점 4: 부정맥 감지 후 병원 안내 — 원격의료 해당 여부""",

    "공정경쟁_설문조사": """사용자 평가 설문조사가 의료기기 거래에 관한 공정경쟁규약 제14조(시장조사)에
따라 적법한지 검토 요청. 조사 대상은 병원 임상병리사, 답례품으로 1만원 상당 제공.
제조사가 직접 수행하는 경우 금품류 제공 가능 여부, 판촉 유인 해당 여부 판단.""",

    "IP_NDA위반": """퇴직 직원이 재직 중 접한 고객 DB 와 소스코드 일부를 USB 로 복사하여
경쟁사에 이직 후 활용한 정황. 비밀유지계약 및 영업비밀 침해 손해배상 청구 가능 여부,
부정경쟁방지법 및 민사상 구제수단 검토.""",

    "노무_부당해고": """근속 8년 직원에게 근무태도 불량을 사유로 징계해고 통보.
경영상 해고 요건, 정당한 이유, 절차적 정당성(소명 기회, 해고예고),
부당해고 구제신청 및 금전보상명령 절차 검토.""",

    # ─── 새 도메인 (기존 하드코딩 힌트 전혀 없음) ───
    "세무_양도소득세": """아파트 1채를 보유한 상태에서 분양권을 취득했고, 잠시 2주택이 된 상태에서
기존 아파트를 매각. 일시적 2주택 비과세 특례 적용 여부, 양도소득세 부과처분의 적법성,
분양권을 주택 수로 보는 시점 기준 판단.""",

    "환경_대기오염물질": """공장에서 허가받은 배출허용기준을 초과하여 대기오염물질을 배출한 사실이
적발되어 사업정지 처분을 받았습니다. 처분의 재량권 일탈 여부, 행정처분 취소소송 가능성,
배출시설 설치허가 취소 사유 해당 여부 검토.""",

    "부동산_임대차보증금": """임차인이 계약기간 중 보증금을 증액하지 않기로 합의했는데, 임대인이
재계약 시점에 보증금 인상과 관리비 별도 청구를 요구. 주택임대차보호법상 증액 한도,
차임증감청구권 행사 요건, 묵시적 갱신 후 전환 가능 여부.""",
}


def run_one(label: str, q: str, svc: GeminiService, limit: int = 5) -> list[dict]:
    print(f"\n{'='*76}\n[{label}]\n{'='*76}")
    t0 = time.time()
    chunks = _search_precedents_as_chunks(q, limit=limit, rerank_with=svc)
    dur = int(time.time() - t0)
    print(f"수집: {len(chunks)}건 ({dur}s)")
    rows = []
    for i, c in enumerate(chunks, 1):
        cn = getattr(c, "article_no", "") or ""
        name = getattr(c, "article_title", "") or ""
        text = getattr(c, "text", "") or ""
        head = text.split("\n")[0] if text else ""
        print(f"  {i}. {cn:22s} {name[:70]}")
        if head:
            print(f"     {head[:90]}")
        rows.append({"rank": i, "case_no": cn, "case_name": name, "header": head})
    return rows


def main() -> None:
    if not os.environ.get("LAW_API_KEY"):
        print("LAW_API_KEY 없음 — .env 로드 필요")
        return
    svc = GeminiService()
    out = {}
    for label, q in SCENARIOS.items():
        out[label] = {"question": q, "top5": run_one(label, q, svc, limit=5)}

    report = ROOT / "scripts" / "out" / "bench_precedent_pipeline.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n\n결과 저장: {report.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
