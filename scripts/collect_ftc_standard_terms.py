"""공정위 표준약관 게시판 전량 수집.

순서:
  1. 목록 페이지 전부 순회 → nttSn + 제목 수집
  2. 각 게시물 상세 접근 → 첨부파일(HWP/PDF) atchmnflNo 추출
  3. curl 로 첨부 다운로드 → data/voluntary-raw/ftc/
  4. 확장자별 분류 (HWP 는 별도, PDF 는 pdf/ 서브디렉토리)

HWP 는 현재 LibreOffice headless 에서 변환 불가. 파일만 받아놓고 차후 처리.
PDF 는 즉시 처리 가능.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "data" / "voluntary-raw" / "ftc"
HWP_DIR = OUT_DIR / "hwp"
PDF_DIR = OUT_DIR / "pdf"
HWP_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)

BASE = "https://www.ftc.go.kr/www"
LIST_URL = f"{BASE}/selectBbsNttList.do?bordCd=201&key=202"
VIEW_URL = BASE + "/selectBbsNttView.do?key=202&bordCd=201&nttSn={}"
DOWN_URL = BASE + "/downloadBbsFile.do?atchmnflNo={}"

HEADERS = [
    "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "-H", "Accept-Language: ko-KR,ko;q=0.9",
    "-H", "Referer: https://www.ftc.go.kr/",
]


def curl_text(url: str, timeout: int = 30, tries: int = 3) -> str:
    for _ in range(tries):
        r = subprocess.run(
            ["curl", "-sSL", "--max-time", str(timeout)] + HEADERS + [url],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and len(r.stdout) > 200:
            return r.stdout
        time.sleep(2)
    return r.stdout


def curl_file(url: str, dest: Path, timeout: int = 60) -> bool:
    r = subprocess.run(
        ["curl", "-sSL", "--max-time", str(timeout), "-o", str(dest)] + HEADERS + [url],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 1000


def _safe_filename(s: str) -> str:
    s = unquote(s)
    s = re.sub(r"[/\\?%*:|\"<>]", "_", s).strip()
    return s[:150] or "unnamed"


def list_posts() -> list[tuple[str, str]]:
    """전체 페이지 순회 → [(nttSn, title)]."""
    posts: list[tuple[str, str]] = []
    seen = set()
    page = 1
    while True:
        url = f"{LIST_URL}&pageIndex={page}" if page > 1 else LIST_URL
        html = curl_text(url)
        if not html:
            print(f"  [page {page}] 빈 응답 — 중단")
            break
        rows = re.findall(r"<tr[^>]*>.*?</tr>", html, re.DOTALL)
        page_posts = []
        for row in rows:
            m = re.search(r"nttSn=(\d+)", row)
            if not m:
                continue
            sn = m.group(1)
            if sn in seen:
                continue
            # 제목 추출 — <a ...>제목</a>
            tm = re.search(r"<a[^>]*nttSn=" + sn + r"[^>]*>\s*([^<]+?)\s*</a>", row)
            title = tm.group(1).strip() if tm else ""
            if not title:
                # fallback: 행 전체 텍스트
                clean = re.sub(r"<[^>]+>", " ", row)
                clean = re.sub(r"\s+", " ", clean).strip()
                title = clean[:120]
            page_posts.append((sn, title))
            seen.add(sn)
        if not page_posts:
            break
        posts.extend(page_posts)
        print(f"  [page {page}] +{len(page_posts)}개 (누적 {len(posts)})")
        page += 1
        if page > 30:
            print("  안전장치: 30페이지 초과")
            break
        time.sleep(0.3)
    return posts


def collect_attachments(ntt_sn: str) -> list[tuple[str, str]]:
    """게시물 상세에서 (atchmnflNo, 파일명) 추출."""
    html = curl_text(VIEW_URL.format(ntt_sn))
    # 패턴: <a href="./downloadBbsFile.do?atchmnflNo=NNN" class="p-attach__link">파일명.ext &nbsp; ...</a>
    matches = re.findall(
        r'href="\./downloadBbsFile\.do\?atchmnflNo=(\d+)"[^>]*>([^<]+?)(?:&nbsp;|</a>)',
        html,
    )
    result = []
    seen = set()
    for no, fname in matches:
        clean = fname.strip()
        if not clean or no in seen:
            continue
        seen.add(no)
        result.append((no, clean))
    return result


def main() -> int:
    print("=== 공정위 표준약관 게시판 전수 ===")
    posts = list_posts()
    print(f"\n총 게시물: {len(posts)}개\n")

    hwp_count = pdf_count = skip = fail = 0
    records = []  # 메타 기록용

    for i, (sn, title) in enumerate(posts, 1):
        try:
            atts = collect_attachments(sn)
        except Exception as e:
            print(f"  [{i}/{len(posts)}] {sn} {title[:40]} — 상세 실패: {e}")
            fail += 1
            continue
        if not atts:
            skip += 1
            continue

        for no, fname in atts:
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else "bin"
            safe = _safe_filename(fname)
            target_dir = PDF_DIR if ext == "pdf" else HWP_DIR
            dest = target_dir / f"{sn}_{safe}"
            if dest.exists():
                skip += 1
                continue
            ok = curl_file(DOWN_URL.format(no), dest)
            if ok:
                if ext == "pdf":
                    pdf_count += 1
                elif ext in ("hwp", "hwpx"):
                    hwp_count += 1
                records.append({"ntt_sn": sn, "title": title, "filename": fname,
                                "saved": str(dest.relative_to(REPO_ROOT)),
                                "ext": ext, "atch_no": no})
                print(f"  [{i}/{len(posts)}] {ext.upper()} {title[:50]} → {dest.name}")
            else:
                fail += 1
                print(f"  [{i}/{len(posts)}] {ext.upper()} {title[:50]} — 다운로드 실패")
            time.sleep(0.2)

    # 메타 기록
    import json
    meta_path = OUT_DIR / "manifest.json"
    meta_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"=== 완료 ===")
    print(f"  PDF: {pdf_count}건 → {PDF_DIR}")
    print(f"  HWP/HWPX: {hwp_count}건 → {HWP_DIR}")
    print(f"  skip: {skip}, fail: {fail}")
    print(f"  manifest: {meta_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
