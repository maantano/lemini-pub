"""HWP → PDF 일괄 변환 (LibreOffice + H2Orestart).

입력: data/voluntary-raw/ftc/hwp/*.hwp
출력: data/voluntary-raw/ftc/pdf/*.pdf

병렬 불가능 (soffice 단일 인스턴스) — 순차 처리.
각 파일 변환은 subprocess.run 타임아웃 걸어서 hang 방지.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HWP_DIR = REPO_ROOT / "data/voluntary-raw/ftc/hwp"
PDF_DIR = REPO_ROOT / "data/voluntary-raw/ftc/pdf"
PDF_DIR.mkdir(parents=True, exist_ok=True)


def convert_one(hwp: Path, timeout: int = 120) -> tuple[bool, str]:
    expected = PDF_DIR / (hwp.stem + ".pdf")
    if expected.exists() and expected.stat().st_size > 1000:
        return True, "이미 변환됨"
    try:
        r = subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf",
             "--outdir", str(PDF_DIR), str(hwp)],
            capture_output=True, text=True, timeout=timeout,
        )
        if expected.exists() and expected.stat().st_size > 1000:
            return True, f"{expected.stat().st_size} bytes"
        return False, f"변환 파일 없음 (stderr: {r.stderr[:200]})"
    except subprocess.TimeoutExpired:
        return False, f"timeout {timeout}s"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    # 기존 soffice 프로세스 kill (H2Orestart 활성화 이슈 회피)
    subprocess.run(["pkill", "-f", "soffice"], capture_output=True)
    time.sleep(2)

    hwps = sorted(HWP_DIR.glob("*.hwp")) + sorted(HWP_DIR.glob("*.hwpx"))
    print(f"대상: {len(hwps)} HWP/HWPX")
    ok = fail = skip = 0
    fails: list[tuple[str, str]] = []
    for i, hwp in enumerate(hwps, 1):
        success, msg = convert_one(hwp)
        if success and "이미" in msg:
            skip += 1
        elif success:
            ok += 1
            if i % 10 == 0 or i == len(hwps):
                print(f"  [{i}/{len(hwps)}] ok={ok} skip={skip} fail={fail}")
        else:
            fail += 1
            fails.append((hwp.name, msg))
            print(f"  [{i}/{len(hwps)}] ✗ {hwp.name}: {msg}")

    print()
    print(f"결과: ok={ok} skip(이미있음)={skip} fail={fail}")
    if fails:
        print("실패 목록:")
        for n, m in fails:
            print(f"  {n}: {m}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
