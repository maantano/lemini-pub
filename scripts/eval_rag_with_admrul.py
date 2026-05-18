"""admrul 적재 후 RAG 응답 품질 검증.

질문 유형:
  1. 법령만 필요 (admrul 불려나오면 안 됨)
  2. 행정규칙 필요 (admrul 상위 인용되어야)
  3. 자율규약 필요 (voluntary_code 인용되어야)
  4. 복합 (법령 + 규칙 섞여 인용)

출력: 각 질문에 대해 인용된 문서의 document_type 분포 + 요약.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

os.environ.setdefault("ARTIFACT_DIR", str(Path(__file__).resolve().parents[1] / "data/test-artifacts"))

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/python/src"))

from law_rag_core.chat.service import ChatService
from law_rag_core.types import ChatRequest


QUESTIONS = [
    ("L1", "전세 계약 연장을 임대인이 거절할 수 있는 조건은?", "법령"),
    ("L2", "음주운전 처벌 기준이 뭔가요?", "법령"),
    ("A1", "공정거래위원회 가맹분야 불공정거래 심사 기준은?", "행정규칙"),
    ("A2", "식약처 의료기기 품목허가 시 제출 자료 요건?", "행정규칙"),
    ("A3", "개인정보보호위원회 개인정보 안전성 확보 기준 고시는?", "행정규칙"),
    ("A4", "국토교통부 건설공사 표준시공 관련 고시 내용?", "행정규칙"),
    ("V1", "의료기기 영업사원이 의사에게 식사 제공 한도는?", "자율규약"),
    ("M1", "의료기기 품목허가와 공정경쟁규약 사이 리베이트 판단 기준?", "복합"),
]


def doc_type_of_law_id(conn: sqlite3.Connection, law_id: str) -> str:
    row = conn.execute(
        "SELECT document_type FROM law_documents WHERE law_id = ?", (law_id,)
    ).fetchone()
    return row[0] if row else "?"


def main() -> int:
    chat = ChatService()
    conn = sqlite3.connect(Path(os.environ["ARTIFACT_DIR"]) / "laws.sqlite")

    print(f"DB: {os.environ['ARTIFACT_DIR']}/laws.sqlite")
    print(f"총 문서: {conn.execute('SELECT COUNT(*) FROM law_documents').fetchone()[0]}")
    print()

    for qid, question, expected in QUESTIONS:
        print(f"[{qid}] ({expected}) {question}")
        try:
            ans = chat.answer(ChatRequest(question=question, save=False, stream=False))
        except Exception as e:
            print(f"    ❌ 오류: {e}")
            print()
            continue

        types = []
        for c in ans.citations[:8]:
            t = doc_type_of_law_id(conn, c.law_id)
            types.append(t)
        type_summary = ", ".join(f"{t}×{types.count(t)}" for t in sorted(set(types)))

        print(f"    grounded={ans.grounded}, 인용 {len(ans.citations)}개 [{type_summary}]")
        if ans.summary:
            print(f"    summary: {ans.summary[:120]}")
        # 상위 3개 인용
        for c in ans.citations[:3]:
            t = doc_type_of_law_id(conn, c.law_id)
            print(f"      • [{t}] {c.law_title} 제{c.article_no}조 {c.article_title or ''}")
        print()

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
