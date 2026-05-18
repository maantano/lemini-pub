"""staging DB 의 voluntary_code title 을 manifest.json 기준으로 재정리.

제목을 파일명 기반 → 진짜 게시물 제목으로 교체.
임베딩은 본문 기반이므로 재생성 불필요.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def clean_ftc_title(raw: str) -> str:
    """'97 [제10021호]국외여행 표준약관 (2026. 1. 28. 개정) 소비자거래정책과 ...' → '[제10021호]국외여행 표준약관'"""
    # 앞 번호 제거
    s = re.sub(r"^\d+\s+", "", raw)
    # "소관부서명", "파일다운로드 문서뷰어", 날짜·숫자 꼬리 제거
    for marker in [" 소비자거래정책과", " 약관제도과", " 약관심사과", " 소비자거래과",
                   " 파일다운로드", " 문서뷰어"]:
        idx = s.find(marker)
        if idx > 0:
            s = s[:idx]
    # HTML entity
    s = s.replace("&#039;", "'").replace("&amp;", "&")
    return s.strip()


def main() -> int:
    manifest_path = REPO_ROOT / "data/voluntary-raw/ftc/manifest.json"
    if not manifest_path.exists():
        print("manifest.json 없음")
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # ntt_sn → 정리된 title
    ntt_to_title = {m["ntt_sn"]: clean_ftc_title(m["title"]) for m in manifest}

    artifact_dir = REPO_ROOT / "data/staging-artifacts"
    db = artifact_dir / "laws.sqlite"
    conn = sqlite3.connect(db)

    # staging DB 의 voluntary_code 중 law_id 가 VC-FTC-* 이거나 title 이 nttSn 으로 시작하는 것 찾아서 교체
    # 우리 voluntary_pdf_to_md.py 는 law_id = VC-FTC-<hash> 로 생성했으므로 source_url 에 파일명 있음
    rows = conn.execute(
        "SELECT id, title, law_id, source_url FROM law_documents WHERE document_type='voluntary_code' AND issuer='공정거래위원회'"
    ).fetchall()

    print(f"대상: {len(rows)}개 공정위 표준약관")
    updated = 0

    for doc_id, old_title, law_id, source_url in rows:
        # old_title 이 '11098 20161031아파트임대차' 같은 형식 → 파일명의 nttSn 접두 추출
        m = re.match(r"^(\d+)\s+", old_title)
        if not m:
            continue
        ntt_sn = m.group(1)
        new_title = ntt_to_title.get(ntt_sn)
        if not new_title or new_title == old_title:
            continue

        # title, title_normalized 모두 업데이트
        from law_rag_core.normalization import normalize_for_search  # noqa: E402
        import sys as _sys
        _sys.path.insert(0, str(REPO_ROOT / "packages/python/src"))
        new_norm = re.sub(r"\s+", "", new_title.lower())

        conn.execute(
            "UPDATE law_documents SET title=?, title_normalized=? WHERE id=?",
            (new_title, new_norm, doc_id),
        )
        # law_search_fts 도 업데이트
        try:
            conn.execute(
                "UPDATE law_search_fts SET law_title=? WHERE law_id=?",
                (new_title, law_id),
            )
        except sqlite3.OperationalError:
            pass
        updated += 1
        if updated <= 5:
            print(f"  {old_title[:50]} → {new_title[:50]}")

    conn.commit()
    conn.close()
    print(f"\n✓ 제목 {updated}건 정리 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
