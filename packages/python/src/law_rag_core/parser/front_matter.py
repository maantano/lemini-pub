from __future__ import annotations

import json
from typing import Any


def _parse_scalar(value: str) -> Any:
    stripped = value.strip()
    if not stripped:
        return ""
    if (stripped.startswith("'") and stripped.endswith("'")) or (
        stripped.startswith('"') and stripped.endswith('"')
    ):
        stripped = stripped[1:-1]
    if stripped in {"''", '""'}:
        return ""
    if stripped.lower() in {"true", "false"}:
        return stripped.lower() == "true"
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
    return stripped


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text

    fence = text.find("\n---\n", 4)
    if fence == -1:
        return {}, text

    raw_meta = text[4:fence]
    body = text[fence + 5 :]
    metadata: dict[str, Any] = {}
    current_key: str | None = None

    for raw_line in raw_meta.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key:
            metadata.setdefault(current_key, [])
            metadata[current_key].append(_parse_scalar(line[4:]))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if value == "":
            metadata[key] = []
        else:
            metadata[key] = _parse_scalar(value)

    return metadata, body
