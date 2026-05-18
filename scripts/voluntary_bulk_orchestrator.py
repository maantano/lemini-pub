"""자율규약 병렬 수집 오케스트레이터.

각 도메인별 수집기 함수를 병렬 실행:
  - ThreadPoolExecutor 로 동시 4-8개 처리
  - 각 수집기는 독립된 예외 스코프 (하나 실패해도 다른 건 계속)
  - 진행 상황을 실시간으로 JSON state 에 기록
  - 완료 시마다 체크리스트 마크다운 업데이트
  - Slack 알림 (시작/완료/실패)

사용:
  PYTHONPATH=packages/python/src .venv/bin/python scripts/voluntary_bulk_orchestrator.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/python/src"))

RAW_ROOT = REPO_ROOT / "data/voluntary-raw"
STATE_PATH = REPO_ROOT / "data/voluntary-raw/orchestrator-state.json"
CHECKLIST_PATH = REPO_ROOT / "docs/voluntary-codes-checklist.md"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("orchestrator")


# ────────────────────────────────────────────────────────────────────────
# 수집 결과 상태
# ────────────────────────────────────────────────────────────────────────

@dataclass
class CollectResult:
    source_id: str
    display_name: str
    category: str
    status: str = "⏳"  # ⏳ | 🔄 | ✅ | ⚠️ | ❌
    downloaded: int = 0
    converted: int = 0
    files: list[str] = field(default_factory=list)
    note: str = ""
    started_at: str = ""
    finished_at: str = ""
    error: str = ""


# ────────────────────────────────────────────────────────────────────────
# 유틸
# ────────────────────────────────────────────────────────────────────────

HEADERS = [
    "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "-H", "Accept-Language: ko-KR,ko;q=0.9",
]


def curl_text(url: str, timeout: int = 25, tries: int = 3, referer: str = "") -> str:
    """EUC-KR/UTF-8 자동 감지 지원."""
    for attempt in range(tries):
        cmd = ["curl", "-sSL", "--max-time", str(timeout)] + HEADERS
        if referer:
            cmd += ["-H", f"Referer: {referer}"]
        cmd += [url]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)  # bytes 로 받기
            if r.returncode == 0 and len(r.stdout) > 200:
                # 인코딩 감지: meta charset 또는 EUC-KR 시도
                raw = r.stdout
                # HTML meta 탐색
                low = raw[:2000].lower()
                if b"euc-kr" in low or b"ksc5601" in low or b"cp949" in low:
                    try:
                        return raw.decode("euc-kr", errors="replace")
                    except Exception:
                        pass
                # UTF-8 시도
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    # EUC-KR fallback
                    return raw.decode("euc-kr", errors="replace")
        except subprocess.TimeoutExpired:
            pass
        time.sleep(2)
    return ""


def curl_file(url: str, dest: Path, timeout: int = 60, referer: str = "") -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["curl", "-sSL", "--max-time", str(timeout), "-o", str(dest)] + HEADERS
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    cmd += [url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 20)
        if dest.exists() and dest.stat().st_size > 1000:
            return True
    except subprocess.TimeoutExpired:
        pass
    return False


_SAFE_RE = re.compile(r"[/\\?%*:|\"<>]")


def safe_name(s: str) -> str:
    from urllib.parse import unquote
    s = unquote(s)
    s = _SAFE_RE.sub("_", s).strip()
    return s[:150] or "unnamed"


# ────────────────────────────────────────────────────────────────────────
# 수집기 함수들 — 각 도메인별
# ────────────────────────────────────────────────────────────────────────

def _find_files(html: str, base_url: str, exts=("pdf", "hwp", "hwpx")) -> list[tuple[str, str]]:
    """HTML 에서 PDF/HWP/HWPX 링크 추출. (absolute_url, filename)."""
    found = []
    # 일반 href
    for m in re.finditer(r'href=["\']([^"\']+?\.(' + "|".join(exts) + r'))["\']', html, re.IGNORECASE):
        url = m.group(1)
        if not url.startswith("http"):
            url = urljoin(base_url, url)
        fname = safe_name(url.rsplit("/", 1)[-1])
        found.append((url, fname))
    # 목록 중복 제거
    seen = set()
    out = []
    for url, fname in found:
        if url in seen:
            continue
        seen.add(url)
        out.append((url, fname))
    return out


_KEYWORD_RE = re.compile(
    r"(공정경쟁|자율규약|자율규제|모범규준|표준약관|표준계약|규약|가이드라인|감독규정|"
    r"윤리강령|윤리규정|공시|고시|행동강령|판매기준|광고심의|심의규정)"
)


def _extract_keyword_links(html: str, base_url: str, max_links: int = 20) -> list[str]:
    """페이지에서 관련 키워드를 포함하는 내부 링크 추출."""
    links = []
    seen = set()
    for m in re.finditer(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{3,80})</a>', html,
    ):
        href, text = m.group(1), m.group(2)
        if not _KEYWORD_RE.search(text):
            continue
        # external 링크는 제외
        abs_url = urljoin(base_url, href)
        if not abs_url.startswith("http"):
            continue
        if abs_url in seen:
            continue
        # 같은 도메인만
        from urllib.parse import urlparse
        if urlparse(abs_url).netloc != urlparse(base_url).netloc:
            continue
        seen.add(abs_url)
        links.append(abs_url)
        if len(links) >= max_links:
            break
    return links


def collect_direct_scan(result: CollectResult, urls: list[str]) -> CollectResult:
    """URL 에서 PDF/HWP 링크 + 관련 키워드 링크까지 1depth recursive 스캔."""
    result.started_at = datetime.now().isoformat(timespec="seconds")
    result.status = "🔄"
    out_dir = RAW_ROOT / result.source_id
    out_dir.mkdir(parents=True, exist_ok=True)

    all_files: list[tuple[str, str]] = []
    visited = set()

    def crawl(url: str, depth: int = 0) -> None:
        if url in visited or depth > 1:
            return
        visited.add(url)
        html = curl_text(url, referer=url)
        if not html:
            return
        # 파일 링크
        all_files.extend(_find_files(html, url))
        # depth 0 에서만 키워드 기반 recursive
        if depth == 0:
            for sub_url in _extract_keyword_links(html, url, max_links=15):
                crawl(sub_url, depth=1)

    try:
        for url in urls:
            crawl(url, depth=0)

        # 중복 제거
        seen = set()
        unique = []
        for u, f in all_files:
            if u in seen:
                continue
            seen.add(u)
            unique.append((u, f))

        for url, fname in unique[:60]:  # 소스당 최대 60건
            dest = out_dir / fname
            if dest.exists() and dest.stat().st_size > 1000:
                result.files.append(str(dest.relative_to(REPO_ROOT)))
                result.downloaded += 1
                continue
            if curl_file(url, dest, referer=url):
                result.files.append(str(dest.relative_to(REPO_ROOT)))
                result.downloaded += 1

        if result.downloaded == 0:
            result.status = "⚠️"
            result.note = f"파일 없음 (방문 {len(visited)}페이지, 차단/비공개)"
        else:
            result.status = "✅"
            result.note = f"{result.downloaded}건 (방문 {len(visited)}페이지)"
    except Exception as e:
        result.status = "❌"
        result.error = f"{type(e).__name__}: {str(e)[:200]}"
        traceback.print_exc()

    result.finished_at = datetime.now().isoformat(timespec="seconds")
    return result


# ────────────────────────────────────────────────────────────────────────
# 도메인 정의
# ────────────────────────────────────────────────────────────────────────

SOURCES: list[dict] = [
    # 정부·공정거래
    {"id": "kofair", "name": "한국공정거래조정원", "cat": "정부·공정거래",
     "urls": ["https://www.kofair.or.kr/"]},

    # 의료·제약
    {"id": "kmdia", "name": "한국의료기기산업협회", "cat": "의료·제약",
     "urls": ["https://www.kmdia.or.kr/"]},

    # 금융
    {"id": "kofia", "name": "금융투자협회", "cat": "금융",
     "urls": ["https://www.kofia.or.kr/brd/m_183/list.do",
              "https://www.kofia.or.kr/brd/m_20/list.do"]},
    {"id": "fss", "name": "금융감독원", "cat": "금융",
     "urls": ["https://www.fss.or.kr/fss/lawReg/lawList.do?menuNo=200373"]},
    {"id": "kfb", "name": "은행연합회", "cat": "금융",
     "urls": ["https://www.kfb.or.kr/main/main.php"]},
    {"id": "klia", "name": "생명보험협회", "cat": "금융",
     "urls": ["https://www.klia.or.kr/"]},
    {"id": "knia", "name": "손해보험협회", "cat": "금융",
     "urls": ["https://www.knia.or.kr/"]},
    {"id": "crefia", "name": "여신금융협회", "cat": "금융",
     "urls": ["https://www.crefia.or.kr/"]},
    {"id": "fsb", "name": "저축은행중앙회", "cat": "금융",
     "urls": ["https://www.fsb.or.kr/"]},

    # 건설·부동산
    {"id": "cak", "name": "대한건설협회", "cat": "건설·부동산",
     "urls": ["https://www.cak.or.kr/"]},
    {"id": "khanet", "name": "한국주택협회", "cat": "건설·부동산",
     "urls": ["https://www.khanet.or.kr/"]},

    # 유통·광고
    {"id": "karb", "name": "한국광고자율심의기구", "cat": "광고",
     "urls": ["https://www.karb.or.kr/"]},
    {"id": "kobaco", "name": "한국방송광고진흥공사", "cat": "광고",
     "urls": ["https://www.kobaco.co.kr/"]},

    # 식품
    {"id": "kfia", "name": "한국식품산업협회", "cat": "식품",
     "urls": ["https://www.kfia.or.kr/"]},

    # 자동차
    {"id": "kama", "name": "한국자동차산업협회", "cat": "자동차",
     "urls": ["https://www.kama.or.kr/"]},

    # 전문직
    {"id": "koreanbar", "name": "대한변호사협회", "cat": "전문직·법조",
     "urls": ["https://www.koreanbar.or.kr/"]},
    {"id": "kma", "name": "대한의사협회", "cat": "전문직·법조",
     "urls": ["https://www.kma.org/"]},
    {"id": "kda", "name": "대한치과의사협회", "cat": "전문직·법조",
     "urls": ["https://www.kda.or.kr/"]},
    {"id": "kicpa", "name": "한국공인회계사회", "cat": "전문직·법조",
     "urls": ["https://www.kicpa.or.kr/"]},
    # 2차 검증 완료 추가 — PDF 직접 링크 확인된 소스들
    {"id": "kacpta", "name": "한국세무사회", "cat": "전문직·법조",
     "urls": ["https://www.kacpta.or.kr/"]},
    {"id": "kfaa", "name": "한국광고총연합회", "cat": "광고",
     "urls": ["https://www.kfaa.or.kr/"]},
    {"id": "ikfa", "name": "한국프랜차이즈산업협회", "cat": "유통·소비자",
     "urls": ["https://www.ikfa.or.kr/"]},
    {"id": "kinternet", "name": "한국인터넷기업협회", "cat": "IT·개인정보",
     "urls": ["https://www.kinternet.org/"]},

    # IT·개인정보
    {"id": "kisa", "name": "KISA", "cat": "IT·개인정보",
     "urls": ["https://www.kisa.or.kr/2060303", "https://www.kisa.or.kr/2060301"]},
    {"id": "pipc", "name": "개인정보보호위원회", "cat": "IT·개인정보",
     "urls": ["https://www.pipc.go.kr/"]},
    {"id": "kocsc", "name": "방송통신심의위원회", "cat": "IT·개인정보",
     "urls": ["https://www.kocsc.or.kr/"]},
]


# ────────────────────────────────────────────────────────────────────────
# State 관리
# ────────────────────────────────────────────────────────────────────────

_state_lock_file = REPO_ROOT / "data/voluntary-raw/.state.lock"


def write_state(results: dict[str, CollectResult]) -> None:
    data = {k: asdict(v) for k, v in results.items()}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def update_checklist(results: dict[str, CollectResult]) -> None:
    """체크리스트 마크다운을 state 기반으로 재생성."""
    if not CHECKLIST_PATH.exists():
        return
    md = CHECKLIST_PATH.read_text(encoding="utf-8")
    # 각 소스 라인 (domain name 에 해당) 을 상태로 업데이트
    # 간단한 approach: 제일 아래 "자동 기록" 섹션에 상태 로그 append
    lines = md.splitlines()

    # 기존 "누적 통계" 블록을 재계산
    total = len(results)
    done = sum(1 for r in results.values() if r.status == "✅")
    partial = sum(1 for r in results.values() if r.status == "⚠️")
    failed = sum(1 for r in results.values() if r.status == "❌")
    total_files = sum(r.downloaded for r in results.values())

    stats = [
        "## 실시간 진행 상황 (오케스트레이터 기록)",
        "",
        f"- 마지막 업데이트: {datetime.now().isoformat(timespec='seconds')}",
        f"- 완료: {done}/{total}, 부분: {partial}, 실패: {failed}",
        f"- 총 다운로드: {total_files}",
        "",
        "### 도메인별 상세",
        "",
        "| 소스 | 카테고리 | 상태 | 다운로드 | 노트 |",
        "|---|---|---|---|---|",
    ]
    for r in sorted(results.values(), key=lambda x: (x.category, x.display_name)):
        note = r.note or r.error
        stats.append(f"| {r.display_name} | {r.category} | {r.status} | {r.downloaded} | {note[:60]} |")

    # "실패·재시도 로그" 섹션 뒤에 append — 섹션 찾기
    out = []
    injected = False
    for line in lines:
        out.append(line)
        if line.startswith("## 실패·재시도 로그") and not injected:
            # 이 섹션 다음 빈줄 후 stats 를 뒤쪽 "누적 통계" 앞에 놓기 위해,
            # 전체 끝에서 stats 만 따로 달기로 단순화. 여기서는 그냥 통과.
            pass
    # 실시간 진행 섹션은 파일 끝에 매번 갱신
    # 이전 "## 실시간 진행 상황" 블록 제거 후 새로 append
    truncated = []
    skipping = False
    for line in out:
        if line.startswith("## 실시간 진행 상황"):
            skipping = True
            continue
        if skipping and line.startswith("## "):
            skipping = False
        if not skipping:
            truncated.append(line)

    final = "\n".join(truncated).rstrip() + "\n\n" + "\n".join(stats) + "\n"
    CHECKLIST_PATH.write_text(final, encoding="utf-8")


# ────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────

def main() -> int:
    results: dict[str, CollectResult] = {
        s["id"]: CollectResult(source_id=s["id"], display_name=s["name"], category=s["cat"])
        for s in SOURCES
    }

    log.info(f"=== 병렬 수집 시작: {len(SOURCES)} 소스 ===")
    write_state(results)
    update_checklist(results)

    def task(source: dict) -> CollectResult:
        r = results[source["id"]]
        log.info(f"[{source['id']}] 시작: {source['name']}")
        collect_direct_scan(r, source["urls"])
        log.info(f"[{source['id']}] 완료: {r.status} (다운로드 {r.downloaded})")
        return r

    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="collect") as pool:
        futures = {pool.submit(task, s): s for s in SOURCES}
        for fut in as_completed(futures):
            try:
                r = fut.result()
                results[r.source_id] = r
            except Exception as e:
                s = futures[fut]
                log.exception(f"[{s['id']}] 치명적 오류")
                results[s["id"]].status = "❌"
                results[s["id"]].error = f"{type(e).__name__}: {e}"
            # 매 완료마다 state + checklist 업데이트
            write_state(results)
            update_checklist(results)

    log.info("=== 모든 수집 완료 ===")
    done = sum(1 for r in results.values() if r.status == "✅")
    partial = sum(1 for r in results.values() if r.status == "⚠️")
    failed = sum(1 for r in results.values() if r.status == "❌")
    total_files = sum(r.downloaded for r in results.values())
    log.info(f"결과: 완료 {done} / 부분 {partial} / 실패 {failed} / 총 파일 {total_files}")

    # Slack 알림
    try:
        from worker.sync.core import alerts
        alerts.success("voluntary-bulk", {
            "sources_done": done,
            "sources_partial": partial,
            "sources_failed": failed,
            "files_downloaded": total_files,
        })
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
