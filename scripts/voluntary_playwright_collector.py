"""Playwright 기반 자율규약 2차 수집기 (SPA/AJAX 사이트 커버).

각 사이트 → 자율규약·공정경쟁·모범규준·윤리강령 키워드 네비게이션 →
렌더링 후 PDF/HWP 링크 모두 다운로드.

타임아웃 철저 (페이지 30s, 파일 60s).
병렬 실행 (asyncio.gather, concurrency=4).
state.json + 체크리스트 실시간 업데이트.

사용:
  PYTHONPATH=packages/python/src .venv/bin/python scripts/voluntary_playwright_collector.py
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = REPO_ROOT / "data/voluntary-raw"
STATE_PATH = RAW_ROOT / "playwright-state.json"
CHECKLIST_PATH = REPO_ROOT / "docs/voluntary-codes-checklist.md"

_SAFE_RE = re.compile(r"[/\\?%*:|\"<>]")
_KEYWORD_RE = re.compile(
    r"(공정경쟁|자율규약|자율규제|모범규준|표준약관|표준계약|규약|가이드라인|"
    r"감독규정|윤리강령|윤리규정|공시|고시|행동강령|광고심의|심의규정|"
    r"업무규정|준칙|지침)"
)


def safe_name(s: str) -> str:
    s = unquote(s)
    s = _SAFE_RE.sub("_", s).strip()
    return s[:180] or "unnamed"


@dataclass
class Result:
    source_id: str
    display_name: str
    category: str
    status: str = "⏳"
    pages_visited: int = 0
    files_found: int = 0
    downloaded: int = 0
    files: list[str] = field(default_factory=list)
    note: str = ""
    error: str = ""
    started_at: str = ""
    finished_at: str = ""


# ────────────────────────────────────────────────────────────
# 도메인 리스트 — 이번엔 남은 전체 커버
# ────────────────────────────────────────────────────────────

SOURCES: list[dict] = [
    # 의료·제약
    {"id": "kmdia_pw", "name": "한국의료기기산업협회", "cat": "의료·제약",
     "start": "https://www.kmdia.or.kr/"},
    {"id": "kpbma_ext", "name": "한국제약바이오협회(확장)", "cat": "의료·제약",
     "start": "https://www.kpbma.or.kr/"},
    {"id": "kaoma", "name": "한국한의학회", "cat": "의료·제약",
     "start": "https://www.kaoma.or.kr/"},

    # 금융
    {"id": "kofia_pw", "name": "금융투자협회", "cat": "금융",
     "start": "https://www.kofia.or.kr/"},
    {"id": "kofia_law", "name": "금투협 법규포털", "cat": "금융",
     "start": "https://law.kofia.or.kr/"},
    {"id": "fss_pw", "name": "금융감독원", "cat": "금융",
     "start": "https://www.fss.or.kr/"},
    {"id": "kfb_pw", "name": "은행연합회", "cat": "금융",
     "start": "https://www.kfb.or.kr/"},
    {"id": "klia_pw", "name": "생명보험협회", "cat": "금융",
     "start": "https://www.klia.or.kr/"},
    {"id": "knia_pw", "name": "손해보험협회", "cat": "금융",
     "start": "https://www.knia.or.kr/"},
    {"id": "crefia_pw", "name": "여신금융협회", "cat": "금융",
     "start": "https://www.crefia.or.kr/"},
    {"id": "fsb_pw", "name": "저축은행중앙회", "cat": "금융",
     "start": "https://www.fsb.or.kr/"},
    {"id": "cu_pw", "name": "신협중앙회", "cat": "금융",
     "start": "https://www.cu.co.kr/"},

    # IT·개인정보
    {"id": "kisa_pw", "name": "KISA", "cat": "IT·개인정보",
     "start": "https://www.kisa.or.kr/2060303"},
    {"id": "pipc_pw", "name": "개인정보보호위원회", "cat": "IT·개인정보",
     "start": "https://www.pipc.go.kr/"},
    {"id": "kocsc_pw", "name": "방송통신심의위원회", "cat": "IT·개인정보",
     "start": "https://www.kocsc.or.kr/"},

    # 건설·부동산
    {"id": "cak_pw", "name": "대한건설협회", "cat": "건설·부동산",
     "start": "https://www.cak.or.kr/"},
    {"id": "khanet_pw", "name": "한국주택협회", "cat": "건설·부동산",
     "start": "https://www.khanet.or.kr/"},

    # 유통·소비자
    {"id": "korcham", "name": "대한상공회의소", "cat": "유통·소비자",
     "start": "https://www.korcham.net/"},
    {"id": "retail", "name": "한국유통산업연합회", "cat": "유통·소비자",
     "start": "https://www.kcta.or.kr/"},

    # 광고
    {"id": "karb_pw", "name": "한국광고자율심의기구", "cat": "광고",
     "start": "https://www.karb.or.kr/"},
    {"id": "kobaco_pw", "name": "한국방송광고진흥공사", "cat": "광고",
     "start": "https://www.kobaco.co.kr/"},

    # 식품
    {"id": "kfia_pw", "name": "한국식품산업협회", "cat": "식품",
     "start": "https://www.kfia.or.kr/"},
    {"id": "kalia", "name": "한국주류산업협회", "cat": "식품",
     "start": "https://www.kalia.or.kr/"},

    # 자동차
    {"id": "kama_pw", "name": "한국자동차산업협회", "cat": "자동차",
     "start": "https://www.kama.or.kr/"},
    {"id": "kamma", "name": "한국자동차모빌리티산업협회", "cat": "자동차",
     "start": "https://www.kamma.or.kr/"},

    # 전문직
    {"id": "koreanbar_pw", "name": "대한변호사협회", "cat": "전문직·법조",
     "start": "https://www.koreanbar.or.kr/"},
    {"id": "kma_pw", "name": "대한의사협회", "cat": "전문직·법조",
     "start": "https://www.kma.org/"},
    {"id": "kda_pw", "name": "대한치과의사협회", "cat": "전문직·법조",
     "start": "https://www.kda.or.kr/"},
    {"id": "kicpa_pw", "name": "한국공인회계사회", "cat": "전문직·법조",
     "start": "https://www.kicpa.or.kr/"},
]


def write_state(results: dict[str, Result]) -> None:
    data = {k: asdict(v) for k, v in results.items()}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def update_checklist_block(results: dict[str, Result]) -> None:
    if not CHECKLIST_PATH.exists():
        return
    md = CHECKLIST_PATH.read_text(encoding="utf-8")
    marker = "## Playwright 2차 수집 (SPA/AJAX 사이트)"
    block_lines = [
        marker,
        "",
        f"- 마지막 업데이트: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "| 소스 | 카테고리 | 상태 | 방문 | 파일 | 다운 | 노트 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results.values(), key=lambda x: (x.category, x.display_name)):
        note = (r.note or r.error)[:50]
        block_lines.append(
            f"| {r.display_name} | {r.category} | {r.status} | "
            f"{r.pages_visited} | {r.files_found} | {r.downloaded} | {note} |"
        )

    if marker in md:
        parts = md.split(marker, 1)
        post = parts[1]
        # 다음 ## 섹션이 있으면 그 전까지만 교체
        next_section = re.search(r"\n## ", post)
        if next_section:
            tail = post[next_section.start():]
        else:
            tail = ""
        new = parts[0] + "\n".join(block_lines) + "\n" + tail
    else:
        new = md.rstrip() + "\n\n" + "\n".join(block_lines) + "\n"

    CHECKLIST_PATH.write_text(new, encoding="utf-8")


async def collect_one(context, source: dict, result: Result) -> Result:
    from playwright.async_api import TimeoutError as PWTimeout
    result.started_at = datetime.now().isoformat(timespec="seconds")
    result.status = "🔄"
    out_dir = RAW_ROOT / result.source_id
    out_dir.mkdir(parents=True, exist_ok=True)

    visited = set()
    file_urls: list[tuple[str, str]] = []  # (url, filename)

    async def scan_page(page, url: str, depth: int = 0) -> None:
        if url in visited or depth > 1:
            return
        visited.add(url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(1500)  # JS 살짝 대기
        except PWTimeout:
            return
        except Exception:
            return
        result.pages_visited += 1

        # PDF/HWP 링크 추출 (렌더 후)
        links = await page.evaluate(
            """() => {
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                return anchors.map(a => ({href: a.href, text: (a.innerText || a.textContent || '').trim()}));
            }"""
        )

        for link in links:
            href = link.get("href") or ""
            text = link.get("text") or ""
            low = href.lower()
            if any(low.endswith(ext) or (ext + "?") in low for ext in (".pdf", ".hwp", ".hwpx")):
                fname = safe_name(href.rsplit("/", 1)[-1].split("?")[0])
                file_urls.append((href, fname))

        if depth == 0:
            # 키워드 링크 수집 → recursive
            keyword_links = []
            for link in links:
                text = link.get("text") or ""
                href = link.get("href") or ""
                if not href or not text:
                    continue
                if not _KEYWORD_RE.search(text):
                    continue
                if not href.startswith("http"):
                    continue
                if urlparse(href).netloc != urlparse(url).netloc:
                    continue
                if href not in visited:
                    keyword_links.append(href)

            for sub in keyword_links[:12]:
                await scan_page(page, sub, depth=1)

    try:
        page = await context.new_page()
        page.set_default_timeout(25_000)

        await scan_page(page, source["start"], depth=0)

        # 중복 제거
        seen = set()
        unique = []
        for u, f in file_urls:
            if u in seen:
                continue
            seen.add(u)
            unique.append((u, f))
        result.files_found = len(unique)

        # 다운로드 (curl 이 playwright download 보다 안정적)
        for url, fname in unique[:80]:
            dest = out_dir / fname
            if dest.exists() and dest.stat().st_size > 1000:
                result.files.append(str(dest.relative_to(REPO_ROOT)))
                result.downloaded += 1
                continue
            cmd = ["curl", "-sSL", "--max-time", "60",
                   "-H", "User-Agent: Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
                   "-H", f"Referer: {source['start']}",
                   "-o", str(dest), url]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=70)
                if dest.exists() and dest.stat().st_size > 1000:
                    result.files.append(str(dest.relative_to(REPO_ROOT)))
                    result.downloaded += 1
            except subprocess.TimeoutExpired:
                continue

        await page.close()

        if result.downloaded == 0:
            result.status = "⚠️"
            result.note = f"파일 못찾음 (방문 {result.pages_visited}p, 후보 {result.files_found}개 중 다운 0)"
        else:
            result.status = "✅"
            result.note = f"{result.downloaded}건 (방문 {result.pages_visited}p, 후보 {result.files_found})"

    except Exception as e:
        result.status = "❌"
        result.error = f"{type(e).__name__}: {str(e)[:180]}"
        traceback.print_exc()

    result.finished_at = datetime.now().isoformat(timespec="seconds")
    return result


async def main() -> int:
    from playwright.async_api import async_playwright

    results: dict[str, Result] = {
        s["id"]: Result(source_id=s["id"], display_name=s["name"], category=s["cat"])
        for s in SOURCES
    }
    write_state(results)
    update_checklist_block(results)

    print(f"=== Playwright 2차 수집: {len(SOURCES)}개 도메인 ===")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            locale="ko-KR",
        )

        # 동시 처리 — 브라우저 메모리 고려해 5개 병렬
        sem = asyncio.Semaphore(5)

        async def run_one(source):
            async with sem:
                r = results[source["id"]]
                print(f"  → {source['id']} 시작")
                await collect_one(context, source, r)
                print(f"  ← {source['id']} {r.status} 방문{r.pages_visited} 다운{r.downloaded}")
                write_state(results)
                update_checklist_block(results)

        await asyncio.gather(*[run_one(s) for s in SOURCES], return_exceptions=True)

        await context.close()
        await browser.close()

    ok = sum(1 for r in results.values() if r.status == "✅")
    partial = sum(1 for r in results.values() if r.status == "⚠️")
    fail = sum(1 for r in results.values() if r.status == "❌")
    total = sum(r.downloaded for r in results.values())
    print(f"\n결과: ✅{ok}  ⚠️{partial}  ❌{fail}  총 파일 {total}")

    try:
        import sys as _sys
        _sys.path.insert(0, str(REPO_ROOT / "packages/python/src"))
        from worker.sync.core import alerts
        alerts.success("voluntary-playwright", {
            "sources_ok": ok, "sources_partial": partial, "sources_failed": fail,
            "files_downloaded": total,
        })
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
