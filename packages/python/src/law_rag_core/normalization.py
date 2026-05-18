from __future__ import annotations

import hashlib
import re
from typing import Iterable


WHITESPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^0-9A-Za-z가-힣]+")


def compact_whitespace(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def normalize_for_search(text: str) -> str:
    lowered = compact_whitespace(text).lower()
    lowered = NON_WORD_RE.sub(" ", lowered)
    return compact_whitespace(lowered)


def hash_text(parts: Iterable[str]) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
    return hasher.hexdigest()


def estimate_token_count(text: str) -> int:
    if not text.strip():
        return 0
    return max(1, len(compact_whitespace(text).split()))

