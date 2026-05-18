"""가이드 문서에 있는 모든 URL 과 네비게이션 경로를 실제 접속해 검증.

각 URL 에 대해:
  1. HTTP 상태
  2. 응답 본문에 "자율규약/공정경쟁/규약" 키워드 유무
  3. PDF/HWP 다운로드 링크 포함 여부
  4. "로그인 필요" 류 문구 유무

결과를 `docs/voluntary-codes-url-verification.md` 에 저장.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "docs/voluntary-codes-url-verification.md"

# (카테고리, 기관명, URL, 기대 키워드)
TARGETS = [
    # 정부·공정거래
    ("정부·공정거래", "공정위 표준약관", "https://www.ftc.go.kr/www/selectBbsNttList.do?bordCd=201&key=202", "표준약관"),
    ("정부·공정거래", "한국공정경쟁연합회 법규집", "https://www.kfcf.or.kr/publishing/regulations/list.do", "규약"),
    ("정부·공정거래", "한국공정경쟁연합회 정책자료실", "https://www.kfcf.or.kr/archive/policy/list.do", "자료"),
    ("정부·공정거래", "한국공정경쟁연합회 연합회자료실", "https://www.kfcf.or.kr/archive/association/list.do", "자료"),

    # 의료·제약
    ("의료·제약", "KMDIA 메인", "https://www.kmdia.or.kr/", "공정경쟁"),
    ("의료·제약", "KMDIA Policy/Rule 추정", "https://www.kmdia.or.kr/Policy/Rule", ""),
    ("의료·제약", "KPBMA 공정경쟁 자료실", "https://www.kpbma.or.kr/library/trend/fair/list", "공정경쟁"),
    ("의료·제약", "KAOMA 한의학회", "https://www.kaoma.or.kr/", "윤리"),
    ("의료·제약", "대한의사협회", "https://www.kma.org/", "의료광고"),
    ("의료·제약", "대한치과의사협회", "https://www.kda.or.kr/", "윤리"),
    ("의료·제약", "대한약사회", "https://www.kpanet.or.kr/", "자료"),
    ("의료·제약", "대한간호협회", "https://www.koreanurse.or.kr/", "윤리"),

    # 금융
    ("금융", "금투협 법규포털", "https://law.kofia.or.kr/", "자율"),
    ("금융", "금투협 메인", "https://www.kofia.or.kr/", "규정"),
    ("금융", "금감원 법규정보", "https://www.fss.or.kr/fss/lawReg/lawList.do?menuNo=200373", "감독규정"),
    ("금융", "은행연합회", "https://www.kfb.or.kr/", "규약"),
    ("금융", "생명보험협회", "https://www.klia.or.kr/", "자율"),
    ("금융", "손해보험협회", "https://www.knia.or.kr/", "자율"),
    ("금융", "여신금융협회", "https://www.crefia.or.kr/", "자율"),
    ("금융", "저축은행중앙회", "https://www.fsb.or.kr/", "규약"),
    ("금융", "신협중앙회", "https://www.cu.co.kr/", "규약"),
    ("금융", "새마을금고중앙회", "https://www.kfcc.co.kr/", "규약"),
    ("금융", "한국거래소 규정포털", "https://regulation.krx.co.kr/", "규정"),

    # IT·개인정보
    ("IT·개인정보", "KISA 가이드라인", "https://www.kisa.or.kr/2060303", "가이드"),
    ("IT·개인정보", "KISA 메인", "https://www.kisa.or.kr/", "가이드"),
    ("IT·개인정보", "개인정보보호위원회", "https://www.pipc.go.kr/", "가이드"),
    ("IT·개인정보", "방송통신심의위원회", "https://www.kocsc.or.kr/", "심의"),
    ("IT·개인정보", "방송통신위원회", "https://www.kcc.go.kr/", "고시"),

    # 건설·부동산
    ("건설·부동산", "대한건설협회", "https://www.cak.or.kr/", "표준"),
    ("건설·부동산", "한국주택협회", "https://www.khanet.or.kr/", "표준"),
    ("건설·부동산", "대한주택건설협회", "https://www.kha.or.kr/", "표준"),
    ("건설·부동산", "한국부동산협회", "https://www.kreaa.or.kr/", "표준"),

    # 유통·광고
    ("광고", "한국광고자율심의기구", "https://www.karb.or.kr/", "심의"),
    ("광고", "한국방송광고진흥공사", "https://www.kobaco.co.kr/", "광고"),
    ("광고", "한국광고총연합회", "https://www.kfaa.or.kr/", "광고"),

    # 식품·주류
    ("식품", "한국식품산업협회", "https://www.kfia.or.kr/", "공정경쟁"),
    ("식품", "한국주류산업협회", "https://www.kalia.or.kr/", "광고"),
    ("식품", "한국외식업중앙회", "https://www.ikra.or.kr/", "자료"),

    # 자동차
    ("자동차", "한국자동차산업협회 KAMA", "https://www.kama.or.kr/", "규약"),
    ("자동차", "한국자동차모빌리티산업협회 KAMMA", "https://www.kamma.or.kr/", "공정경쟁"),

    # 전문직
    ("전문직", "대한변호사협회", "https://www.koreanbar.or.kr/", "광고"),
    ("전문직", "대한법무사회", "https://www.kabl.kr/", "윤리"),
    ("전문직", "한국공인회계사회", "https://www.kicpa.or.kr/", "윤리"),
    ("전문직", "한국세무사회", "https://www.kacpta.or.kr/", "윤리"),
    ("전문직", "대한변리사회", "https://www.kpaa.or.kr/", "윤리"),

    # 기타
    ("유통·소비자", "대한상공회의소", "https://www.korcham.net/", "표준"),
    ("유통·소비자", "한국프랜차이즈산업협회", "https://www.ikfa.or.kr/", "가맹"),
    ("IT", "한국인터넷기업협회", "https://www.kinternet.org/", "자율"),
    ("IT", "한국게임산업협회", "https://www.gamek.or.kr/", "자율"),
]


def curl(url: str, timeout: int = 20) -> tuple[int, str, str]:
    """Returns (http_status, final_url, body_text)."""
    cmd = ["curl", "-sSLI", "-o", "/dev/null",
           "-w", "%{http_code}\t%{url_effective}",
           "--max-time", str(timeout),
           "-H", "User-Agent: Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
           url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
        parts = (r.stdout or "").strip().split("\t")
        status = int(parts[0]) if parts and parts[0].isdigit() else 0
        final = parts[1] if len(parts) > 1 else url
    except Exception:
        return 0, url, ""

    # 본문 가져오기 (charset 자동 처리 단순화)
    body_cmd = ["curl", "-sSL", "--max-time", str(timeout),
                "-H", "User-Agent: Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
                url]
    try:
        r2 = subprocess.run(body_cmd, capture_output=True, timeout=timeout + 10)
        raw = r2.stdout
        if b"euc-kr" in raw[:2000].lower() or b"cp949" in raw[:2000].lower():
            body = raw.decode("euc-kr", errors="replace")
        else:
            try:
                body = raw.decode("utf-8")
            except UnicodeDecodeError:
                body = raw.decode("euc-kr", errors="replace")
    except Exception:
        body = ""
    return status, final, body


def analyze(body: str, expected: str) -> dict:
    if not body:
        return {"len": 0, "has_kw": False, "pdfs": 0, "hwps": 0, "needs_login": False, "is_spa": False}
    has_kw = (expected in body) if expected else True
    pdfs = len(set(re.findall(r'(?:href|src)=["\'][^"\']+\.pdf["\']', body, re.I)))
    hwps = len(set(re.findall(r'(?:href|src)=["\'][^"\']+\.(?:hwp|hwpx)["\']', body, re.I)))
    needs_login = any(kw in body for kw in ["로그인", "회원가입", "login", "Login"])
    is_spa = (
        len(body) < 2000 and ("<div id=\"root\"" in body or "<div id=\"app\"" in body
                              or "<script" in body and "react" in body.lower())
    )
    return {
        "len": len(body),
        "has_kw": has_kw,
        "pdfs": pdfs,
        "hwps": hwps,
        "needs_login": needs_login,
        "is_spa": is_spa,
    }


def main() -> int:
    rows = []
    print(f"=== 총 {len(TARGETS)}개 URL 검증 ===\n")
    for i, (cat, name, url, exp) in enumerate(TARGETS, 1):
        status, final, body = curl(url)
        info = analyze(body, exp)

        # 상태 판정
        if status == 0:
            verdict = "❌ 접속불가"
        elif status >= 400:
            verdict = f"❌ HTTP {status}"
        elif info["len"] < 500:
            verdict = f"⚠️ 빈페이지({info['len']}b)"
        elif info["is_spa"]:
            verdict = "⚠️ SPA"
        elif info["needs_login"] and info["pdfs"] == 0 and info["hwps"] == 0:
            verdict = "🔴 로그인요구"
        elif info["pdfs"] + info["hwps"] > 0:
            verdict = f"✅ 파일 {info['pdfs']+info['hwps']}개"
        elif info["has_kw"]:
            verdict = "🟡 키워드O/파일X"
        else:
            verdict = "⚠️ 키워드X"

        print(f"  [{i}/{len(TARGETS)}] {verdict:18s}  {name[:25]:25s}  {url[:80]}")
        rows.append({
            "cat": cat, "name": name, "url": url, "expected": exp,
            "status": status, "final": final, "verdict": verdict, **info,
        })
        time.sleep(0.3)

    # 마크다운 테이블로 저장
    lines = [
        "# 수집 가이드 URL 실접속 검증 결과",
        "",
        f"- 검증 시각: 2026-04-21",
        f"- 총 URL: {len(rows)}",
        "",
        "## 판정 요약",
        "",
        "| verdict | 건수 |",
        "|---|---|",
    ]
    from collections import Counter
    vc = Counter(r["verdict"][:2] for r in rows)
    for v, n in vc.most_common():
        lines.append(f"| {v} | {n} |")

    lines.extend([
        "",
        "## 전체 결과",
        "",
        "| 카테고리 | 기관 | URL | HTTP | 길이 | PDF/HWP | 판정 |",
        "|---|---|---|---|---|---|---|",
    ])
    for r in rows:
        files = f"{r['pdfs']}/{r['hwps']}"
        lines.append(
            f"| {r['cat']} | {r['name']} | `{r['url'][:60]}` | {r['status']} | {r['len']} | {files} | {r['verdict']} |"
        )

    OUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n저장: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
