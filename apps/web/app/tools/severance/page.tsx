"use client";

import { useState } from "react";
import { SiteShell } from "../../../components/site-shell";

function calcSeverance(startDate: string, endDate: string, avgMonthlyWage: number) {
  const start = new Date(startDate);
  const end = new Date(endDate);
  const diffMs = end.getTime() - start.getTime();
  const diffDays = diffMs / (1000 * 60 * 60 * 24);

  if (diffDays < 365) {
    return { eligible: false, amount: 0, days: Math.floor(diffDays), years: 0, paymentDeadline: "" };
  }

  // 퇴직금 = (1일 평균임금) × 30일 × (총 근속일수 / 365)
  const dailyWage = (avgMonthlyWage * 3) / 90; // 3개월 평균임금 기준 1일 평균임금
  const severance = dailyWage * 30 * (diffDays / 365);

  // 지급 기한: 퇴직일로부터 14일
  const deadline = new Date(end);
  deadline.setDate(deadline.getDate() + 14);

  return {
    eligible: true,
    amount: Math.round(severance),
    days: Math.floor(diffDays),
    years: (diffDays / 365).toFixed(1),
    paymentDeadline: deadline.toLocaleDateString("ko-KR"),
  };
}

function formatNumber(n: number): string {
  return n.toLocaleString("ko-KR");
}

export default function SeveranceCalculatorPage() {
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [wage, setWage] = useState("");
  const [result, setResult] = useState<ReturnType<typeof calcSeverance> | null>(null);

  function handleCalc(e: React.FormEvent) {
    e.preventDefault();
    if (!startDate || !endDate || !wage) return;
    const wageNum = Number(wage.replace(/,/g, ""));
    if (isNaN(wageNum) || wageNum <= 0) return;
    setResult(calcSeverance(startDate, endDate, wageNum));
  }

  return (
    <SiteShell title="퇴직금 계산기" description="입사일, 퇴사일, 평균 월급으로 퇴직금을 계산합니다.">
      <div className="calculator-page">
        <div className="calculator-header">
          <h1 className="calculator-title">퇴직금 계산기</h1>
          <p className="calculator-desc">
            근로기준법 제34조에 따라 1년 이상 근속한 근로자는 퇴직금을 받을 수 있습니다.
          </p>
        </div>

        <form className="calculator-form" onSubmit={handleCalc}>
          <div className="calc-field">
            <label className="calc-label">입사일</label>
            <input
              type="date"
              className="calc-input"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              required
            />
          </div>

          <div className="calc-field">
            <label className="calc-label">퇴사일 (예정일)</label>
            <input
              type="date"
              className="calc-input"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              required
            />
          </div>

          <div className="calc-field">
            <label className="calc-label">최근 3개월 월 평균임금 (세전)</label>
            <div className="calc-input-group">
              <input
                type="text"
                className="calc-input"
                value={wage}
                onChange={(e) => {
                  const raw = e.target.value.replace(/[^0-9]/g, "");
                  setWage(raw ? Number(raw).toLocaleString("ko-KR") : "");
                }}
                placeholder="3,000,000"
                required
              />
              <span className="calc-unit">원</span>
            </div>
            <span className="calc-hint">기본급 + 고정수당 + 기타 수당 포함. 상여금은 연간 합산 후 12로 나눈 금액 포함.</span>
          </div>

          <button type="submit" className="calc-submit">계산하기</button>
        </form>

        {result && (
          <div className="calc-result">
            {result.eligible ? (
              <>
                <div className="calc-result-main">
                  <span className="calc-result-label">예상 퇴직금</span>
                  <strong className="calc-result-amount">{formatNumber(result.amount)}원</strong>
                </div>

                <div className="calc-result-details">
                  <div className="calc-detail-item">
                    <span>총 근속일수</span>
                    <strong>{formatNumber(result.days)}일 (약 {result.years}년)</strong>
                  </div>
                  <div className="calc-detail-item">
                    <span>지급 기한</span>
                    <strong>{result.paymentDeadline}까지</strong>
                  </div>
                  <div className="calc-detail-item">
                    <span>미지급 시 지연이자</span>
                    <strong>연 20% (근로기준법 제37조)</strong>
                  </div>
                </div>

                <div className="calc-legal-info">
                  <h4>관련 법조문</h4>
                  <ul>
                    <li><strong>근로기준법 제34조</strong> — 퇴직급여 지급 의무 (1년 이상 근속)</li>
                    <li><strong>근로기준법 제36조</strong> — 퇴직 후 14일 이내 금품 청산 의무</li>
                    <li><strong>근로기준법 제37조</strong> — 미지급 시 지연이자 연 20%</li>
                  </ul>
                </div>

                <div className="calc-next-steps">
                  <h4>퇴직금을 못 받았다면?</h4>
                  <p>
                    사업주가 14일 이내에 퇴직금을 지급하지 않으면 고용노동부에 진정할 수 있습니다.
                  </p>
                  <a href="/?q=퇴직금+미지급+어떻게+해야+하나요" className="calc-cta">
                    자세한 대응 방법 알아보기 →
                  </a>
                </div>
              </>
            ) : (
              <div className="calc-result-ineligible">
                <strong>퇴직금 수급 요건 미충족</strong>
                <p>
                  총 근속일수가 {formatNumber(result.days)}일로, 1년(365일) 미만입니다.
                  근로기준법 제34조에 따라 1년 이상 계속 근로한 근로자에게 퇴직금이 지급됩니다.
                </p>
              </div>
            )}

            <p className="calc-disclaimer">
              ※ 이 계산은 참고용이며, 정확한 금액은 평균임금 산정 방식에 따라 달라질 수 있습니다.
            </p>
          </div>
        )}
      </div>
    </SiteShell>
  );
}
