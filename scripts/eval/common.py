"""eval 스크립트 공통 유틸."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path


def extract_text(local_path: Path) -> str:
    """파일에서 텍스트 추출. 실패 시 빈 문자열."""
    suffix = local_path.suffix.lower()
    try:
        if suffix == ".pdf":
            try:
                import pypdf  # type: ignore
                reader = pypdf.PdfReader(str(local_path))
                # v3: 50페이지 제한 제거 — 판결서·의견서 뒷부분 손실 방지
                return "\n".join((p.extract_text() or "") for p in reader.pages)
            except Exception:
                return ""
        if suffix in (".docx",):
            try:
                from docx import Document  # type: ignore
                doc = Document(str(local_path))
                return "\n".join(p.text for p in doc.paragraphs)
            except Exception:
                return ""
        if suffix in (".txt", ".md", ".csv", ".json"):
            return local_path.read_text(encoding="utf-8", errors="ignore")
        return ""
    except Exception:
        return ""


def download_slack_file(url: str, dest: Path, token: str) -> bool:
    """Slack 파일 다운로드. 성공 시 True."""
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req) as r:
            ct = r.headers.get("Content-Type", "")
            data = r.read()
            if ct.startswith("text/html") and len(data) < 200_000:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return True
    except Exception:
        return False
