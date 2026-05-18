#!/usr/bin/env python3
from __future__ import annotations

import html
import importlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SRC = REPO_ROOT / "packages" / "python" / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

CHANNEL_ID = "C04EUJENPR7"
CHANNEL_NAME_FALLBACK = "법률 검토"
OUTPUT_DIR = REPO_ROOT / "data" / "slack-legal-qa"
ATTACHMENTS_ROOT = OUTPUT_DIR / "_attachments"
USER_CACHE_PATH = REPO_ROOT / ".cache" / "slack_users.json"

REQUIRED_SCOPES = [
    "channels:history",
    "groups:history",
    "channels:read",
    "groups:read",
    "users:read",
    "files:read",
]

KST = ZoneInfo("Asia/Seoul")

IMPORT_CACHE: dict[str, Any | None] = {}
INSTALL_ATTEMPTED: set[str] = set()


class MissingScopeError(RuntimeError):
    def __init__(self, message: str = "Missing required Slack scope") -> None:
        super().__init__(message)


@dataclass
class ExtractResult:
    extracted: bool
    method: str
    text: str
    error: str | None = None


@dataclass
class Stats:
    total_threads: int = 0
    created_md: int = 0
    skipped_existing_md: int = 0
    downloaded_attachments: int = 0
    skipped_existing_attachments: int = 0
    extract_success: int = 0
    extract_failed: int = 0


def optional_import(module_name: str, package_name: str | None = None) -> Any | None:
    if module_name in IMPORT_CACHE:
        return IMPORT_CACHE[module_name]

    try:
        mod = importlib.import_module(module_name)
        IMPORT_CACHE[module_name] = mod
        return mod
    except ImportError:
        pass

    if package_name and package_name not in INSTALL_ATTEMPTED:
        INSTALL_ATTEMPTED.add(package_name)
        print(f"[deps] trying to install optional package: {package_name}")
        proc = subprocess.run(
            ["uv", "pip", "install", package_name],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            print(f"[deps] install failed for {package_name}: {proc.stderr.strip()[:200]}")

    try:
        mod = importlib.import_module(module_name)
        IMPORT_CACHE[module_name] = mod
        return mod
    except ImportError:
        IMPORT_CACHE[module_name] = None
        return None


def get_tqdm():
    tqdm_mod = optional_import("tqdm", "tqdm")
    if tqdm_mod is None:
        return None
    return getattr(tqdm_mod, "tqdm", None)


def ts_to_kst_iso(ts: str | float | int) -> str:
    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(KST)
    return dt.isoformat()


def ts_to_kst_date(ts: str | float | int) -> str:
    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(KST)
    return dt.strftime("%Y-%m-%d")


def sanitize_filename(name: str, default_stem: str = "file") -> str:
    cleaned = name.replace("/", "_").replace("\\", "_").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[^\w\-. ()가-힣]", "_", cleaned)
    cleaned = cleaned.strip(" .")
    return cleaned or default_stem


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    return html.unescape(text).strip()


def is_bot_or_app_message(message: dict[str, Any]) -> bool:
    return (
        message.get("subtype") == "bot_message"
        or bool(message.get("app_id"))
        or bool(message.get("bot_id"))
    )


def call_slack(client: WebClient, method_name: str, **kwargs: Any) -> dict[str, Any]:
    method = getattr(client, method_name)

    while True:
        try:
            return method(**kwargs)
        except SlackApiError as exc:
            response = exc.response
            status = getattr(response, "status_code", None)
            error_code = response.get("error") if response is not None else None

            if status == 429 or error_code == "ratelimited":
                retry_after = 1
                if response is not None:
                    retry_after = int(response.headers.get("Retry-After", "1"))
                print(f"[rate-limit] {method_name}: sleeping {retry_after}s")
                time.sleep(retry_after)
                continue

            if status == 401 or error_code == "missing_scope":
                raise MissingScopeError(f"Slack API scope/auth error on {method_name}: {error_code}") from exc

            raise


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENTS_ROOT.mkdir(parents=True, exist_ok=True)
    USER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)


class UserResolver:
    def __init__(self, client: WebClient, cache_path: Path) -> None:
        self.client = client
        self.cache_path = cache_path
        self.cache: dict[str, str] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.cache = {str(k): str(v) for k, v in data.items()}
            except Exception:
                self.cache = {}

    def save(self) -> None:
        if not self._dirty:
            return
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._dirty = False

    def get_name(self, user_id: str | None) -> str:
        if not user_id:
            return "unknown"
        if user_id in self.cache:
            return self.cache[user_id]

        resp = call_slack(self.client, "users_info", user=user_id)
        user = resp.get("user", {})
        profile = user.get("profile", {}) if isinstance(user, dict) else {}

        name = (
            profile.get("real_name_normalized")
            or profile.get("real_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
        self.cache[user_id] = str(name)
        self._dirty = True
        return self.cache[user_id]


def preflight_scopes(client: WebClient) -> tuple[str, str]:
    # 채널 읽기/히스토리
    info = call_slack(client, "conversations_info", channel=CHANNEL_ID)
    channel = info.get("channel", {}) if isinstance(info, dict) else {}
    channel_name = channel.get("name") or CHANNEL_NAME_FALLBACK

    call_slack(client, "conversations_history", channel=CHANNEL_ID, limit=1)

    # users:read
    auth = call_slack(client, "auth_test")
    bot_user_id = auth.get("user_id")
    if bot_user_id:
        call_slack(client, "users_info", user=bot_user_id)

    # files:read preflight (missing_scope 검출용)
    try:
        call_slack(client, "files_list", count=1, channel=CHANNEL_ID)
    except SlackApiError as exc:
        status = getattr(exc.response, "status_code", None)
        error_code = exc.response.get("error") if exc.response else None
        if status == 401 or error_code == "missing_scope":
            raise MissingScopeError("Slack API scope/auth error on files_list") from exc
        # 일부 워크스페이스에서 files.list 제한이 있을 수 있어 missing_scope가 아니면 무시

    return bot_user_id or "", str(channel_name)


def collect_parent_messages(client: WebClient) -> list[dict[str, Any]]:
    parents: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    tqdm = get_tqdm()

    if tqdm:
        page_bar = tqdm(desc="history pages", unit="page")
    else:
        page_bar = None

    while True:
        resp = call_slack(
            client,
            "conversations_history",
            channel=CHANNEL_ID,
            limit=200,
            cursor=cursor,
        )
        messages = resp.get("messages", []) or []

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            reply_count = int(msg.get("reply_count") or 0)
            has_replies = bool(msg.get("has_replies"))
            if not (reply_count > 0 or has_replies):
                continue

            thread_ts = str(msg.get("thread_ts") or msg.get("ts") or "").strip()
            ts = str(msg.get("ts") or "").strip()
            if not thread_ts or ts != thread_ts:
                continue

            # thread root는 bot/app 질문도 가능하므로 여기서는 제외하지 않음
            parents[thread_ts] = msg

        cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip() or None

        if page_bar is not None:
            page_bar.update(1)
        if not cursor:
            break

    if page_bar is not None:
        page_bar.close()

    return sorted(parents.values(), key=lambda x: float(x.get("ts", 0.0)))


def collect_thread_messages(client: WebClient, thread_ts: str) -> list[dict[str, Any]]:
    cursor: str | None = None
    all_messages: list[dict[str, Any]] = []

    while True:
        resp = call_slack(
            client,
            "conversations_replies",
            channel=CHANNEL_ID,
            ts=thread_ts,
            limit=200,
            cursor=cursor,
        )
        messages = resp.get("messages", []) or []
        for msg in messages:
            if isinstance(msg, dict):
                all_messages.append(msg)

        cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip() or None
        if not cursor:
            break

    return sorted(all_messages, key=lambda m: float(m.get("ts", 0.0)))


def safe_relative(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except Exception:
        return path.as_posix()


def pick_attachment_path(thread_ts: str, file_name: str, file_id: str | None) -> Path:
    thread_dir = ATTACHMENTS_ROOT / thread_ts
    thread_dir.mkdir(parents=True, exist_ok=True)

    base_name = sanitize_filename(file_name, default_stem=file_id or "file")
    candidate = thread_dir / base_name
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    if file_id:
        with_id = thread_dir / f"{stem}__{sanitize_filename(file_id)}{suffix}"
        if not with_id.exists():
            return with_id

    i = 1
    while True:
        alt = thread_dir / f"{stem}__{i}{suffix}"
        if not alt.exists():
            return alt
        i += 1


def download_slack_file(url: str, bot_token: str, dest: Path) -> bool:
    if dest.exists():
        return False

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {bot_token}")

    attempts = 0
    while attempts < 4:
        attempts += 1
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status = getattr(resp, "status", 200)
                if status == 401:
                    raise MissingScopeError("Slack file download unauthorized (401)")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as f:
                    shutil.copyfileobj(resp, f)
                return True

        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = int(exc.headers.get("Retry-After", "1"))
                print(f"[rate-limit] file download sleeping {retry_after}s")
                time.sleep(retry_after)
                continue

            body = ""
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""

            if exc.code in (401, 403) and "missing_scope" in body.lower():
                raise MissingScopeError("Slack file download missing_scope") from exc
            if exc.code in (401,):
                raise MissingScopeError("Slack file download unauthorized") from exc

            if exc.code >= 500 and attempts < 4:
                time.sleep(1.0 * attempts)
                continue

            raise RuntimeError(f"download failed ({exc.code}): {dest.name}") from exc

        except urllib.error.URLError as exc:
            if attempts < 4:
                time.sleep(1.0 * attempts)
                continue
            raise RuntimeError(f"download network error: {dest.name}") from exc

    raise RuntimeError(f"download failed after retries: {dest.name}")


def read_text_file(path: Path) -> tuple[str | None, str | None]:
    for enc in ("utf-8", "utf-8-sig", "cp949", "euc-kr", "latin-1"):
        try:
            return path.read_text(encoding=enc), None
        except Exception:
            continue
    return None, "unable to decode text with utf-8/cp949/euc-kr/latin-1"


def extract_pdf(path: Path) -> ExtractResult:
    pypdf_mod = optional_import("pypdf", "pypdf")
    if pypdf_mod is not None:
        try:
            reader = pypdf_mod.PdfReader(str(path))
            parts: list[str] = []
            for page in reader.pages:
                txt = page.extract_text() or ""
                txt = txt.strip()
                if txt:
                    parts.append(txt)
            if parts:
                return ExtractResult(True, "pdf", "\n\n".join(parts))
        except Exception as exc:
            last_error = f"pypdf failed: {exc}"
        else:
            last_error = "pypdf returned no text"
    else:
        last_error = "pypdf unavailable"

    pdfplumber_mod = optional_import("pdfplumber", "pdfplumber")
    if pdfplumber_mod is not None:
        try:
            parts = []
            with pdfplumber_mod.open(str(path)) as pdf:
                for page in pdf.pages:
                    txt = (page.extract_text() or "").strip()
                    if txt:
                        parts.append(txt)
            if parts:
                return ExtractResult(True, "pdf", "\n\n".join(parts))
            return ExtractResult(False, "pdf", "", "pdfplumber returned no text")
        except Exception as exc:
            return ExtractResult(False, "pdf", "", f"{last_error}; pdfplumber failed: {exc}")

    return ExtractResult(False, "skipped", "", f"{last_error}; install pdfplumber")


def extract_docx(path: Path) -> ExtractResult:
    docx_mod = optional_import("docx", "python-docx")
    if docx_mod is None:
        return ExtractResult(False, "skipped", "", "install python-docx")
    try:
        doc = docx_mod.Document(str(path))
        lines = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
        if lines:
            return ExtractResult(True, "docx", "\n".join(lines))
        return ExtractResult(False, "docx", "", "no text found in docx")
    except Exception as exc:
        return ExtractResult(False, "docx", "", str(exc))


def extract_doc(path: Path) -> ExtractResult:
    antiword = shutil.which("antiword")
    if antiword:
        try:
            proc = subprocess.run(
                [antiword, str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            out = (proc.stdout or "").strip()
            if proc.returncode == 0 and out:
                return ExtractResult(True, "text", out)
            antiword_err = (proc.stderr or "").strip() or f"antiword exit {proc.returncode}"
        except Exception as exc:
            antiword_err = f"antiword failed: {exc}"
    else:
        antiword_err = "antiword not installed"

    textract_mod = optional_import("textract", "textract")
    if textract_mod is not None:
        try:
            raw = textract_mod.process(str(path))
            text = raw.decode("utf-8", errors="ignore").strip()
            if text:
                return ExtractResult(True, "text", text)
            return ExtractResult(False, "text", "", "textract returned empty text")
        except Exception as exc:
            return ExtractResult(False, "skipped", "", f"{antiword_err}; textract failed: {exc}")

    return ExtractResult(False, "skipped", "", f"{antiword_err}; install textract")


def extract_hwpx(path: Path) -> ExtractResult:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = sorted(
                [n for n in zf.namelist() if n.startswith("Contents/") and n.endswith(".xml")]
            )
            section_names = [n for n in names if "section" in n.lower()]
            targets = section_names or names
            texts: list[str] = []
            for name in targets:
                raw = zf.read(name)
                try:
                    root = ET.fromstring(raw)
                except Exception:
                    continue
                for elem in root.iter():
                    tag = elem.tag
                    if isinstance(tag, str) and tag.endswith("}t"):
                        if elem.text and elem.text.strip():
                            texts.append(elem.text.strip())
            if texts:
                return ExtractResult(True, "hwpx", "\n".join(texts))
            return ExtractResult(False, "hwpx", "", "no text nodes found in HWPX XML")
    except Exception as exc:
        return ExtractResult(False, "hwpx", "", str(exc))


def extract_hwp(path: Path) -> ExtractResult:
    olefile_mod = optional_import("olefile", "olefile")
    if olefile_mod is None:
        return ExtractResult(False, "skipped", "", "install olefile")

    try:
        if not olefile_mod.isOleFile(str(path)):
            return ExtractResult(False, "hwp", "", "not an OLE compound file")

        ole = olefile_mod.OleFileIO(str(path))
        try:
            streams = ole.listdir(streams=True, storages=False)
            prv_stream = None
            for entry in streams:
                joined = "/".join(entry)
                if joined.lower().endswith("prvtext"):
                    prv_stream = entry
                    break
            if prv_stream is None:
                return ExtractResult(False, "hwp", "", "PrvText stream not found")

            raw = ole.openstream(prv_stream).read()
            for enc in ("utf-16", "utf-16-le", "cp949"):
                try:
                    txt = raw.decode(enc, errors="ignore").replace("\x00", "").strip()
                    if txt:
                        return ExtractResult(True, "hwp", txt)
                except Exception:
                    continue
            return ExtractResult(False, "hwp", "", "failed to decode PrvText")
        finally:
            ole.close()
    except Exception as exc:
        return ExtractResult(False, "hwp", "", str(exc))


def extract_xlsx(path: Path) -> ExtractResult:
    openpyxl_mod = optional_import("openpyxl", "openpyxl")
    if openpyxl_mod is None:
        return ExtractResult(False, "skipped", "", "install openpyxl")
    try:
        wb = openpyxl_mod.load_workbook(str(path), data_only=True, read_only=True)
        lines: list[str] = []
        for ws in wb.worksheets:
            lines.append(f"[Sheet: {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                values = [str(v).strip() for v in row if v is not None and str(v).strip()]
                if values:
                    lines.append(" | ".join(values))
        wb.close()
        if lines:
            return ExtractResult(True, "text", "\n".join(lines))
        return ExtractResult(False, "text", "", "xlsx contained no readable cells")
    except Exception as exc:
        return ExtractResult(False, "text", "", str(exc))


def extract_pptx(path: Path) -> ExtractResult:
    pptx_mod = optional_import("pptx", "python-pptx")
    if pptx_mod is None:
        return ExtractResult(False, "skipped", "", "install python-pptx")
    try:
        prs = pptx_mod.Presentation(str(path))
        lines: list[str] = []
        for i, slide in enumerate(prs.slides, start=1):
            slide_texts = []
            for shape in slide.shapes:
                text = getattr(shape, "text", None)
                if text and text.strip():
                    slide_texts.append(text.strip())
            if slide_texts:
                lines.append(f"[Slide {i}]")
                lines.extend(slide_texts)
        if lines:
            return ExtractResult(True, "text", "\n".join(lines))
        return ExtractResult(False, "text", "", "pptx contained no readable text")
    except Exception as exc:
        return ExtractResult(False, "text", "", str(exc))


def get_gemini_ocr_client() -> Any | None:
    try:
        from law_rag_core.ai import GeminiService  # type: ignore

        svc = GeminiService()
        return svc if svc and svc.client else None
    except Exception:
        return None


def extract_image_with_gemini(path: Path, gemini_service: Any | None, mime: str | None) -> ExtractResult:
    if gemini_service is None:
        return ExtractResult(False, "skipped", "", "Gemini client unavailable or GEMINI_API_KEY missing")

    genai_types = optional_import("google.genai.types", "google-genai")
    if genai_types is None:
        return ExtractResult(False, "skipped", "", "install google-genai")

    mime_type = mime or mimetypes.guess_type(path.name)[0] or "image/png"

    try:
        image_bytes = path.read_bytes()
        prompt = (
            "Extract all readable text from this image exactly as seen. "
            "Return plain text only, preserving line breaks where possible."
        )
        response = gemini_service.client.models.generate_content(
            model=gemini_service.settings.gemini_model,
            contents=[
                prompt,
                genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ],
        )
        text = (getattr(response, "text", "") or "").strip()
        if text:
            return ExtractResult(True, "image-ocr", text)
        return ExtractResult(False, "image-ocr", "", "Gemini returned empty OCR text")
    except Exception as exc:
        return ExtractResult(False, "image-ocr", "", str(exc))


def extract_attachment(path: Path, mimetype: str | None, gemini_service: Any | None) -> ExtractResult:
    ext = path.suffix.lower()

    text_exts = {".txt", ".md", ".csv", ".json", ".tsv", ".log", ".yaml", ".yml", ".xml"}
    image_exts = {".png", ".jpg", ".jpeg", ".webp"}

    if ext in text_exts:
        txt, err = read_text_file(path)
        if txt is not None:
            return ExtractResult(True, "text", txt)
        return ExtractResult(False, "text", "", err or "text decode failed")

    if ext == ".pdf":
        return extract_pdf(path)
    if ext == ".docx":
        return extract_docx(path)
    if ext == ".doc":
        return extract_doc(path)
    if ext == ".hwpx":
        return extract_hwpx(path)
    if ext == ".hwp":
        return extract_hwp(path)
    if ext == ".xlsx":
        return extract_xlsx(path)
    if ext == ".pptx":
        return extract_pptx(path)
    if ext in image_exts:
        return extract_image_with_gemini(path, gemini_service, mimetype)

    return ExtractResult(False, "skipped", "", f"unsupported extension: {ext or '(none)'}")


def build_frontmatter(
    thread_ts: str,
    permalink: str,
    asked_by: str,
    asked_at_iso: str,
    answered_by: list[str],
    reply_count: int,
    attachments_meta: list[dict[str, Any]],
    channel_name: str,
) -> str:
    lines = [
        "---",
        f'thread_ts: "{thread_ts}"',
        f'channel_id: "{CHANNEL_ID}"',
        f'channel_name: "{channel_name}"',
        f'permalink: "{permalink}"',
        f'asked_by: "{asked_by}"',
        f'asked_at: "{asked_at_iso}"',
        f"answered_by: {json.dumps(answered_by, ensure_ascii=False)}",
        f"reply_count: {reply_count}",
        "attachments:",
    ]

    if attachments_meta:
        for a in attachments_meta:
            lines.extend(
                [
                    f'  - kind: "{a["kind"]}"',
                    f'    filename: "{str(a["filename"]).replace("\"", "\\\"")}"',
                    f'    mimetype: "{str(a.get("mimetype") or "").replace("\"", "\\\"")}"',
                    f'    local_path: "{str(a["local_path"]).replace("\"", "\\\"")}"',
                    f"    extracted: {str(bool(a.get('extracted'))).lower()}",
                    f'    extract_method: "{a.get("extract_method") or "skipped"}"',
                ]
            )
    else:
        lines.append("  []")

    lines.append("---")
    return "\n".join(lines)


def render_markdown(
    frontmatter: str,
    question_text: str,
    question_attachment_sections: list[str],
    answer_sections: list[str],
    original_paths: list[str],
    permalink: str,
) -> str:
    parts: list[str] = [frontmatter, "", "# 질문", "", question_text.strip() or "(질문 본문 없음)"]

    if question_attachment_sections:
        parts.extend([
            "",
            "## 첨부 파일 내용 (질문)",
            "",
            *question_attachment_sections,
        ])

    parts.extend(["", "---", "", "# 답변", ""])

    if answer_sections:
        parts.extend(answer_sections)
    else:
        parts.append("(답변 본문 없음)")

    parts.extend(["", "---", "", "# 원본", "", f"- Slack: {permalink}", "- 첨부 파일 로컬 경로:"])

    if original_paths:
        for p in original_paths:
            parts.append(f"  - {p}")
    else:
        parts.append("  - (없음)")

    return "\n".join(parts).strip() + "\n"


def process_files_for_message(
    *,
    files: Iterable[dict[str, Any]],
    kind: str,
    thread_ts: str,
    bot_token: str,
    gemini_service: Any | None,
    stats: Stats,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    metas: list[dict[str, Any]] = []
    rendered_sections: list[str] = []
    local_paths: list[str] = []

    for f in files:
        if not isinstance(f, dict):
            continue
        file_id = str(f.get("id") or "") or None
        raw_name = str(f.get("name") or "")
        mimetype = str(f.get("mimetype") or "") or None
        url = str(f.get("url_private_download") or f.get("url_private") or "").strip()

        if not raw_name:
            raw_name = file_id or "attachment"

        local_path = pick_attachment_path(thread_ts, raw_name, file_id)

        if url:
            downloaded_now = download_slack_file(url, bot_token, local_path)
            if downloaded_now:
                stats.downloaded_attachments += 1
            else:
                stats.skipped_existing_attachments += 1
        else:
            stats.skipped_existing_attachments += 1

        extract = extract_attachment(local_path, mimetype, gemini_service)
        if extract.extracted:
            stats.extract_success += 1
        else:
            stats.extract_failed += 1

        rel = safe_relative(local_path)
        local_paths.append(rel)

        metas.append(
            {
                "kind": kind,
                "filename": raw_name,
                "mimetype": mimetype or "",
                "local_path": rel,
                "extracted": extract.extracted,
                "extract_method": extract.method,
            }
        )

        if extract.extracted:
            body = extract.text.strip() or "(추출 텍스트 없음)"
        else:
            reason = extract.error or "unknown"
            body = f"> 추출 실패: {reason}. 원본 경로: {rel}"

        rendered_sections.append(f"### {raw_name}\n{body}" if kind == "question" else f"#### {raw_name}\n{body}")

    return metas, rendered_sections, local_paths


def process_thread(
    *,
    client: WebClient,
    parent_msg: dict[str, Any],
    bot_token: str,
    channel_name: str,
    user_resolver: UserResolver,
    gemini_service: Any | None,
    stats: Stats,
) -> None:
    thread_ts = str(parent_msg.get("ts") or "").strip()
    if not thread_ts:
        return

    asked_date = ts_to_kst_date(thread_ts)
    md_path = OUTPUT_DIR / f"{asked_date}_{thread_ts}.md"
    if md_path.exists():
        stats.skipped_existing_md += 1
        return

    msgs = collect_thread_messages(client, thread_ts)
    if not msgs:
        return

    parent = None
    for m in msgs:
        if str(m.get("ts") or "") == thread_ts:
            parent = m
            break
    if parent is None:
        parent = msgs[0]

    asker_id = str(parent.get("user") or "").strip() or None
    asked_by = user_resolver.get_name(asker_id) if asker_id else "unknown"
    asked_at_iso = ts_to_kst_iso(parent.get("ts") or thread_ts)

    permalink_resp = call_slack(client, "chat_getPermalink", channel=CHANNEL_ID, message_ts=thread_ts)
    permalink = str(permalink_resp.get("permalink") or "")

    question_parts: list[str] = []
    parent_text = clean_text(parent.get("text"))
    if parent_text:
        question_parts.append(parent_text)

    attachments_meta: list[dict[str, Any]] = []
    question_att_sections: list[str] = []
    answer_blocks: list[str] = []
    all_local_paths: list[str] = []

    # parent 첨부파일 처리
    q_meta, q_sections, q_paths = process_files_for_message(
        files=parent.get("files", []) or [],
        kind="question",
        thread_ts=thread_ts,
        bot_token=bot_token,
        gemini_service=gemini_service,
        stats=stats,
    )
    attachments_meta.extend(q_meta)
    question_att_sections.extend(q_sections)
    all_local_paths.extend(q_paths)

    answered_by_set: set[str] = set()
    answer_message_count = 0

    for msg in msgs:
        ts = str(msg.get("ts") or "")
        if ts == thread_ts:
            continue
        if is_bot_or_app_message(msg):
            continue

        user_id = str(msg.get("user") or "").strip() or None
        text = clean_text(msg.get("text"))
        iso_time = ts_to_kst_iso(ts) if ts else ""

        if user_id and asker_id and user_id == asker_id:
            # 질문자의 추가 설명은 질문 섹션으로 병합
            if text:
                question_parts.append(f"\n[질문 추가 — {iso_time}]\n{text}")

            q_meta2, q_sections2, q_paths2 = process_files_for_message(
                files=msg.get("files", []) or [],
                kind="question",
                thread_ts=thread_ts,
                bot_token=bot_token,
                gemini_service=gemini_service,
                stats=stats,
            )
            attachments_meta.extend(q_meta2)
            question_att_sections.extend(q_sections2)
            all_local_paths.extend(q_paths2)
            continue

        # 질문자 외 유저 = 답변자
        answer_name = user_resolver.get_name(user_id) if user_id else "unknown"
        if answer_name and answer_name != asked_by:
            answered_by_set.add(answer_name)

        answer_message_count += 1
        block_lines = [f"## {answer_name} — {iso_time}"]
        block_lines.append(text or "(답변 본문 없음)")

        a_meta, a_sections, a_paths = process_files_for_message(
            files=msg.get("files", []) or [],
            kind="answer",
            thread_ts=thread_ts,
            bot_token=bot_token,
            gemini_service=gemini_service,
            stats=stats,
        )
        attachments_meta.extend(a_meta)
        all_local_paths.extend(a_paths)

        if a_sections:
            block_lines.append("")
            block_lines.append("### 첨부 파일 내용 (답변)")
            block_lines.extend(a_sections)

        block_lines.append("")
        block_lines.append("---")
        answer_blocks.append("\n".join(block_lines))

    answered_by = sorted(answered_by_set)

    frontmatter = build_frontmatter(
        thread_ts=thread_ts,
        permalink=permalink,
        asked_by=asked_by,
        asked_at_iso=asked_at_iso,
        answered_by=answered_by,
        reply_count=answer_message_count,
        attachments_meta=attachments_meta,
        channel_name=channel_name,
    )

    markdown = render_markdown(
        frontmatter=frontmatter,
        question_text="\n\n".join([p for p in question_parts if p.strip()]),
        question_attachment_sections=question_att_sections,
        answer_sections=answer_blocks,
        original_paths=sorted(dict.fromkeys(all_local_paths)),
        permalink=permalink,
    )

    md_path.write_text(markdown, encoding="utf-8")
    stats.created_md += 1


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    ensure_dirs()

    user_token = os.getenv("SLACK_TOKEN", "").strip()
    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    app_token = os.getenv("SLACK_APP_TOKEN", "").strip()

    token = user_token or bot_token
    token_kind = "user" if user_token else ("bot" if bot_token else None)

    if not token:
        print("[error] SLACK_TOKEN (xoxp-) 또는 SLACK_BOT_TOKEN (xoxb-) 중 하나가 .env에 필요합니다.")
        return 1

    print(f"[info] using {token_kind} token ({'xoxp-' if token_kind == 'user' else 'xoxb-'}...)")
    if token_kind == "bot" and not app_token:
        print("[warn] SLACK_APP_TOKEN is missing in .env (not required for Web API export)")

    client = WebClient(token=token)
    user_resolver = UserResolver(client, USER_CACHE_PATH)
    gemini_service = get_gemini_ocr_client()

    stats = Stats()

    try:
        _, channel_name = preflight_scopes(client)

        parents = collect_parent_messages(client)
        stats.total_threads = len(parents)
        print(f"[info] candidate threads with replies: {stats.total_threads}")

        tqdm = get_tqdm()
        if tqdm:
            iterator = tqdm(parents, desc="threads", unit="thread")
        else:
            iterator = parents

        for parent in iterator:
            process_thread(
                client=client,
                parent_msg=parent,
                bot_token=token,
                channel_name=channel_name,
                user_resolver=user_resolver,
                gemini_service=gemini_service,
                stats=stats,
            )

    except MissingScopeError as exc:
        print("[error] Slack 토큰 scope/auth 문제로 중단합니다.")
        print(f"[error] detail: {exc}")
        print("[required_scopes] " + ", ".join(REQUIRED_SCOPES))
        user_resolver.save()
        return 2
    except Exception as exc:
        print(f"[error] export failed: {exc}")
        user_resolver.save()
        return 1

    user_resolver.save()

    print("\n=== Export Summary ===")
    print(f"total_threads: {stats.total_threads}")
    print(f"created_md: {stats.created_md}")
    print(f"skipped_existing_md: {stats.skipped_existing_md}")
    print(f"downloaded_attachments: {stats.downloaded_attachments}")
    print(f"skipped_existing_attachments: {stats.skipped_existing_attachments}")
    print(f"extract_success: {stats.extract_success}")
    print(f"extract_failed: {stats.extract_failed}")
    print(f"output_dir: {safe_relative(OUTPUT_DIR)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
