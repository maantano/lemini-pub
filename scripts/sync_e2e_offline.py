"""sync → ingest 오프라인 E2E 검증 스크립트.

OC 키 없이 돌릴 수 있는 최소 E2E. 가상의 DRF detail 응답을 만들어서
worker.sync.laws.converter가 만든 마크다운이 law_rag_core.parser와 IngestService를
통과해 laws.sqlite까지 저장되는지 확인한다.

Usage:
    python scripts/sync_e2e_offline.py

실행 결과는 data/sync-test/ 아래에 생성되며 .gitignore로 제외된다.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/python/src"))

TEST_ROOT = REPO_ROOT / "data" / "sync-test"
os.environ["ARTIFACT_DIR"] = str(TEST_ROOT / "artifacts")
os.environ["WORKSPACE_ROOT"] = str(TEST_ROOT)
os.environ.setdefault("GEMINI_API_KEY", "")  # 임베딩 없이 검증


def main() -> int:
    from law_rag_core.ingest import IngestService
    from law_rag_core.parser import parse_law_markdown
    from worker.sync.laws import checkpoint, converter, failures

    # 1. 가상 DRF detail → 마크다운
    detail = {
        "metadata": {
            "법령명한글": "테스트법",
            "법령MST": "999999",
            "법령ID": "TEST001",
            "법령구분": "법률",
            "법령구분코드": "A",
            "소관부처명": "법무부, 과학기술정보통신부",
            "소관부처코드": "M01",
            "공포일자": "20260420",
            "공포번호": "12345",
            "시행일자": "20260501",
            "법령분야": "총칙",
        },
        "articles": [
            {
                "조문번호": "1",
                "조문제목": "목적",
                "조문내용": "제1조(목적) 이 법은 테스트를 목적으로 한다.",
                "항": [
                    {
                        "항번호": "①",
                        "항내용": "① 첫째 규정.",
                        "호": [{"호번호": "1", "호내용": "1. 첫 번째 호 내용", "목": []}],
                    }
                ],
            },
            {
                "조문번호": "2",
                "조문제목": "정의",
                "조문내용": "제2조(정의) 이 법에서 사용하는 용어의 뜻은 다음과 같다.",
                "항": [],
            },
        ],
        "addenda": [
            {
                "부칙공포일자": "20260420",
                "부칙공포번호": "12345",
                "부칙내용": "이 법은 공포일로부터 시행한다.",
            }
        ],
    }

    converter.reset_path_registry()
    md = converter.law_to_markdown(detail)

    kr_dir = TEST_ROOT / "kr" / "테스트법"
    kr_dir.mkdir(parents=True, exist_ok=True)
    md_path = kr_dir / "법률.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"[1/5] 마크다운 생성: {md_path}")

    # 2. parser 호환 확인
    parsed = parse_law_markdown(md_path, md)
    assert parsed.document.title == "테스트법", parsed.document.title
    assert parsed.document.law_id == "TEST001"
    assert parsed.document.law_mst == "999999"
    assert parsed.document.ministry == ["법무부", "과학기술정보통신부"], parsed.document.ministry
    assert len(parsed.chunks) >= 2
    print(
        f"[2/5] parser 호환: title={parsed.document.title!r} "
        f"ministry={parsed.document.ministry} chunks={len(parsed.chunks)}"
    )

    # 3. IngestService E2E (임베딩 skip)
    service = IngestService()
    job = service.ingest_path(TEST_ROOT / "kr", mode="minimal", apply_schema=True, reindex=False)
    stats = job.stats
    assert stats["documents"] == 1, stats
    print(f"[3/5] IngestService: documents={stats['documents']} chunks={stats['chunks']}")

    # 4. DB 검증
    import sqlite3

    conn = sqlite3.connect(TEST_ROOT / "artifacts" / "laws.sqlite")
    try:
        row = conn.execute(
            "SELECT title, law_id, law_mst, law_type, ministry, promulgation_date FROM law_documents"
        ).fetchone()
        assert row is not None and row[0] == "테스트법"
        print(f"[4/5] DB: {row}")

        # M0 신규 컬럼 존재 확인 (값은 아직 sync가 채우지 않음 — M5 스코프)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(law_documents)").fetchall()]
        for expected in ("source", "source_fetched_at", "document_type", "content_hash"):
            assert expected in cols, f"M0 컬럼 누락: {expected}"
    finally:
        conn.close()

    # 5. 체크포인트·failures
    checkpoint.mark_processed("999999")
    checkpoint.set_last_update("2026-04-20")
    assert "999999" in checkpoint.get_processed_msts()
    assert checkpoint.get_last_update() == "2026-04-20"

    failures.mark_failed("111111", reason="empty_body", detail="fixture", law_name="FixtureLaw")
    assert "111111" in failures.get_failed_msts()
    print("[5/5] checkpoint + failures 영속화 OK")

    print()
    print("✓ sync → parser → IngestService → DB E2E 모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
