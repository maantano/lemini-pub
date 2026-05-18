"""행정규칙 상세 → 마크다운 + YAML front-matter.

기존 laws/converter 와 동일한 `law_rag_core.parser` 파서 호환 포맷을 생성.
admrul 은 조문 구조가 명시적이지 않아 본문을 한 chunk 로 그대로 담는 전략.

front-matter:
  제목, 법령MST(행정규칙일련번호), 법령ID, 법령구분(규칙종류), 소관부처,
  공포일자(발령일자), 시행일자, 상태, 출처
  document_type='administrative_rule', license_policy='kogl_type1',
  citation_mode='full', issuer(부처명)
"""

from __future__ import annotations

import datetime
import re

import yaml

from ..laws.converter import _LawDumper, _QuotedStr, _to_date, normalize_law_name


def _format_compact_date(s: str) -> str:
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _safe_segment(name: str) -> str:
    """파일/디렉토리에 안전한 문자열. 영문/숫자/한글/하이픈만 남김."""
    out = re.sub(r"[^\w가-힣\-]", "", name)
    return out[:80] or "unnamed"


def get_admrul_path(rule: dict) -> str:
    """저장 경로: kr/행정규칙/{부처}/{규칙명}.md"""
    org = _safe_segment(rule.get("org_name") or "기타")
    name = normalize_law_name(rule.get("rule_name", "")).replace(" ", "")
    name = _safe_segment(name)
    return f"kr/행정규칙/{org}/{name}.md"


def build_frontmatter(rule: dict) -> dict:
    raw_name = rule.get("rule_name", "")
    normalized = normalize_law_name(raw_name)

    fm = {
        "제목": normalized,
        "법령MST": int(rule["rule_id"]) if rule.get("rule_id", "").isdigit() else rule.get("rule_id", ""),
        "법령ID": _QuotedStr(rule.get("rule_id", "")),
        "법령구분": rule.get("rule_type", ""),
        "법령구분코드": rule.get("rule_type_code", ""),
        "소관부처": [rule["org_name"]] if rule.get("org_name") else [],
        "공포일자": _to_date(_format_compact_date(rule.get("promulgation_date", ""))),
        "공포번호": _QuotedStr(rule.get("promulgation_no", "")),
        "시행일자": _to_date(_format_compact_date(rule.get("effective_date", ""))),
        "법령분야": "",
        # 행정규칙의 "현행여부" 는 'Y'/'N' 이므로 사람이 읽을 수 있게 변환
        "상태": "시행" if rule.get("status", "").upper() in ("Y", "현행", "") else "폐지",
        "출처": f"https://www.law.go.kr/행정규칙/({rule.get('rule_id', '')})",
        "document_type": "administrative_rule",
        "license_policy": "kogl_type1",
        "citation_mode": "full",
        "issuer": rule.get("org_name", ""),
    }

    if normalized != raw_name:
        fm["원본제목"] = raw_name

    return fm


_ARTICLE_RE = re.compile(r"(?m)^\s*제(\d+(?:의\d+)?)조\s*(?:\(([^)]+)\))?")
_CHAPTER_RE = re.compile(r"(?m)^\s*제(\d+)장\s+(.+)$")
_SECTION_RE = re.compile(r"(?m)^\s*제(\d+)절\s+(.+)$")


def _segment_body(body: str) -> str:
    """행정규칙 본문에서 조문 구조를 감지해 ##### 마크다운 헤딩 추가.

    parser.markdown 의 ARTICLE_HEADER_RE 가 `(?:#+\s*)?제N조...` 를 인식하지만,
    같은 줄에 본문이 붙어 있으면 쪼개지 못한다. 따라서 여기서 전처리로
    조문 경계마다 줄바꿈 + 헤딩을 삽입해준다.
    """
    if not body.strip():
        return body

    # 1) 장/절 헤딩
    body = _CHAPTER_RE.sub(lambda m: f"\n\n## 제{m.group(1)}장 {m.group(2).strip()}\n\n", body)
    body = _SECTION_RE.sub(lambda m: f"\n\n### 제{m.group(1)}절 {m.group(2).strip()}\n\n", body)

    # 2) 조문 헤딩: `제N조(제목) 본문...` → `\n##### 제N조 (제목)\n\n본문...`
    def repl(match: re.Match) -> str:
        no = match.group(1)
        title = match.group(2) or ""
        title_part = f" ({title.strip()})" if title else ""
        return f"\n\n##### 제{no}조{title_part}\n\n"

    body = _ARTICLE_RE.sub(repl, body)

    # 3) 연속 빈 줄 정리
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body


def admrul_to_markdown(rule: dict) -> str:
    """행정규칙 상세 → 완전한 마크다운 문서."""
    body = rule.get("body", "")
    if rule.get("body_is_reference_only"):
        # 본문이 placeholder 면 마크다운 생성 안 함 — 호출자가 skip
        raise ValueError(f"body_is_reference_only: {rule.get('rule_id')} {rule.get('rule_name')}")

    frontmatter = build_frontmatter(rule)
    yaml_str = yaml.dump(
        frontmatter,
        Dumper=_LawDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )

    normalized_name = normalize_law_name(rule.get("rule_name", ""))
    body_parts = [f"# {normalized_name}", ""]

    # 조문 구조 감지하여 헤딩 삽입 → parser 가 조문별 chunk 로 분리 가능해짐
    body_segmented = _segment_body(body)
    body_parts.append(body_segmented)
    body_parts.append("")

    # 부칙
    if rule.get("addenda"):
        body_parts.append("## 부칙")
        body_parts.append("")
        body_parts.append(rule["addenda"])
        body_parts.append("")

    # 제개정이유
    if rule.get("reason"):
        body_parts.append("## 제개정이유")
        body_parts.append("")
        body_parts.append(rule["reason"])
        body_parts.append("")

    body_md = "\n".join(body_parts)
    return f"---\n{yaml_str}---\n\n{body_md}\n"
