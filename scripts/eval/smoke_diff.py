"""스모크 A/B 리포트의 정량 diff 분석.

입력: review-reports/smoke-ab-<ts>.md
출력: review-reports/smoke-diff-<ts>.md

LLM 없이 코드로 측정:
1. judgment 개수 (A vs B)
2. verdict 분포
3. observation / gap / external_considerations 개수
4. 변호사 답변에서 추출한 법령·판례·조문 번호를 A와 B가 인용한 rate
5. 섹션별 텍스트 길이
6. 실행 오류 여부
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "review-reports"


# 법령·조문·판례 패턴
STATUTE_RE = re.compile(r"「[^」]+」|\b[가-힣]+법(?:\s*제\s*\d+\s*조(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?)?")
ARTICLE_RE = re.compile(r"제\s*\d+\s*조(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?")
CASE_RE = re.compile(r"(?:대법원|대법|고등법원|헌법재판소|헌재)\s*\d{4}[.·]?\s*\d{1,2}[.·]?\s*\d{1,2}[.·]?\s*선고\s*\d+[가-힣]+\d+")
ORDINANCE_RE = re.compile(r"(?:복지부|보건복지부|고용노동부|공정거래위원회|식약처|식품의약품안전처|개인정보보호위원회)[^\n]*?고시\s*제\s*\d+-\d+\s*호")


def extract_anchors(text: str) -> dict:
    """법령·판례·고시 식별자 추출 (중복 제거)."""
    statutes = set(m.group() for m in STATUTE_RE.finditer(text))
    articles = set(m.group() for m in ARTICLE_RE.finditer(text))
    cases = set(m.group() for m in CASE_RE.finditer(text))
    ordinances = set(m.group() for m in ORDINANCE_RE.finditer(text))
    return {
        "statutes": statutes,
        "articles": articles,
        "cases": cases,
        "ordinances": ordinances,
    }


def parse_section(case_body: str, emoji: str) -> str:
    """4단 섹션 중 하나 추출 (질문/변호사/A/B)."""
    m = re.search(rf"{emoji}[^<]*?</summary>\s*(.*?)</details>", case_body, re.S)
    return m.group(1).strip() if m else ""


def count_judgments(body: str) -> list[str]:
    """B 응답의 judgment 라벨·verdict 리스트."""
    m = re.search(r"### 판단\n(.*?)(?=\n### |\n</details|\Z)", body, re.S)
    if not m:
        return []
    out = []
    for lm in re.finditer(r"\*\*([^*]+)\*\* \[([a-z_]+)\]", m.group(1)):
        out.append(f"{lm.group(1).strip()}→{lm.group(2)}")
    return out


def count_section(body: str, section: str) -> int:
    """'상세 검토 (N건)' 등의 숫자 파싱."""
    m = re.search(rf"### {section} \((\d+)건\)", body)
    return int(m.group(1)) if m else 0


def count_axes(body: str) -> int:
    """선언된 축 수."""
    m = re.search(r"### 선언된 축\n(.*?)(?=\n### |\n</details|\Z)", body, re.S)
    if not m:
        return 0
    return len(re.findall(r"^- \*\*", m.group(1), re.M))


def overall_risk(body: str) -> str:
    m = re.search(r"\*\*전체 위험도\*\*:\s*(\S+)", body)
    return m.group(1) if m else "?"


def is_error(body: str) -> bool:
    return "[실행 오류]" in body


def main(report_path: Path) -> None:
    text = report_path.read_text()
    cases = re.split(r"(?m)^## 케이스", text)[1:]

    lines: list[str] = []
    lines.append(f"# 스모크 A/B diff 분석\n")
    lines.append(f"원본: `{report_path.name}`\n")
    lines.append(f"케이스 수: {len(cases)}\n")
    lines.append("---\n")

    # 전체 요약 표
    summary_rows: list[dict] = []

    for i, c in enumerate(cases, 1):
        ts_m = re.search(r"`([\d.]+)`", c)
        ts = ts_m.group(1) if ts_m else "?"
        q = parse_section(c, "📩 질문")
        lawyer = parse_section(c, "⚖️ 변호사")
        a = parse_section(c, "🅰️ A 응답")
        b = parse_section(c, "🅱️ B 응답")

        # 앵커 추출
        lawyer_anchors = extract_anchors(lawyer)
        a_anchors = extract_anchors(a)
        b_anchors = extract_anchors(b)

        def coverage(response: dict, gold: dict, key: str) -> tuple[int, int]:
            gs = gold[key]
            if not gs:
                return (0, 0)
            hit = len(gs & response[key])
            return (hit, len(gs))

        def cov_str(response: dict, gold: dict, key: str) -> str:
            hit, total = coverage(response, gold, key)
            if total == 0:
                return "n/a"
            return f"{hit}/{total} ({hit*100//total}%)"

        a_judgments = count_judgments(a)
        b_judgments = count_judgments(b)

        row = {
            "idx": i,
            "ts": ts,
            "a_error": is_error(a),
            "b_error": is_error(b),
            "a_obs": count_section(a, "상세 검토"),
            "b_obs": count_section(b, "상세 검토"),
            "a_gaps": count_section(a, "부족·보완"),
            "b_gaps": count_section(b, "부족·보완"),
            "a_ext": count_section(a, "문서 외 고려사항"),
            "b_ext": count_section(b, "문서 외 고려사항"),
            "a_scen": count_section(a, "리스크 시나리오"),
            "b_scen": count_section(b, "리스크 시나리오"),
            "a_axes": count_axes(a),
            "b_axes": count_axes(b),
            "a_risk": overall_risk(a),
            "b_risk": overall_risk(b),
            "a_jdg": a_judgments,
            "b_jdg": b_judgments,
            "a_stat_cov": cov_str(a_anchors, lawyer_anchors, "statutes"),
            "b_stat_cov": cov_str(b_anchors, lawyer_anchors, "statutes"),
            "a_art_cov": cov_str(a_anchors, lawyer_anchors, "articles"),
            "b_art_cov": cov_str(b_anchors, lawyer_anchors, "articles"),
            "a_case_cov": cov_str(a_anchors, lawyer_anchors, "cases"),
            "b_case_cov": cov_str(b_anchors, lawyer_anchors, "cases"),
            "a_len": len(a),
            "b_len": len(b),
            "lawyer_len": len(lawyer),
            "lawyer_stats": len(lawyer_anchors["statutes"]),
            "lawyer_arts": len(lawyer_anchors["articles"]),
            "lawyer_cases": len(lawyer_anchors["cases"]),
        }
        summary_rows.append(row)

    # 상단 한눈표
    lines.append("## 한눈 표\n")
    lines.append("| # | 응답길이 A→B | obs A→B | gap A→B | ext A→B | scen A→B | axes A→B | judgment A→B | 법령커버 A→B | 조문커버 A→B | 판례커버 A→B | 오류 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in summary_rows:
        err = ""
        if r["a_error"]:
            err += "A❌"
        if r["b_error"]:
            err += "B❌"
        lines.append(
            f"| {r['idx']} "
            f"| {r['a_len']}→{r['b_len']} "
            f"| {r['a_obs']}→{r['b_obs']} "
            f"| {r['a_gaps']}→{r['b_gaps']} "
            f"| {r['a_ext']}→{r['b_ext']} "
            f"| {r['a_scen']}→{r['b_scen']} "
            f"| {r['a_axes']}→{r['b_axes']} "
            f"| {len(r['a_jdg'])}→{len(r['b_jdg'])} "
            f"| {r['a_stat_cov']}→{r['b_stat_cov']} "
            f"| {r['a_art_cov']}→{r['b_art_cov']} "
            f"| {r['a_case_cov']}→{r['b_case_cov']} "
            f"| {err or '-'} |"
        )
    lines.append("")

    # 개선/역행 요약
    improved_jdg = sum(1 for r in summary_rows if len(r["b_jdg"]) > len(r["a_jdg"]))
    regressed_jdg = sum(1 for r in summary_rows if len(r["b_jdg"]) < len(r["a_jdg"]))
    b_errors = sum(1 for r in summary_rows if r["b_error"])
    a_errors = sum(1 for r in summary_rows if r["a_error"])

    lines.append("## 집계\n")
    lines.append(f"- judgment 개수 **증가**: {improved_jdg}/{len(summary_rows)}건")
    lines.append(f"- judgment 개수 **감소**: {regressed_jdg}/{len(summary_rows)}건")
    lines.append(f"- A 실행 오류: {a_errors}건 / B 실행 오류: {b_errors}건\n")

    # 케이스별 상세
    lines.append("## 케이스별 상세\n")
    for r in summary_rows:
        lines.append(f"### 케이스 {r['idx']} — `{r['ts']}`\n")
        if r["a_error"] or r["b_error"]:
            lines.append(f"⚠ 실행 오류: A={r['a_error']}, B={r['b_error']}")
            continue

        lines.append(f"- 변호사 답변: {r['lawyer_len']}자, 법령 {r['lawyer_stats']}개/조문 {r['lawyer_arts']}개/판례 {r['lawyer_cases']}개 언급")
        lines.append(f"- A: 응답 {r['a_len']}자 / obs {r['a_obs']} / gap {r['a_gaps']} / axes {r['a_axes']}")
        lines.append(f"- B: 응답 {r['b_len']}자 / obs {r['b_obs']} / gap {r['b_gaps']} / axes {r['b_axes']}")
        lines.append(f"- overall_risk: A={r['a_risk']} → B={r['b_risk']}")
        lines.append(f"- A judgments ({len(r['a_jdg'])}): {', '.join(r['a_jdg']) or '(없음)'}")
        lines.append(f"- B judgments ({len(r['b_jdg'])}): {', '.join(r['b_jdg']) or '(없음)'}")
        lines.append(f"- 앵커 커버리지 (변호사 언급 대비): 법령 {r['a_stat_cov']}→{r['b_stat_cov']}, 조문 {r['a_art_cov']}→{r['b_art_cov']}, 판례 {r['a_case_cov']}→{r['b_case_cov']}")
        lines.append("")

    out = REPORTS / f"smoke-diff-{report_path.stem.replace('smoke-ab-', '')}.md"
    out.write_text("\n".join(lines))
    print(f"✅ diff 리포트: {out}")
    print(f"   크기: {out.stat().st_size // 1024}KB")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
    else:
        # 최신 smoke-ab 파일
        cands = sorted(REPORTS.glob("smoke-ab-*.md"))
        if not cands:
            print("smoke-ab 리포트가 없습니다.")
            sys.exit(1)
        p = cands[-1]
    main(p)
