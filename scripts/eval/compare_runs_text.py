"""두 eval run을 나란히 비교하는 텍스트 리포트.

사용:
  python scripts/eval/compare_runs_text.py <old_run_id> <new_run_id> [--sample N] [--case <thread_ts>]

  --sample N    : 랜덤 샘플 N개만 상세 출력 (기본 5)
  --case <ts>   : 특정 thread_ts 케이스만 상세 출력

구조 지표 집계는 양쪽 run_dir/structural_metrics.json 기반.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_RUNS = ROOT / "eval-runs"


def load_case(run_dir: Path, ts_key: str) -> dict | None:
    p = run_dir / "cases" / f"{ts_key}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def fmt_basis(basis) -> str:
    """v1(문자열) / v3(구조화) 모두 지원."""
    if basis is None:
        return "(none)"
    if isinstance(basis, str):
        return basis[:300]
    if isinstance(basis, dict):
        parts = []
        for key in ("statutes", "cases", "ordinances", "voluntary_codes"):
            arr = basis.get(key) or []
            if not arr:
                continue
            items = []
            for item in arr[:3]:
                if key == "statutes":
                    items.append(f"{item.get('name','')} {item.get('article','')}")
                elif key == "cases":
                    items.append(f"{item.get('court','')} {item.get('date','')} {item.get('case_no','')}")
                elif key == "ordinances":
                    items.append(f"{item.get('issuer','')} {item.get('title','')} {item.get('number','')}")
                elif key == "voluntary_codes":
                    items.append(f"{item.get('issuer','')} {item.get('title','')} {item.get('article','')}")
            if items:
                parts.append(f"{key}: [{'; '.join(items)}]")
        return " | ".join(parts) if parts else "(empty)"
    return str(basis)[:300]


def collect_basis_stats(sr: dict) -> dict:
    """system_result에서 basis 출현 통계 집계."""
    counts = {"statutes": 0, "cases": 0, "ordinances": 0, "voluntary_codes": 0, "string_basis": 0, "null_basis": 0}
    obs = sr.get("observations") or sr.get("clause_reviews") or []
    for o in obs:
        for iss in (o.get("issues") or []):
            b = iss.get("basis")
            if b is None:
                counts["null_basis"] += 1
            elif isinstance(b, str):
                counts["string_basis"] += 1
            elif isinstance(b, dict):
                for k in ("statutes", "cases", "ordinances", "voluntary_codes"):
                    counts[k] += len(b.get(k) or [])
    return counts


def print_case_diff(ts_key: str, old: dict, new: dict) -> None:
    print("=" * 80)
    print(f"CASE: {ts_key}")
    print("=" * 80)

    osr = old.get("system_result") or {}
    nsr = new.get("system_result") or {}

    # elapsed
    print(f"elapsed: old={old.get('elapsed_sec')}s  new={new.get('elapsed_sec')}s")
    print()

    # summary
    print("[summary] OLD:")
    print(f"  {(osr.get('summary') or '')[:400]}")
    print("[summary] NEW:")
    print(f"  {(nsr.get('summary') or '')[:400]}")
    print()

    # overall_assessment
    print("[overall_assessment] OLD:")
    print(f"  {(osr.get('overall_assessment') or '')[:500]}")
    print("[overall_assessment] NEW:")
    print(f"  {(nsr.get('overall_assessment') or '')[:500]}")
    print()

    # basis 통계 비교
    print("[basis 통계]")
    os_stat = collect_basis_stats(osr)
    ns_stat = collect_basis_stats(nsr)
    print(f"  OLD: {os_stat}")
    print(f"  NEW: {ns_stat}")
    print()

    # 첫 3개 observation의 basis 샘플
    print("[observation 샘플 basis 3건]")
    for src_name, sr in [("OLD", osr), ("NEW", nsr)]:
        obs = sr.get("observations") or sr.get("clause_reviews") or []
        basis_samples = []
        for o in obs:
            for iss in (o.get("issues") or []):
                b = iss.get("basis")
                if b:
                    basis_samples.append((o.get("locator") or "?", fmt_basis(b)))
                    if len(basis_samples) >= 3:
                        break
            if len(basis_samples) >= 3:
                break
        print(f"  {src_name}:")
        for loc, bs in basis_samples:
            print(f"    @{loc}: {bs}")
        if not basis_samples:
            print(f"    (none)")
    print()

    # confidence (v3 전용)
    conf = nsr.get("confidence")
    if conf:
        print(f"[v3 confidence] grounded={conf.get('grounded')}  dropped_count={conf.get('dropped_count')}")
        w = nsr.get("warnings", {}).get("dropped_basis") if isinstance(nsr.get("warnings"), dict) else None
        if w:
            print(f"  dropped_basis:")
            for item in w[:5]:
                print(f"    - {item.get('kind')}: {item.get('reason')}")
    print()


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: compare_runs_text.py <old_run_id> <new_run_id> [--sample N] [--case <ts>]", file=sys.stderr)
        sys.exit(1)
    old_id, new_id = args[0], args[1]
    sample_n = 5
    target_case = None
    for i, a in enumerate(args[2:], start=2):
        if a == "--sample" and i + 1 < len(args):
            sample_n = int(args[i + 1])
        elif a == "--case" and i + 1 < len(args):
            target_case = args[i + 1].replace(".", "_")

    old_dir = EVAL_RUNS / old_id
    new_dir = EVAL_RUNS / new_id

    # 구조 지표 집계 요약
    print("#" * 80)
    print(f"# COMPARE: {old_id}  vs  {new_id}")
    print("#" * 80)
    for label, d in [("OLD", old_dir), ("NEW", new_dir)]:
        sm = d / "structural_metrics.json"
        if sm.exists():
            m = json.loads(sm.read_text())
            print(f"\n[{label} structural_metrics]")
            print(f"  cases: {m.get('n_cases')}")
            print(f"  case_no (verified/unique): {m.get('case_no_verified')}/{m.get('case_no_unique')}")
            print(f"  ordinance_no (verified/unique): {m.get('ordinance_no_verified')}/{m.get('ordinance_no_unique')}")
            print(f"  cases_with_verified_case_no: {m.get('cases_with_verified_case_no')}")
            print(f"  cases_with_verified_ordinance_no: {m.get('cases_with_verified_ordinance_no')}")
            forbidden = m.get("forbidden_phrases") or {}
            print(f"  forbidden total: {sum(forbidden.values())}")
        else:
            print(f"\n[{label}] structural_metrics.json 없음 (structural_metrics.py 먼저 실행)")

    # 개별 케이스 diff
    old_cases = sorted((old_dir / "cases").glob("*.json"))
    old_keys = [p.stem for p in old_cases if not p.stem.endswith(".scored")]
    new_cases_dir = new_dir / "cases"
    new_keys = set(p.stem for p in new_cases_dir.glob("*.json"))
    common = [k for k in old_keys if k in new_keys]

    if target_case:
        if target_case not in common:
            print(f"\ncase {target_case} 공통 부재 (old={target_case in old_keys}, new={target_case in new_keys})")
            return
        old = load_case(old_dir, target_case)
        new = load_case(new_dir, target_case)
        print_case_diff(target_case, old, new)
    else:
        random.seed(0)
        sample = random.sample(common, min(sample_n, len(common)))
        for ts_key in sample:
            old = load_case(old_dir, ts_key)
            new = load_case(new_dir, ts_key)
            if old and new and "system_result" in new:
                print_case_diff(ts_key, old, new)


if __name__ == "__main__":
    main()
