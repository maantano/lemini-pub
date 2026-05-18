"""수동 업로드된 자율규약 파일 자동 처리.

사용자가 data/voluntary-raw/manual/{issuer_slug}/*.{pdf,hwp,hwpx} 에 파일 넣으면:
  1. HWP/HWPX → PDF 변환 (LibreOffice)
  2. PDF → 마크다운 (pdfplumber + 조문 segmentation)
  3. staging DB 에 voluntary_code 로 append (issuer 자동 매핑)
  4. 임베딩 생성

사용:
  ARTIFACT_DIR=$(pwd)/data/staging-artifacts \\
  PYTHONPATH=packages/python/src \\
  .venv/bin/python scripts/process_manual_uploads.py
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANUAL_ROOT = REPO_ROOT / "data/voluntary-raw/manual"
MD_ROOT = REPO_ROOT / "data/sync/kr/자율규약"

# issuer_slug → 실제 기관명 매핑 (사용자 디렉토리명)
ISSUER_MAP = {
    # 금융
    "kofia": ("한국금융투자협회", "association_copyright", "quote_only"),
    "fss": ("금융감독원", "kogl_type1", "full"),
    "kfb": ("전국은행연합회", "association_copyright", "quote_only"),
    "klia": ("생명보험협회", "association_copyright", "quote_only"),
    "knia": ("손해보험협회", "association_copyright", "quote_only"),
    "crefia": ("여신금융협회", "association_copyright", "quote_only"),
    "fsb": ("저축은행중앙회", "association_copyright", "quote_only"),
    "cu": ("신협중앙회", "association_copyright", "quote_only"),
    "kfcc": ("새마을금고중앙회", "association_copyright", "quote_only"),
    "krx": ("한국거래소", "association_copyright", "full"),

    # 의료·제약
    "kmdia": ("한국의료기기산업협회", "association_copyright", "quote_only"),
    "kpbma": ("한국제약바이오협회", "association_copyright", "quote_only"),
    "kaoma": ("한국한의학회", "association_copyright", "quote_only"),
    "kma": ("대한의사협회", "association_copyright", "quote_only"),
    "kda": ("대한치과의사협회", "association_copyright", "quote_only"),
    "kpanet": ("대한약사회", "association_copyright", "quote_only"),
    "koreanurse": ("대한간호협회", "association_copyright", "quote_only"),

    # IT·개인정보
    "kisa": ("한국인터넷진흥원", "kogl_type1", "full"),
    "pipc": ("개인정보보호위원회", "kogl_type1", "full"),
    "kocsc": ("방송통신심의위원회", "kogl_type1", "full"),
    "kcc": ("방송통신위원회", "kogl_type1", "full"),
    "ktoa": ("한국통신사업자연합회", "association_copyright", "quote_only"),
    "kinter": ("한국인터넷기업협회", "association_copyright", "quote_only"),
    "kgames": ("한국게임산업협회", "association_copyright", "quote_only"),

    # 전문직
    "koreanbar": ("대한변호사협회", "association_copyright", "quote_only"),
    "kabl": ("대한법무사회", "association_copyright", "quote_only"),
    "kicpa": ("한국공인회계사회", "association_copyright", "quote_only"),
    "kacpta": ("한국세무사회", "association_copyright", "quote_only"),
    "kpaa": ("대한변리사회", "association_copyright", "quote_only"),

    # 유통·광고
    "karb": ("한국광고자율심의기구", "association_copyright", "quote_only"),
    "kobaco": ("한국방송광고진흥공사", "kogl_type1", "full"),
    "korcham": ("대한상공회의소", "association_copyright", "quote_only"),
    "ikfa": ("한국프랜차이즈산업협회", "association_copyright", "quote_only"),

    # 건설·부동산
    "cak": ("대한건설협회", "association_copyright", "quote_only"),
    "khanet": ("한국주택협회", "association_copyright", "quote_only"),
    "kreaa": ("한국부동산협회", "association_copyright", "quote_only"),

    # 식품·자동차
    "kfia": ("한국식품산업협회", "association_copyright", "quote_only"),
    "kalia": ("한국주류산업협회", "association_copyright", "quote_only"),
    "kama": ("한국자동차산업협회", "association_copyright", "quote_only"),
    "kamma": ("한국자동차모빌리티산업협회", "association_copyright", "quote_only"),
}


def _convert_hwp_to_pdf(hwp: Path, out_dir: Path, timeout: int = 120) -> Path | None:
    """soffice --headless 로 HWP/HWPX → PDF."""
    out_dir.mkdir(parents=True, exist_ok=True)
    expected = out_dir / (hwp.stem + ".pdf")
    if expected.exists() and expected.stat().st_size > 1000:
        return expected
    # 기존 soffice 프로세스 종료 (안정성)
    subprocess.run(["pkill", "-f", "soffice"], capture_output=True)
    time.sleep(1)
    try:
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf",
             "--outdir", str(out_dir), str(hwp)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    return expected if expected.exists() and expected.stat().st_size > 1000 else None


def _pdf_to_text(pdf: Path, timeout: int = 60) -> str:
    """pdfplumber + 타임아웃."""
    import pdfplumber

    def _h(s, f): raise TimeoutError("pdfplumber timeout")
    signal.signal(signal.SIGALRM, _h)
    signal.alarm(timeout)
    try:
        with pdfplumber.open(str(pdf)) as p:
            return "\n".join((page.extract_text() or "") for page in p.pages)
    finally:
        signal.alarm(0)


def process_one(issuer_slug: str, file_path: Path) -> tuple[bool, str]:
    """한 파일 처리: (HWP→PDF →) PDF→MD."""
    if issuer_slug not in ISSUER_MAP:
        return False, f"unknown issuer: {issuer_slug}"
    issuer_name, license_policy, citation_mode = ISSUER_MAP[issuer_slug]

    ext = file_path.suffix.lower()

    # 1. PDF 확보
    if ext in (".hwp", ".hwpx"):
        pdf_dir = file_path.parent / "_converted_pdf"
        pdf = _convert_hwp_to_pdf(file_path, pdf_dir)
        if pdf is None:
            return False, "HWP 변환 실패"
    elif ext == ".pdf":
        pdf = file_path
    else:
        return False, f"지원 안 하는 확장자: {ext}"

    # 2. 텍스트 추출
    try:
        text = _pdf_to_text(pdf)
    except TimeoutError:
        return False, "PDF 추출 timeout"
    if len(text) < 200:
        return False, f"본문 너무 짧음 ({len(text)} chars)"

    # 3. 마크다운 생성
    sys.path.insert(0, str(REPO_ROOT / "packages/python/src"))
    import yaml
    import re
    import hashlib
    from worker.sync.admrul.converter import _segment_body
    from worker.sync.laws.converter import _LawDumper, _QuotedStr, normalize_law_name

    title = file_path.stem.replace("_", " ").strip()
    normalized = normalize_law_name(title)
    law_id = f"VC-{issuer_slug.upper()}-{hashlib.sha256(f'{issuer_slug}:{title}'.encode()).hexdigest()[:12]}"

    fm = {
        "제목": normalized,
        "법령MST": law_id,
        "법령ID": _QuotedStr(law_id),
        "법령구분": "자율규약",
        "법령구분코드": "VC",
        "소관부처": [issuer_name],
        "공포일자": "",
        "공포번호": _QuotedStr(""),
        "시행일자": "",
        "법령분야": "",
        "상태": "시행",
        "출처": f"manual_upload:{issuer_slug}/{file_path.name}",
        "document_type": "voluntary_code",
        "license_policy": license_policy,
        "citation_mode": citation_mode,
        "issuer": issuer_name,
    }
    yaml_str = yaml.dump(fm, Dumper=_LawDumper, allow_unicode=True,
                         default_flow_style=False, sort_keys=False)

    # 노이즈 제거 + 조문 segmentation
    text = re.sub(r"\n\s*-\s*\d+\s*-\s*\n", "\n", text)
    text = re.sub(r"\f", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    body = _segment_body(text)

    md_content = f"---\n{yaml_str}---\n\n# {normalized}\n\n{body}\n"

    # 저장 경로: data/sync/kr/자율규약/manual-{issuer_name}/{safe_filename}.md
    out_dir = MD_ROOT / f"manual-{issuer_name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w가-힣\-]", "", normalized)[:80] or "unnamed"
    out_path = out_dir / f"{safe}.md"
    out_path.write_text(md_content, encoding="utf-8")

    return True, f"→ {out_path.relative_to(REPO_ROOT)}"


def main() -> int:
    if not MANUAL_ROOT.exists():
        print(f"업로드 디렉토리 없음: {MANUAL_ROOT}")
        print("다음 명령으로 생성:")
        print(f"  mkdir -p {MANUAL_ROOT}")
        for slug in sorted(ISSUER_MAP.keys()):
            print(f"  mkdir -p {MANUAL_ROOT}/{slug}")
        return 1

    total_ok = total_fail = 0
    skipped_dirs = []

    for issuer_dir in sorted(MANUAL_ROOT.iterdir()):
        if not issuer_dir.is_dir() or issuer_dir.name.startswith("."):
            continue
        slug = issuer_dir.name
        if slug not in ISSUER_MAP:
            skipped_dirs.append(slug)
            continue

        files = sorted([f for f in issuer_dir.iterdir()
                        if f.is_file() and f.suffix.lower() in (".pdf", ".hwp", ".hwpx")])
        if not files:
            continue

        print(f"\n=== {slug} ({ISSUER_MAP[slug][0]}) — {len(files)}개 파일 ===")
        for f in files:
            ok, msg = process_one(slug, f)
            status = "✓" if ok else "✗"
            print(f"  {status} {f.name[:60]}  {msg}")
            if ok:
                total_ok += 1
            else:
                total_fail += 1

    print()
    print(f"=== 완료: 성공 {total_ok}, 실패 {total_fail} ===")
    if skipped_dirs:
        print(f"알 수 없는 디렉토리 (ISSUER_MAP 에 추가 필요): {skipped_dirs}")

    if total_ok > 0:
        print()
        print("다음 단계 — staging DB 에 ingest:")
        print(f"  ARTIFACT_DIR=$(pwd)/data/staging-artifacts \\")
        print(f"  PYTHONPATH=packages/python/src \\")
        print(f"  .venv/bin/python scripts/append_admrul_bulk.py data/sync/kr/자율규약")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
