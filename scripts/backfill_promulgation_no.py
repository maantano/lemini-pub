"""admrul/statute 마크다운 front-matter의 '공포번호'를 law_documents.promulgation_no로 백필.

마이그레이션 0003 적용 후 1회 실행.
기존 DB의 law_id 기준으로 매칭한다 (MD front-matter 법령ID 사용).

실행:
  python scripts/backfill_promulgation_no.py [--dry-run]
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "artifacts" / "laws.sqlite"
SYNC_ROOTS = [
    ROOT / "data" / "sync" / "kr" / "행정규칙",
    ROOT / "data" / "sync" / "kr" / "법령",
]

# YAML front-matter 파싱용 (구조만)
FM_START = re.compile(r"^---\s*$")
FM_LAW_ID_RE = re.compile(r"^법령ID:\s*[\"']?([^\"'\n]+?)[\"']?\s*$")
FM_PROMULGATION_RE = re.compile(r"^공포번호:\s*[\"']?([^\"'\n]+?)[\"']?\s*$")
FM_ISSUER_RE = re.compile(r"^issuer:\s*(.+)\s*$")


def parse_front_matter(md_path: Path) -> dict | None:
    """MD 첫 front-matter에서 법령ID·공포번호·issuer 추출."""
    try:
        with md_path.open("r", encoding="utf-8") as f:
            first = f.readline()
            if not FM_START.match(first.rstrip()):
                return None
            lines = []
            for ln in f:
                if FM_START.match(ln.rstrip()):
                    break
                lines.append(ln)
    except Exception:
        return None

    law_id = None
    promulgation_no = None
    issuer = None
    for ln in lines:
        m = FM_LAW_ID_RE.match(ln.rstrip())
        if m:
            law_id = m.group(1).strip()
            continue
        m = FM_PROMULGATION_RE.match(ln.rstrip())
        if m:
            promulgation_no = m.group(1).strip()
            continue
        m = FM_ISSUER_RE.match(ln.rstrip())
        if m:
            issuer = m.group(1).strip().strip("'\"")

    if not law_id:
        return None
    return {"law_id": law_id, "promulgation_no": promulgation_no, "issuer": issuer}


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # promulgation_no 컬럼 존재 확인
    cols = [r[1] for r in conn.execute("PRAGMA table_info(law_documents)").fetchall()]
    if "promulgation_no" not in cols:
        print("ERROR: law_documents.promulgation_no 컬럼 없음. 마이그레이션 0003을 먼저 적용하세요.", file=sys.stderr)
        sys.exit(1)

    md_files: list[Path] = []
    for root in SYNC_ROOTS:
        if root.exists():
            md_files.extend(root.rglob("*.md"))
    print(f"# 대상 MD: {len(md_files)}개")

    updated = 0
    skipped_no_fm = 0
    skipped_no_law_id = 0
    skipped_no_promulgation = 0
    skipped_not_in_db = 0
    for md in md_files:
        fm = parse_front_matter(md)
        if not fm:
            skipped_no_fm += 1
            continue
        if not fm.get("promulgation_no"):
            skipped_no_promulgation += 1
            continue
        law_id = fm["law_id"]
        promulgation_no = fm["promulgation_no"]
        # "제X호" 포맷으로 정규화 (없는 경우만)
        if not promulgation_no.startswith("제") and "호" not in promulgation_no:
            # 단순 숫자 "15" 같은 경우 — 별도 포맷 강제는 하지 않음, 원본 유지
            pass

        row = conn.execute(
            "SELECT id, promulgation_no FROM law_documents WHERE law_id = ?",
            (law_id,),
        ).fetchone()
        if not row:
            skipped_not_in_db += 1
            continue
        if row["promulgation_no"] == promulgation_no:
            continue  # 이미 동일
        if not dry_run:
            conn.execute(
                "UPDATE law_documents SET promulgation_no = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (promulgation_no, row["id"]),
            )
        updated += 1

    if not dry_run:
        conn.commit()

    print(f"updated: {updated}")
    print(f"skipped (FM 없음): {skipped_no_fm}")
    print(f"skipped (law_id 없음): {skipped_no_law_id}")
    print(f"skipped (공포번호 없음): {skipped_no_promulgation}")
    print(f"skipped (DB에 없음): {skipped_not_in_db}")

    n_with = conn.execute(
        "SELECT COUNT(*) FROM law_documents WHERE promulgation_no IS NOT NULL AND promulgation_no != ''"
    ).fetchone()[0]
    print(f"\n최종 DB 상태: promulgation_no 채워진 문서 {n_with}개")


if __name__ == "__main__":
    main()
