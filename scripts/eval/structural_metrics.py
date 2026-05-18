"""eval run 결과를 regex + DB 대조로 구조적 지표 산출.

Judge LLM 없이 결정론적으로 측정:
  - 사건번호 패턴 개수 / precedent_doc_cache 실존 여부 (verified)
  - 고시번호 패턴 개수 / law_documents.promulgation_no 실존 여부
  - 조문 인용 패턴 개수
  - 자율규약·공정경쟁규약·표준약관 언급 횟수
  - 금지어 ("위반 가능성 매우 높음", "실현 불가능" 등)
  - 조건부 판단 표현 ("~에 해당한다", "~로 해석된다", "단서에 따라" 등)

사용:
  python scripts/eval/structural_metrics.py [<run_id>]
    run_id 생략 시 eval-runs/latest.txt 사용
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_RUNS = ROOT / "eval-runs"
DB_PATH = ROOT / "data" / "artifacts" / "laws.sqlite"

CASE_NO_RE = re.compile(r"\d{4}\s*[다도누가두마머부상호감]\s*[\dA-Za-z가-힣]+")
ORDINANCE_NO_RE = re.compile(r"제\s*\d{4}\s*-\s*\d+\s*호")
ARTICLE_RE = re.compile(r"제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?")
SUPREME_COURT_RE = re.compile(r"대법원\s*\d{4}\s*[.\s]\s*\d+\s*[.\s]\s*\d+\s*[.\s]?\s*선고")

VOLUNTARY_HINTS = ["공정경쟁규약", "자율규약", "표준약관", "자율준수", "행동강령"]

FORBIDDEN_PHRASES = [
    "위반 가능성 매우 높음",
    "위반 가능성이 매우 높",
    "실현 불가능",
    "매우 위험",
    "반드시 위반",
]

CONDITIONAL_PHRASES = [
    "에 해당한다",
    "에 해당할 수 있",
    "해당하지 아니한",
    "로 해석된다",
    "로 해석될 여지",
    "단서에 따라",
    "단서의 예외",
    "조건부",
    "법령보충적",
    "대외적 구속력",
]


def collect_text(system_result: dict) -> str:
    """system_result의 모든 텍스트 필드를 합친다."""
    if not isinstance(system_result, dict):
        return ""
    return json.dumps(system_result, ensure_ascii=False)


def count_patterns(text: str) -> dict:
    return {
        "case_no_pattern": len(CASE_NO_RE.findall(text)),
        "supreme_court_citation": len(SUPREME_COURT_RE.findall(text)),
        "ordinance_number_pattern": len(ORDINANCE_NO_RE.findall(text)),
        "article_pattern": len(ARTICLE_RE.findall(text)),
        "voluntary_mentions": sum(text.count(k) for k in VOLUNTARY_HINTS),
        "forbidden_phrases": {p: text.count(p) for p in FORBIDDEN_PHRASES if p in text},
        "conditional_phrases": {p: text.count(p) for p in CONDITIONAL_PHRASES if p in text},
    }


def verify_against_db(text: str, conn: sqlite3.Connection) -> dict:
    """텍스트에서 뽑은 사건번호·고시번호 대조.

    - 사건번호: 판례는 law.go.kr 실시간 API 에서 가져오고 DB 에 캐싱 안 함 (범용성·저작권).
      따라서 DB 대조는 의미 없음 — unique 만 집계.
    - 고시번호: law_documents.promulgation_no 에 admrul 262건 저장되어 있으므로 대조 가능.
    """
    case_nos = set(
        re.sub(r"\s+", "", m) for m in CASE_NO_RE.findall(text)
    )
    ordinance_nos = set(
        re.sub(r"\s+", "", m) for m in ORDINANCE_NO_RE.findall(text)
    )

    # 사건번호는 DB 대조 안 함 (evidence 기반 검증은 verify.py 가 담당)
    verified_cases = 0  # deprecated

    verified_ordinances = 0
    for on in ordinance_nos:
        # 양방향 매칭: "제1726호" / "1726" / "제1726" / "1726호"
        variants = {on, on.lstrip("제").rstrip("호"), on.lstrip("제"), on.rstrip("호")}
        placeholders = ",".join("?" for _ in variants)
        row = conn.execute(
            f"SELECT 1 FROM law_documents "
            f"WHERE promulgation_no IS NOT NULL "
            f"AND REPLACE(promulgation_no, ' ', '') IN ({placeholders}) LIMIT 1",
            tuple(variants),
        ).fetchone()
        if row:
            verified_ordinances += 1

    return {
        "case_no_unique": len(case_nos),
        "case_no_verified": verified_cases,
        "ordinance_no_unique": len(ordinance_nos),
        "ordinance_no_verified": verified_ordinances,
    }


def check_promulgation_no_column(conn: sqlite3.Connection) -> bool:
    cols = conn.execute("PRAGMA table_info(law_documents)").fetchall()
    return any(c[1] == "promulgation_no" for c in cols)


def main() -> None:
    if len(sys.argv) > 1:
        run_id = sys.argv[1]
    else:
        latest = EVAL_RUNS / "latest.txt"
        if not latest.exists():
            print("no eval-runs/latest.txt", file=sys.stderr)
            sys.exit(1)
        run_id = latest.read_text().strip()

    run_dir = EVAL_RUNS / run_id
    cases_dir = run_dir / "cases"
    if not cases_dir.exists():
        print(f"no cases dir: {cases_dir}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    has_promulgation = check_promulgation_no_column(conn)
    print(f"# structural_metrics — run {run_id}")
    print(f"# law_documents.promulgation_no column: {'EXISTS' if has_promulgation else 'MISSING'}")
    print()

    totals = Counter()
    forbidden_totals = Counter()
    conditional_totals = Counter()
    case_totals = 0
    verified_case_totals = 0
    ord_totals = 0
    verified_ord_totals = 0
    n_cases = 0
    n_with_case = 0
    n_with_verified_case = 0
    n_with_ord = 0
    n_with_verified_ord = 0

    for case_file in sorted(cases_dir.glob("*.json")):
        if case_file.name.endswith(".scored.json"):
            continue
        try:
            d = json.loads(case_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"skip {case_file.name}: {e}")
            continue
        if "error" in d:
            continue
        n_cases += 1
        sr = d.get("system_result") or {}
        text = collect_text(sr)

        pat = count_patterns(text)
        totals["case_no_pattern"] += pat["case_no_pattern"]
        totals["supreme_court_citation"] += pat["supreme_court_citation"]
        totals["ordinance_number_pattern"] += pat["ordinance_number_pattern"]
        totals["article_pattern"] += pat["article_pattern"]
        totals["voluntary_mentions"] += pat["voluntary_mentions"]
        for k, v in pat["forbidden_phrases"].items():
            forbidden_totals[k] += v
        for k, v in pat["conditional_phrases"].items():
            conditional_totals[k] += v

        if has_promulgation:
            vd = verify_against_db(text, conn)
        else:
            vd = {
                "case_no_unique": len(set(re.sub(r"\s+", "", m) for m in CASE_NO_RE.findall(text))),
                "case_no_verified": 0,
                "ordinance_no_unique": len(set(re.sub(r"\s+", "", m) for m in ORDINANCE_NO_RE.findall(text))),
                "ordinance_no_verified": 0,
            }
            # promulgation_no 컬럼이 없어도 case_no는 검증 가능
            case_nos = set(re.sub(r"\s+", "", m) for m in CASE_NO_RE.findall(text))
            verified_cases = 0
            for cn in case_nos:
                row = conn.execute(
                    "SELECT 1 FROM precedent_doc_cache WHERE REPLACE(case_no, ' ', '') = ? LIMIT 1",
                    (cn,),
                ).fetchone()
                if row:
                    verified_cases += 1
            vd["case_no_verified"] = verified_cases

        case_totals += vd["case_no_unique"]
        verified_case_totals += vd["case_no_verified"]
        ord_totals += vd["ordinance_no_unique"]
        verified_ord_totals += vd["ordinance_no_verified"]
        if vd["case_no_unique"] > 0:
            n_with_case += 1
        if vd["case_no_verified"] > 0:
            n_with_verified_case += 1
        if vd["ordinance_no_unique"] > 0:
            n_with_ord += 1
        if vd["ordinance_no_verified"] > 0:
            n_with_verified_ord += 1

    def pct(n, d):
        return f"{100*n/d:.1f}%" if d else "n/a"

    print(f"evaluated cases: {n_cases}")
    print()
    print("== Citation Patterns (raw, regex만) ==")
    print(f"사건번호 패턴 합계: {totals['case_no_pattern']}")
    print(f"  대법원 선고형 인용: {totals['supreme_court_citation']}")
    print(f"고시번호 패턴 합계: {totals['ordinance_number_pattern']}")
    print(f"조문 인용 패턴 합계: {totals['article_pattern']}")
    print(f"자율규약/표준약관 언급: {totals['voluntary_mentions']}")
    print()
    print("== DB Verified ==")
    print(f"사건번호 unique: {case_totals}  /  verified: {verified_case_totals} ({pct(verified_case_totals, case_totals)})")
    print(f"고시번호 unique: {ord_totals}  /  verified: {verified_ord_totals} ({pct(verified_ord_totals, ord_totals)})")
    print()
    print("== Case Coverage ==")
    print(f"사건번호 포함 케이스: {n_with_case}/{n_cases} ({pct(n_with_case, n_cases)})")
    print(f"verified 사건번호 포함 케이스: {n_with_verified_case}/{n_cases} ({pct(n_with_verified_case, n_cases)})")
    print(f"고시번호 포함 케이스: {n_with_ord}/{n_cases} ({pct(n_with_ord, n_cases)})")
    print(f"verified 고시번호 포함 케이스: {n_with_verified_ord}/{n_cases} ({pct(n_with_verified_ord, n_cases)})")
    print()
    print("== Forbidden Phrases (0이 목표) ==")
    if forbidden_totals:
        for p, c in forbidden_totals.most_common():
            print(f"  {p}: {c}")
    else:
        print("  (none)")
    print()
    print("== Conditional / Legal-Tone Phrases ==")
    for p in CONDITIONAL_PHRASES:
        c = conditional_totals.get(p, 0)
        print(f"  {p}: {c}")

    # CSV/JSON 저장
    out = {
        "run_id": run_id,
        "has_promulgation_no_column": has_promulgation,
        "n_cases": n_cases,
        "totals": dict(totals),
        "case_no_unique": case_totals,
        "case_no_verified": verified_case_totals,
        "ordinance_no_unique": ord_totals,
        "ordinance_no_verified": verified_ord_totals,
        "cases_with_case_no": n_with_case,
        "cases_with_verified_case_no": n_with_verified_case,
        "cases_with_ordinance_no": n_with_ord,
        "cases_with_verified_ordinance_no": n_with_verified_ord,
        "forbidden_phrases": dict(forbidden_totals),
        "conditional_phrases": dict(conditional_totals),
    }
    out_path = run_dir / "structural_metrics.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
