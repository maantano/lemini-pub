export const examplePrompts = [
  "계약서 검토를 요청 드립니다",
  "법적으로 어떻게 대응해야 하나요?",
  "이 상황에서 손해배상 청구가 가능한가요?",
  "분쟁이 생기면 어떤 절차를 밟아야 하나요?",
  "계약 해지 시 주의할 점은?",
];

export const trustBadges = [
  { label: "법령 전문 수록" },
  { label: "판례 검색 지원" },
  { label: "조문 근거 직접 인용" },
  { label: "AI 근거 기반 답변" },
];

// 서버 review_document 파이프라인 실제 단계와 평균 소요시간(실측 기반).
// Chain 1(스캔) → Chain 1.5(제도 프레임) → Chain 2/2.5(조항별 검토)
// → Chain 3/B/D(판단·리스크·외부 고려 병렬) → merge/format
// 합계 ≈ 135초 — 평균 실측 약 2분 30초와 부합.
export const ANALYSIS_STEPS = [
  "사실관계 정리 및 쟁점을 식별하고 있습니다",
  "제도 프레임을 수립하고 관련 법령·판례를 수집하고 있습니다",
  "조항별 상세 법적 검토를 진행하고 있습니다",
  "판단·리스크·외부 고려사항을 종합하고 있습니다",
  "결과를 정리하고 있습니다",
];

export const ANALYSIS_STEP_DURATIONS_SEC = [20, 25, 50, 30, 10];
