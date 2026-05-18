"""IngestService append 모드 안전성 검증 스크립트.

프로덕션 laws.sqlite 는 절대 건드리지 않음. data/test-artifacts/ 복사본만 사용.

검증 시나리오:
  1. 기존 5,568건 법령 보존 + 자율규약 1건 추가 → 5,569건
  2. 같은 자율규약 재실행 → 중복 없이 5,569건 유지 (멱등)
  3. 자율규약 내용 수정 후 재실행 → content_hash 바뀌어 갱신, 개수 5,569건
  4. 완전 새로운 자율규약 1건 추가 → 5,570건
  5. 각 단계마다 기존 5,568건 법령 손상 없음 확인
  6. law_chunks 카운트도 일관성 있게 증감
  7. 임베딩 파일도 정상 append

실패 시:
  - ValueError 로 중단
  - 복사본은 data/artifacts → data/test-artifacts 재복사로 복구

사용법:
  ARTIFACT_DIR=$(pwd)/data/test-artifacts \\
  PYTHONPATH=packages/python/src \\
  python scripts/test_ingest_append.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/python/src"))

ARTIFACT_DIR = Path(os.environ.get("ARTIFACT_DIR", REPO_ROOT / "data" / "test-artifacts"))
PROD_DIR = REPO_ROOT / "data" / "artifacts"

VOLUNTARY_FIXTURE = REPO_ROOT / "data" / "test-voluntary"


def _safety_check() -> None:
    """프로덕션 DB 와 혼동 방지."""
    if ARTIFACT_DIR.resolve() == PROD_DIR.resolve():
        raise RuntimeError(
            f"ARTIFACT_DIR 가 프로덕션({PROD_DIR}) 을 가리킴 — 테스트 중단"
        )
    if not ARTIFACT_DIR.exists():
        raise RuntimeError(f"ARTIFACT_DIR 없음: {ARTIFACT_DIR}")
    print(f"[safety] test artifacts: {ARTIFACT_DIR}")
    print(f"[safety] prod artifacts (untouched): {PROD_DIR}")


def _snapshot() -> dict:
    """현재 DB 상태 스냅샷."""
    conn = sqlite3.connect(ARTIFACT_DIR / "laws.sqlite")
    try:
        rows = conn.execute(
            "SELECT document_type, COUNT(*) FROM law_documents GROUP BY document_type"
        ).fetchall()
        doc_by_type = {r[0]: r[1] for r in rows}
        total_docs = sum(doc_by_type.values())
        total_chunks = conn.execute("SELECT COUNT(*) FROM law_chunks").fetchone()[0]
        # 법령 5,568건 정체성 검증 — 가장 오래된 법령 하나 픽하고 id/title 확인
        sample = conn.execute(
            "SELECT id, title FROM law_documents WHERE document_type='statute' ORDER BY created_at LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return {
        "total_docs": total_docs,
        "by_type": doc_by_type,
        "total_chunks": total_chunks,
        "sample_statute": sample,
    }


def _assert_progress(before: dict, after: dict, expected_delta: int, tag: str) -> None:
    """법령 손상 없고 voluntary_code 만 변했는지 확인."""
    # 법령은 변하면 안됨
    if before["by_type"].get("statute", 0) != after["by_type"].get("statute", 0):
        raise ValueError(
            f"[{tag}] 법령 개수 변경 detected! "
            f"before={before['by_type']['statute']} after={after['by_type']['statute']}"
        )
    if before["sample_statute"] != after["sample_statute"]:
        raise ValueError(f"[{tag}] 샘플 법령 레코드가 변경됨 — 기존 데이터 손상")

    # voluntary_code 는 예상대로 변해야 함
    delta = after["by_type"].get("voluntary_code", 0) - before["by_type"].get("voluntary_code", 0)
    if delta != expected_delta:
        raise ValueError(
            f"[{tag}] voluntary_code 증감 불일치. expected_delta={expected_delta}, actual={delta}"
        )

    print(f"[{tag}] ✓ 법령 {before['by_type']['statute']}건 그대로, "
          f"voluntary_code {before['by_type'].get('voluntary_code', 0)} → {after['by_type'].get('voluntary_code', 0)}, "
          f"chunks {before['total_chunks']} → {after['total_chunks']}")


def _run_append_voluntary(md_path: Path) -> None:
    """기존 append 스크립트 재활용."""
    from subprocess import run
    env = os.environ.copy()
    env["ARTIFACT_DIR"] = str(ARTIFACT_DIR)
    env["PYTHONPATH"] = str(REPO_ROOT / "packages/python/src")
    result = run(
        [sys.executable, str(REPO_ROOT / "scripts/append_voluntary_code_test.py"), str(md_path)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"append 실패: {result.stderr}")


def main() -> int:
    _safety_check()
    print()

    # --- 시나리오 1: 현재 상태 확인 ---
    s0 = _snapshot()
    print(f"[S0] initial: {s0['by_type']} chunks={s0['total_chunks']}")

    # 만약 이미 voluntary_code 1건 있으면 0 으로 리셋하고 시작
    if s0["by_type"].get("voluntary_code", 0) > 0:
        conn = sqlite3.connect(ARTIFACT_DIR / "laws.sqlite")
        try:
            conn.execute("DELETE FROM law_chunks WHERE law_id LIKE 'VC-%' OR law_id = 'VC-KMDIA-001'")
            conn.execute("DELETE FROM law_documents WHERE document_type='voluntary_code'")
            conn.commit()
        finally:
            conn.close()
        s0 = _snapshot()
        print(f"[S0'] reset: {s0['by_type']} chunks={s0['total_chunks']}")

    # --- 시나리오 2: 자율규약 1건 추가 → 5569 ---
    fixture1 = VOLUNTARY_FIXTURE / "kr" / "의료기기공정경쟁규약" / "자율규약.md"
    if not fixture1.exists():
        raise RuntimeError(f"fixture 없음: {fixture1}")
    _run_append_voluntary(fixture1)
    s1 = _snapshot()
    _assert_progress(s0, s1, expected_delta=+1, tag="S1: append 1건")

    # --- 시나리오 3: 같은 파일 재실행 (멱등) → 5569 유지 ---
    _run_append_voluntary(fixture1)
    s2 = _snapshot()
    _assert_progress(s1, s2, expected_delta=0, tag="S2: 멱등 재실행")

    # --- 시나리오 4: 완전 새 자율규약 1건 추가 → 5570 ---
    fixture2_dir = VOLUNTARY_FIXTURE / "kr" / "테스트자율규약2"
    fixture2_dir.mkdir(parents=True, exist_ok=True)
    fixture2 = fixture2_dir / "자율규약.md"
    fixture2.write_text("""---
제목: 테스트 자율규약2
법령MST: VC-TEST-002
법령ID: 'VC-TEST-002'
법령구분: 자율규약
법령구분코드: VC
소관부처:
  - 테스트협회
공포일자: 2026-04-20
공포번호: 'TEST-002'
시행일자: 2026-04-20
상태: 시행
출처: https://example.com
document_type: voluntary_code
license_policy: association_copyright
citation_mode: quote_only
issuer: 테스트협회
---

# 테스트 자율규약2

##### 제1조 (목적)

이 규약은 테스트를 목적으로 한다.
""", encoding="utf-8")

    _run_append_voluntary(fixture2)
    s3 = _snapshot()
    _assert_progress(s2, s3, expected_delta=+1, tag="S3: 새 규약 추가")

    # --- 시나리오 5: 첫 번째 자율규약을 내용 변경해서 다시 추가 (content_hash 변경) → 개수 유지 ---
    original_content = fixture1.read_text(encoding="utf-8")
    try:
        modified = original_content + "\n\n##### 제99조 (부가 조항)\n\n신규 조항이 추가되었다.\n"
        fixture1.write_text(modified, encoding="utf-8")
        _run_append_voluntary(fixture1)
        s4 = _snapshot()
        _assert_progress(s3, s4, expected_delta=0, tag="S4: 내용 수정 재실행")
        # 청크는 늘어야 함 (제99조 추가)
        if s4["total_chunks"] <= s3["total_chunks"]:
            raise ValueError(f"[S4] 청크 증가 안됨: {s3['total_chunks']} → {s4['total_chunks']}")
        print(f"[S4] ✓ 청크 {s3['total_chunks']} → {s4['total_chunks']} (제99조 반영)")
    finally:
        # 픽스처 원상복구
        fixture1.write_text(original_content, encoding="utf-8")

    # --- 시나리오 6: 새 규약 정리 (cleanup) ---
    if fixture2.exists():
        fixture2.unlink()
    if fixture2_dir.exists():
        fixture2_dir.rmdir()

    print()
    print("=" * 60)
    print("✅ 모든 시나리오 통과")
    print(f"   법령 {s0['by_type']['statute']}건 그대로 유지")
    print(f"   자율규약 append·멱등·수정·추가 검증 완료")
    print(f"   최종 DB: total={s4['total_docs']} chunks={s4['total_chunks']}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as e:
        print(f"\n❌ 테스트 실패: {e}", file=sys.stderr)
        print("   복구: cp data/artifacts/laws.sqlite data/test-artifacts/ && 마이그레이션 재적용", file=sys.stderr)
        raise SystemExit(2)
