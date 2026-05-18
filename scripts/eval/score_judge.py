"""LLM-as-judge로 시스템 답변 vs 변호사 답변 채점.

흐름:
  1. eval-goldset.json + eval-runs/<run_id>/cases/*.json 매칭
  2. 변호사 답변(텍스트 + 첨부 텍스트 합본) 추출
  3. 시스템 답변 텍스트 직렬화
  4. LLM-as-judge: 5축 점수 + coverage 보조 지표
  5. eval-runs/<run_id>/cases/<ts>.scored.json + summary.json 갱신
  6. eval-history.csv에 회차 한 줄 append
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "python" / "src"))

from common import extract_text

QUESTIONS = ROOT / ".local-data" / "eval-questions.json"
ANSWERS = ROOT / ".local-data" / "eval-answers.json"
EVAL_RUNS = ROOT / "eval-runs"
HISTORY_CSV = ROOT / "eval-history.csv"


def get_run_id() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    latest = EVAL_RUNS / "latest.txt"
    if latest.exists():
        return latest.read_text(encoding="utf-8").strip()
    raise SystemExit("usage: score_judge.py <run_id>  (or set eval-runs/latest.txt)")


def serialize_system(result: dict) -> str:
    """시스템 응답 dict을 채점용 텍스트로 직렬화."""
    parts: list[str] = []
    overview = result.get("document_overview") or {}
    if overview.get("nature"):
        parts.append(f"[문서 성격] {overview['nature']}")
    j = result.get("judgment") or {}
    if j.get("is_judgment_question"):
        parts.append(f"[결론] verdict={j.get('verdict')} :: {j.get('short_answer','')}")
        if j.get("reasoning"):
            parts.append(f"[근거] {j['reasoning']}")
        for a in j.get("key_authorities", []) or []:
            parts.append(f"  - {a.get('type')} {a.get('ref')} — {a.get('point')}")
        if j.get("typical_path"):
            parts.append(f"[일반 경로] {j['typical_path']}")
    if result.get("summary"):
        parts.append(f"[요약] {result['summary']}")
    for o in (result.get("observations") or [])[:20]:
        loc = o.get("locator", "")
        for it in (o.get("issues") or [])[:3]:
            c = it.get("concern") or it.get("issue") or ""
            s = it.get("suggestion") or ""
            parts.append(f"[관찰] {loc} :: {c[:200]} → {s[:200]}")
    for g in (result.get("gaps") or [])[:15]:
        parts.append(f"[부족·보완] {g.get('topic','')} — {g.get('reason','')[:120]} → {g.get('suggestion','')[:200]}")
    for ec in (result.get("external_considerations") or [])[:15]:
        parts.append(f"[문서 외] [{ec.get('section_title','')}] {ec.get('topic','')} :: {ec.get('detail','')[:200]} → {ec.get('suggestion','')[:200]}")
    for s in (result.get("risk_scenarios") or [])[:10]:
        parts.append(f"[리스크] {s.get('trigger','')[:200]} ← 원인: {s.get('root_cause','')[:120]} → 예방: {s.get('suggestion','')[:200]}")
    for kp in result.get("key_points", []) or []:
        parts.append(f"[핵심] {kp}")
    for ai in result.get("action_items", []) or []:
        parts.append(f"[행동] {ai}")
    rejected = result.get("rejected_citations") or []
    for r in rejected:
        parts.append(f"[적용X 인용] {r.get('ref','')} — {r.get('reason','')}")
    return "\n".join(parts)


def collect_lawyer_text(case: dict) -> str:
    parts: list[str] = []
    for ans in case.get("lawyer_answers") or []:
        if ans.get("text"):
            parts.append(f"[메시지 by {ans.get('author')}]\n{ans['text']}")
        for f in ans.get("files") or []:
            if f.get("downloaded") and f.get("local_path"):
                fp = ROOT / f["local_path"]
                t = extract_text(fp)
                if t:
                    parts.append(f"\n=== 첨부 답변: {fp.name} ===\n{t[:30000]}\n")
    return "\n\n".join(parts)


JUDGE_PROMPT = """당신은 두 법률 검토 답변을 비교해 점수화하는 평가자다.

채점 원칙:
1. **변호사 답변이 항상 옳다고 가정하지 마라.** 변호사도 누락·오류가 있을 수 있다.
   변호사가 놓친 것을 시스템이 잡았다면 가산하라. 시스템이 놓친 것은 감점하라.
2. 다음 5축에 대해 0~5점으로 채점하라:
   - **conclusion (결론 일치)**: 핵심 판단(yes/no/depends)이 변호사와 같은 방향인가. 가중 1.5x.
   - **reasoning (논거 적절성)**: 적용 법령·판례·논리가 정확하고 사안에 맞는가.
   - **coverage (범위 적절성)**: 변호사 답변에 있는 주요 논점을 시스템이 다뤘는가. 시스템이 추가 짚은 게 의미 있으면 가산, 과잉이면 감점.
   - **practicality (실무성)**: 사용자가 실제 행동 가능한 구체적 권고인가.
   - **safety (안전성)**: 잘못된 법령 인용·주체 적용·방향 반대 권고가 없는가. 가중 1.5x.
3. 각 축에 점수와 한 줄 근거.
4. 마지막에 핵심 차이점(critical_diffs)과 시스템이 잘한 점(system_strengths)을 짧게 정리.

## 변호사 답변 (참고용)
{lawyer_text}

## 시스템 답변 (평가 대상)
{system_text}

## 사용자 질문 (맥락)
{question}

JSON으로만 출력:
{{
  "scores": {{
    "conclusion": {{"score": 0, "rationale": ""}},
    "reasoning": {{"score": 0, "rationale": ""}},
    "coverage": {{"score": 0, "rationale": ""}},
    "practicality": {{"score": 0, "rationale": ""}},
    "safety": {{"score": 0, "rationale": ""}}
  }},
  "critical_diffs": ["변호사와 시스템의 핵심 차이 (있다면)"],
  "system_strengths": ["시스템이 변호사보다 더 잘 다룬 지점 (있다면)"],
  "lawyer_only_points": ["변호사가 짚었는데 시스템이 놓친 핵심 (있다면)"]
}}
"""


def judge(question: str, lawyer_text: str, system_text: str) -> dict:
    from law_rag_core.ai import GeminiService
    from google.genai import types as gtypes
    g = GeminiService()
    prompt = JUDGE_PROMPT.format(
        lawyer_text=lawyer_text[:30000] or "(변호사 답변 없음)",
        system_text=system_text[:30000] or "(시스템 답변 없음)",
        question=question[:5000],
    )
    resp = g.client.models.generate_content(
        model=g.settings.gemini_model,
        contents=prompt,
        config=gtypes.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
        ),
    )
    return json.loads(resp.text or "{}")


def weighted_score(scores: dict) -> float:
    """5축 가중평균을 0~100으로 환산. 결론·안전 1.5x."""
    weights = {
        "conclusion": 1.5,
        "reasoning": 1.0,
        "coverage": 1.0,
        "practicality": 1.0,
        "safety": 1.5,
    }
    total = 0.0
    wsum = 0.0
    for key, w in weights.items():
        s = (scores.get(key) or {}).get("score")
        if isinstance(s, (int, float)):
            total += float(s) * w
            wsum += w
    if wsum == 0:
        return 0.0
    return round((total / wsum) * 20, 2)  # 0~5 → 0~100


def _write_report_md(run_dir: Path, summary: dict, questions: dict) -> None:
    """케이스별 상세 비교 리포트를 마크다운으로 생성한다."""
    lines: list[str] = []
    lines.append(f"# 평가 리포트 — {summary['run_id']}")
    lines.append("")
    lines.append(f"- 채점 케이스: {summary['case_count']}건")
    lines.append(f"- **composite 평균: {summary['composite_avg']} / 100**")
    lines.append("")
    lines.append("| 축 | 평균 (0~5) | 가중치 |")
    lines.append("|---|---|---|")
    weights = {"conclusion": "1.5x", "reasoning": "1.0x", "coverage": "1.0x", "practicality": "1.0x", "safety": "1.5x"}
    labels = {"conclusion": "결론 일치", "reasoning": "논거 적절", "coverage": "범위 적절", "practicality": "실무성", "safety": "안전성"}
    for axis, label in labels.items():
        avg = summary["axis_avg"].get(axis, 0)
        lines.append(f"| {label} | {avg} | {weights[axis]} |")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, case in enumerate(summary.get("cases", [])):
        ts = case["thread_ts"]
        q_preview = case.get("question_preview", "")[:120].replace("\n", " ")
        author = case.get("author", "?")
        posted = case.get("posted_at", "")
        composite = case.get("composite", 0)

        lines.append(f"## 케이스 {i+1} — {composite}점")
        lines.append(f"- **질문**: {q_preview}")
        lines.append(f"- **작성자**: {author} ({posted})")
        lines.append("")

        # 축별 점수 테이블
        lines.append("| 축 | 점수 | 판정 |")
        lines.append("|---|---|---|")
        scores = case.get("scores", {})
        for axis, label in labels.items():
            info = scores.get(axis, {})
            score = info.get("score", "?")
            rationale = info.get("rationale", "")
            lines.append(f"| {label} | **{score}/5** | {rationale} |")
        lines.append("")

        # 핵심 차이
        diffs = case.get("critical_diffs", [])
        if diffs:
            lines.append("**핵심 차이:**")
            for d in diffs:
                lines.append(f"- {d}")
            lines.append("")

        # 시스템 우수 지점
        strengths = case.get("system_strengths", [])
        if strengths:
            lines.append("**시스템 우수 지점:**")
            for s in strengths:
                lines.append(f"- {s}")
            lines.append("")

        # 변호사만 지적한 점
        lawyer_only = case.get("lawyer_only_points", [])
        if lawyer_only:
            lines.append("**변호사만 지적한 점:**")
            for lp in lawyer_only:
                lines.append(f"- {lp}")
            lines.append("")

        lines.append("---")
        lines.append("")

    report_path = run_dir / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"   📄 report: {report_path.relative_to(ROOT)}")


def main() -> None:
    run_id = get_run_id()
    run_dir = EVAL_RUNS / run_id
    cases_dir = run_dir / "cases"
    if not cases_dir.exists():
        raise SystemExit(f"run not found: {run_dir}")
    if not QUESTIONS.exists():
        raise SystemExit("eval-questions.json missing (run collect_questions.py first)")
    if not ANSWERS.exists():
        raise SystemExit("eval-answers.json missing (run collect_answers.py after run_system.py)")

    questions = {c["thread_ts"]: c for c in json.loads(QUESTIONS.read_text(encoding="utf-8"))}
    answers = {a["thread_ts"]: a for a in json.loads(ANSWERS.read_text(encoding="utf-8"))}
    gold: dict[str, dict] = {}
    for ts, q in questions.items():
        a = answers.get(ts)
        if not a:
            continue
        gold[ts] = {**q, **a}

    case_results: list[dict] = []
    for path in sorted(cases_dir.glob("*.json")):
        if path.name.endswith(".scored.json"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = data["thread_ts"]
        c = gold.get(ts)
        if not c:
            print(f"  ! no gold for {ts}")
            continue
        if "error" in data:
            print(f"  - skip errored case {ts}")
            continue

        question = c["question"]["text"]
        lawyer_text = collect_lawyer_text(c)
        system_text = serialize_system(data["system_result"])
        if not lawyer_text.strip():
            print(f"  - skip {ts} (no lawyer text)")
            continue

        print(f"  judging {ts}...")
        try:
            j = judge(question, lawyer_text, system_text)
        except Exception as exc:
            print(f"    ! judge failed: {exc}")
            continue

        scores = j.get("scores") or {}
        composite = weighted_score(scores)
        scored = {
            "thread_ts": ts,
            "composite": composite,
            "scores": scores,
            "critical_diffs": j.get("critical_diffs") or [],
            "system_strengths": j.get("system_strengths") or [],
            "lawyer_only_points": j.get("lawyer_only_points") or [],
        }
        scored_path = path.with_suffix(".scored.json")
        scored_path.write_text(json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8")
        case_results.append(scored)
        print(f"    composite={composite}")
        time.sleep(0.4)

    if not case_results:
        print("no scored cases")
        return

    avg = lambda key: round(
        sum(((s["scores"].get(key) or {}).get("score") or 0) for s in case_results) / len(case_results), 2
    )
    summary = {
        "run_id": run_id,
        "scored_at": time.time(),
        "case_count": len(case_results),
        "composite_avg": round(sum(s["composite"] for s in case_results) / len(case_results), 2),
        "axis_avg": {
            "conclusion": avg("conclusion"),
            "reasoning": avg("reasoning"),
            "coverage": avg("coverage"),
            "practicality": avg("practicality"),
            "safety": avg("safety"),
        },
    }
    # 케이스별 상세를 summary에 포함
    summary["cases"] = []
    for s in case_results:
        ts = s["thread_ts"]
        q_data = questions.get(ts, {})
        q_text = q_data.get("question", {}).get("text", "")[:200]
        q_author = q_data.get("question", {}).get("author", "")
        q_date = q_data.get("posted_at", "")
        summary["cases"].append({
            "thread_ts": ts,
            "question_preview": q_text,
            "author": q_author,
            "posted_at": q_date,
            "composite": s["composite"],
            "scores": s["scores"],
            "critical_diffs": s.get("critical_diffs", []),
            "system_strengths": s.get("system_strengths", []),
            "lawyer_only_points": s.get("lawyer_only_points", []),
        })

    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # 읽기 편한 마크다운 리포트 생성
    _write_report_md(run_dir, summary, questions)

    # history append
    headers = ["run_id", "scored_at", "case_count", "composite_avg",
               "conclusion", "reasoning", "coverage", "practicality", "safety", "git_sha"]
    git_sha = ""
    try:
        import subprocess
        git_sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT)).decode().strip()
    except Exception:
        pass
    row = [
        run_id,
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(summary["scored_at"])),
        summary["case_count"],
        summary["composite_avg"],
        summary["axis_avg"]["conclusion"],
        summary["axis_avg"]["reasoning"],
        summary["axis_avg"]["coverage"],
        summary["axis_avg"]["practicality"],
        summary["axis_avg"]["safety"],
        git_sha,
    ]
    file_exists = HISTORY_CSV.exists()
    with HISTORY_CSV.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(headers)
        w.writerow(row)

    print(f"\n✅ scored {len(case_results)} cases")
    print(f"   composite avg = {summary['composite_avg']} / 100")
    print(f"   axis avg = {summary['axis_avg']}")


if __name__ == "__main__":
    main()
