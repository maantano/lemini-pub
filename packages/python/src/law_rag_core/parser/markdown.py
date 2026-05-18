from __future__ import annotations

from datetime import date
import re
from pathlib import Path
from typing import Any

from ..normalization import compact_whitespace, estimate_token_count, hash_text, normalize_for_search
from ..types import IngestedLaw, LawChunkRecord, LawDocumentRecord
from .front_matter import parse_front_matter


ARTICLE_HEADER_RE = re.compile(
    r"^(?:#+\s*)?(제\s*(?P<number>\d+조(?:의\d+)?)\s*(?:\((?P<title>[^)]*)\))?)\s*$"
)
APPENDIX_RE = re.compile(r"^(?:#+\s*)?부칙")
HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "law_title", "제목"),
    "law_id": ("law_id", "lawId", "법령ID"),
    "law_mst": ("law_mst", "lawMst", "법령MST"),
    "law_type": ("law_type", "lawType", "법령구분"),
    "ministry": ("ministry", "소관부처"),
    "promulgation_date": ("promulgation_date", "공포일자"),
    "effective_date": ("effective_date", "시행일자"),
    "status": ("status", "상태"),
    "source_url": ("source_url", "출처"),
    "aliases": ("aliases",),
}


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _build_document(metadata: dict[str, Any], body: str) -> LawDocumentRecord:
    def pick(name: str) -> Any:
        for alias in FIELD_ALIASES[name]:
            if alias in metadata and metadata[alias] not in (None, ""):
                return metadata[alias]
        return None

    title = str(pick("title") or "제목 미상")
    law_id = str(pick("law_id") or "") or None
    law_mst = str(pick("law_mst") or "") or None
    content_hash = hash_text([law_id or "", law_mst or "", title, body])
    aliases = pick("aliases") or []
    if isinstance(aliases, str):
        aliases = [alias.strip() for alias in aliases.split(",") if alias.strip()]
    ministry = pick("ministry")

    return LawDocumentRecord(
        law_id=law_id or content_hash[:12],
        law_mst=law_mst,
        title=title,
        law_type=str(pick("law_type") or "") or None,
        ministry=ministry,
        promulgation_date=_parse_date(pick("promulgation_date")),
        effective_date=_parse_date(pick("effective_date")),
        status=str(pick("status") or "") or None,
        source_url=str(pick("source_url") or "") or None,
        aliases=aliases,
        content_hash=content_hash,
    )


def _make_search_text(
    title: str,
    chapter_title: str | None,
    section_title: str | None,
    article_no: str | None,
    article_title: str | None,
    text: str,
    *,
    minimal_mode: bool,
) -> str:
    joined = " ".join(
        part
        for part in [title, chapter_title, section_title, article_no, article_title, text]
        if part
    )
    normalized = normalize_for_search(joined)
    limit = 900 if minimal_mode else 1800
    return normalized[:limit]


def _split_long_article(
    base_chunk: LawChunkRecord,
    title: str,
    *,
    minimal_mode: bool,
    segment_enabled: bool,
) -> list[LawChunkRecord]:
    if minimal_mode or not segment_enabled or base_chunk.token_count < 380:
        return [base_chunk]

    paragraphs = [part.strip() for part in base_chunk.text.split("\n\n") if part.strip()]
    if len(paragraphs) < 2:
        return [base_chunk]

    chunks: list[LawChunkRecord] = []
    buffer: list[str] = []
    order = 0
    for paragraph in paragraphs:
        candidate = "\n\n".join(buffer + [paragraph])
        if estimate_token_count(candidate) > 260 and buffer:
            segment_text = "\n\n".join(buffer)
            chunks.append(
                LawChunkRecord(
                    chunk_type="article_segment",
                    chapter_title=base_chunk.chapter_title,
                    section_title=base_chunk.section_title,
                    article_no=base_chunk.article_no,
                    article_title=base_chunk.article_title,
                    text=segment_text,
                    search_text=_make_search_text(
                        title,
                        base_chunk.chapter_title,
                        base_chunk.section_title,
                        base_chunk.article_no,
                        base_chunk.article_title,
                        segment_text,
                        minimal_mode=minimal_mode,
                    ),
                    order_index=base_chunk.order_index + order,
                    token_count=estimate_token_count(segment_text),
                    content_hash=hash_text(
                        [base_chunk.content_hash, f"segment-{order}", segment_text]
                    ),
                )
            )
            buffer = [paragraph]
            order += 1
            continue
        buffer.append(paragraph)

    if buffer:
        segment_text = "\n\n".join(buffer)
        chunks.append(
            LawChunkRecord(
                chunk_type="article_segment",
                chapter_title=base_chunk.chapter_title,
                section_title=base_chunk.section_title,
                article_no=base_chunk.article_no,
                article_title=base_chunk.article_title,
                text=segment_text,
                search_text=_make_search_text(
                    title,
                    base_chunk.chapter_title,
                    base_chunk.section_title,
                    base_chunk.article_no,
                    base_chunk.article_title,
                    segment_text,
                    minimal_mode=minimal_mode,
                ),
                order_index=base_chunk.order_index + order,
                token_count=estimate_token_count(segment_text),
                content_hash=hash_text([base_chunk.content_hash, f"segment-{order}", segment_text]),
            )
        )
    return chunks


def parse_law_markdown(
    path: Path,
    raw_text: str,
    *,
    minimal_mode: bool = True,
    enable_article_segment: bool = False,
) -> IngestedLaw:
    metadata, body = parse_front_matter(raw_text)
    document = _build_document(metadata, body)

    lines = body.splitlines()
    chapter_title: str | None = None
    section_title: str | None = None
    current_header: str | None = None
    current_article_no: str | None = None
    current_article_title: str | None = None
    current_chunk_type: str | None = None
    buffer: list[str] = []
    order_index = 0
    chunks: list[LawChunkRecord] = []

    def flush_buffer() -> None:
        nonlocal buffer, order_index
        if not buffer or not current_chunk_type:
            buffer = []
            return
        text = compact_whitespace("\n".join(buffer))
        if not text:
            buffer = []
            return
        full_text = text if current_header is None else compact_whitespace(f"{current_header}\n{text}")
        base_chunk = LawChunkRecord(
            chunk_type=current_chunk_type,
            chapter_title=chapter_title,
            section_title=section_title,
            article_no=current_article_no,
            article_title=current_article_title,
            text=full_text,
            search_text=_make_search_text(
                document.title,
                chapter_title,
                section_title,
                current_article_no,
                current_article_title,
                full_text,
                minimal_mode=minimal_mode,
            ),
            order_index=order_index,
            token_count=estimate_token_count(full_text),
            content_hash=hash_text(
                [
                    document.law_id,
                    current_chunk_type,
                    str(order_index),
                    current_article_no or "",
                    full_text,
                ]
            ),
        )
        split_chunks = _split_long_article(
            base_chunk,
            document.title,
            minimal_mode=minimal_mode,
            segment_enabled=enable_article_segment,
        )
        chunks.extend(split_chunks)
        order_index += max(1, len(split_chunks))
        buffer = []

    for line in lines:
        stripped = line.strip()
        heading_match = HEADING_RE.match(line)
        if heading_match:
            heading_title = heading_match.group("title").strip()
            article_match = ARTICLE_HEADER_RE.match(heading_title)
            if article_match:
                flush_buffer()
                current_chunk_type = "article"
                current_header = heading_title
                current_article_no = article_match.group("number").replace(" ", "")
                current_article_title = article_match.group("title")
                buffer = []
                continue
            if APPENDIX_RE.match(heading_title):
                flush_buffer()
                current_chunk_type = "appendix"
                chapter_title = "부칙"
                section_title = None
                current_header = heading_title
                current_article_no = None
                current_article_title = None
                buffer = []
                continue
            flush_buffer()
            marks = heading_match.group("marks")
            if len(marks) == 1:
                chapter_title = None
                section_title = None
            elif len(marks) == 2:
                chapter_title = heading_title
                section_title = None
            else:
                section_title = heading_title
            current_header = None
            current_chunk_type = None
            current_article_no = None
            current_article_title = None
            continue

        inline_article_match = ARTICLE_HEADER_RE.match(stripped)
        if inline_article_match:
            flush_buffer()
            current_chunk_type = "article"
            current_header = stripped
            current_article_no = inline_article_match.group("number").replace(" ", "")
            current_article_title = inline_article_match.group("title")
            buffer = []
            continue

        if APPENDIX_RE.match(stripped):
            flush_buffer()
            current_chunk_type = "appendix"
            chapter_title = "부칙"
            section_title = None
            current_header = stripped
            current_article_no = None
            current_article_title = None
            buffer = []
            continue

        if current_chunk_type:
            buffer.append(line)

    flush_buffer()

    if not chunks:
        fallback_text = compact_whitespace(body)
        chunks.append(
            LawChunkRecord(
                chunk_type="document",
                text=fallback_text,
                search_text=_make_search_text(
                    document.title,
                    None,
                    None,
                    None,
                    None,
                    fallback_text,
                    minimal_mode=minimal_mode,
                ),
                order_index=0,
                token_count=estimate_token_count(fallback_text),
                content_hash=hash_text([str(path), fallback_text]),
            )
        )

    return IngestedLaw(document=document, chunks=chunks)
