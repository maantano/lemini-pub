"""Validate all law Markdown files for consistency.

Checks:
- Valid YAML frontmatter with required fields
- 소관부처 is a YAML list
- Unicode dot normalization consistency
- 법령구분 vs 파일명(시행령/시행규칙) 일관성

Usage:
    python -m worker.sync.laws.validate

Adapted from legalize-pipeline (MIT/Apache-2.0). See sync/NOTICES.md.
원본의 metadata.json 교차검증은 제거 (우리 파이프라인은 metadata.json을 생성하지 않음).
"""

import logging
import sys
from pathlib import Path

import yaml

from .config import CHILD_SUFFIXES, KR_DIR, WORKSPACE_ROOT
from .converter import normalize_law_name

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = ["제목", "법령MST", "법령구분", "법령구분코드", "소관부처", "공포일자", "상태"]


def validate_frontmatter(file_path: Path) -> list[str]:
    """Validate a single law file. Returns list of error messages."""
    errors = []
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"Cannot read: {e}"]

    if not text.startswith("---"):
        return ["No YAML frontmatter"]

    try:
        end = text.index("---", 3)
    except ValueError:
        return ["Unterminated YAML frontmatter"]

    yaml_str = text[3:end]
    try:
        fm = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        return [f"Invalid YAML: {e}"]

    if not isinstance(fm, dict):
        return ["Frontmatter is not a dict"]

    for field in REQUIRED_FIELDS:
        if field not in fm:
            errors.append(f"Missing required field: {field}")

    dept = fm.get("소관부처")
    if dept is not None and not isinstance(dept, list):
        errors.append(f"소관부처 must be a YAML list, got {type(dept).__name__}")

    title = fm.get("제목", "")
    if title != normalize_law_name(title):
        errors.append(f"제목 contains un-normalized Unicode dots: {title}")

    # Cross-validate suffix-based grouping against 법령구분
    law_type = fm.get("법령구분", "")
    normalized_title = normalize_law_name(title)
    for suffix, _ in CHILD_SUFFIXES:
        if normalized_title.endswith(suffix):
            if suffix == " 시행령" and law_type not in ("대통령령", ""):
                errors.append(
                    f"이름이 '{suffix}'로 끝나지만 법령구분이 '{law_type}' "
                    f"(예상: 대통령령)"
                )
            if suffix == " 시행규칙" and law_type != "" and not law_type.endswith("총리령") and not law_type.endswith("부령") and not law_type.endswith("규칙"):
                errors.append(
                    f"이름이 '{suffix}'로 끝나지만 법령구분이 '{law_type}' "
                    f"(예상: 총리령 또는 부령)"
                )
            break

    return errors


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    total_errors = 0
    files_checked = 0

    if not KR_DIR.exists():
        logger.error(f"KR_DIR not found: {KR_DIR}")
        sys.exit(1)

    for md_file in sorted(KR_DIR.rglob("*.md")):
        errors = validate_frontmatter(md_file)
        files_checked += 1
        if errors:
            rel_path = md_file.relative_to(WORKSPACE_ROOT)
            for err in errors:
                logger.error(f"{rel_path}: {err}")
            total_errors += len(errors)

    logger.info(f"Checked {files_checked} files, found {total_errors} errors")

    if total_errors > 0:
        sys.exit(1)
    else:
        logger.info("All validations passed")
        sys.exit(0)


if __name__ == "__main__":
    main()
