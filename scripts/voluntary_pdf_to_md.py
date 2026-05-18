"""자율규약 PDF → 마크다운 (law_rag_core parser 호환 포맷) 변환.

data/voluntary-raw/{source}/*.pdf → data/sync/kr/자율규약/{source}/{name}.md

front-matter:
  document_type='voluntary_code', license_policy='association_copyright',
  citation_mode='quote_only' (협회 저작권 주의)

조문 구조 감지는 worker.sync.admrul.converter._segment_body 재사용.

사용:
  PYTHONPATH=packages/python/src python scripts/voluntary_pdf_to_md.py
"""
from __future__ import annotations

import hashlib
import json
import re
import signal
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/python/src"))

import pdfplumber
import yaml

from worker.sync.admrul.converter import _segment_body
from worker.sync.laws.converter import _LawDumper, _QuotedStr, normalize_law_name


# 각 소스별 메타데이터
_ASSOC = {"license_policy": "association_copyright", "citation_mode": "quote_only"}
_KOGL = {"license_policy": "kogl_type1", "citation_mode": "full"}

SOURCES = {
    # 기존
    "kpbma": {"issuer": "한국제약바이오협회", "out": "한국제약바이오협회", **_ASSOC},
    "kmdia": {"issuer": "한국의료기기산업협회", "out": "한국의료기기산업협회", **_ASSOC},
    "ftc": {"issuer": "공정거래위원회", "out": "공정거래위원회표준약관", **_KOGL,
            "subdir": "pdf"},
    # Playwright 2차 수집분 — _pw 접미사
    "kpbma_ext": {"issuer": "한국제약바이오협회", "out": "한국제약바이오협회", **_ASSOC},
    "kmdia_pw": {"issuer": "한국의료기기산업협회", "out": "한국의료기기산업협회", **_ASSOC},
    "fss_pw": {"issuer": "금융감독원", "out": "금융감독원", **_KOGL},
    "cak_pw": {"issuer": "대한건설협회", "out": "대한건설협회", **_ASSOC},
    "cak": {"issuer": "대한건설협회", "out": "대한건설협회", **_ASSOC},
    "knia_pw": {"issuer": "손해보험협회", "out": "손해보험협회", **_ASSOC},
    "knia": {"issuer": "손해보험협회", "out": "손해보험협회", **_ASSOC},
    "kma": {"issuer": "대한의사협회", "out": "대한의사협회", **_ASSOC},
    "kma_pw": {"issuer": "대한의사협회", "out": "대한의사협회", **_ASSOC},
    # v3 신규 자동화 소스 (검증 완료)
    "kacpta": {"issuer": "한국세무사회", "out": "한국세무사회", **_ASSOC},
    "kfaa": {"issuer": "한국광고총연합회", "out": "한국광고총연합회", **_ASSOC},
    "ikfa": {"issuer": "한국프랜차이즈산업협회", "out": "한국프랜차이즈산업협회", **_ASSOC},
    "kinternet": {"issuer": "한국인터넷기업협회", "out": "한국인터넷기업협회", **_ASSOC},
}

# 각 소스 디렉토리 보정
for _sid, _cfg in SOURCES.items():
    _base = REPO_ROOT / "data/voluntary-raw" / _sid
    if "subdir" in _cfg:
        _base = _base / _cfg["subdir"]
    _cfg["base_path"] = _base
    _cfg["output_base"] = REPO_ROOT / "data/sync/kr/자율규약" / _cfg["out"]


def _extract_pdf_text(path: Path, timeout: int = 60) -> str:
    """pdfplumber 로 텍스트 추출 (타임아웃 보호)."""
    def handler(signum, frame):
        raise TimeoutError(f"pdfplumber timeout {timeout}s")

    signal.signal(signal.SIGALRM, handler)
    signal.alarm(timeout)
    try:
        with pdfplumber.open(str(path)) as pdf:
            parts = []
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
            return "\n".join(parts)
    finally:
        signal.alarm(0)


def _clean_text(t: str) -> str:
    # 페이지 번호·헤더 노이즈 제거
    t = re.sub(r"\n\s*-\s*\d+\s*-\s*\n", "\n", t)  # "- 3 -"
    t = re.sub(r"\f", "\n", t)  # form feed
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _make_law_id(source: str, title: str) -> str:
    h = hashlib.sha256(f"{source}:{title}".encode("utf-8")).hexdigest()[:12]
    return f"VC-{source.upper()}-{h}"


def _safe_segment(s: str) -> str:
    return re.sub(r"[^\w가-힣\-]", "", s)[:60] or "unnamed"


def build_markdown(source: str, pdf_path: Path, cfg: dict) -> tuple[str, Path]:
    issuer = cfg["issuer"]
    text = _clean_text(_extract_pdf_text(pdf_path))

    # 제목 추출: 파일명 기반
    title = pdf_path.stem.replace("_", " ").strip()
    normalized = normalize_law_name(title)
    law_id = _make_law_id(source, title)

    fm = {
        "제목": normalized,
        "법령MST": law_id,
        "법령ID": _QuotedStr(law_id),
        "법령구분": "자율규약",
        "법령구분코드": "VC",
        "소관부처": [issuer],
        "공포일자": "",
        "공포번호": _QuotedStr(""),
        "시행일자": "",
        "법령분야": "",
        "상태": "시행",
        "출처": f"https://www.kpbma.or.kr/ (파일: {pdf_path.name})" if source == "kpbma" else f"source: {source}",
        "document_type": "voluntary_code",
        "license_policy": cfg.get("license_policy", "association_copyright"),
        "citation_mode": cfg.get("citation_mode", "quote_only"),
        "issuer": issuer,
    }
    yaml_str = yaml.dump(
        fm, Dumper=_LawDumper, allow_unicode=True,
        default_flow_style=False, sort_keys=False,
    )

    segmented = _segment_body(text)
    body = f"# {normalized}\n\n{segmented}\n"
    md = f"---\n{yaml_str}---\n\n{body}"

    out_dir = SOURCES[source]["output_base"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_safe_segment(normalized)}.md"
    out_path.write_text(md, encoding="utf-8")
    return law_id, out_path


def main() -> int:
    for source, cfg in SOURCES.items():
        base: Path = cfg["base_path"]
        if not base.exists():
            print(f"[skip] {source}: 원본 디렉토리 없음 ({base})")
            continue
        pdfs = sorted(base.glob("*.pdf"))
        if not pdfs:
            print(f"[skip] {source}: PDF 없음")
            continue
        print(f"=== {source} ({cfg['issuer']}) — {len(pdfs)} PDF ===")
        ok = fail = 0
        for pdf in pdfs:
            try:
                lid, out = build_markdown(source, pdf, cfg)
                ok += 1
                if ok % 20 == 0 or ok == len(pdfs):
                    print(f"  [{ok}/{len(pdfs)}] 변환 중 — 최근: {pdf.name[:50]}")
            except TimeoutError as e:
                fail += 1
                print(f"  ✗ TIMEOUT {pdf.name}: {e}")
            except Exception as e:
                fail += 1
                print(f"  ✗ ERROR {pdf.name}: {type(e).__name__}: {str(e)[:100]}")
        print(f"  [{source}] ok={ok} fail={fail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
