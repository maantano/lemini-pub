"""인용 근거 결정론 검증 (Verify).

review_document의 최종 JSON에 포함된 basis (법령/판례/행정규칙/자율규약) 가
실제 retrieve 된 청크 원문 또는 DB 에 존재하는지 SQL/regex 로 대조한다.
LLM 호출 0회, 레이턴시 <50ms.

실패한 basis 항목은 drop 하고 `warnings.dropped_basis[]` 에 이유와 함께 기록.
전체 basis 의 grounded 플래그도 계산.

관련 문서: docs/lawyer-grade-final-plan.md §2 변경 7
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from ..settings import get_settings


def _get_db_path() -> Path:
    settings = get_settings()
    return Path(settings.laws_db_path) if hasattr(settings, "laws_db_path") else (
        Path(__file__).resolve().parents[5] / "data" / "artifacts" / "laws.sqlite"
    )


def _normalize(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", "", str(s))


def _chunk_text_hay(retrieved_chunks: list[Any]) -> str:
    parts: list[str] = []
    for c in retrieved_chunks or []:
        t = getattr(c, "text", None) or (c.get("text") if isinstance(c, dict) else "") or ""
        title = getattr(c, "law_title", None) or (c.get("law_title") if isinstance(c, dict) else "") or ""
        art = getattr(c, "article_no", None) or (c.get("article_no") if isinstance(c, dict) else "") or ""
        parts.append(f"{title} {art} {t}")
    return "\n".join(parts)


def verify_basis(
    final_json: dict,
    retrieved_chunks: list[Any],
    db_path: Path | None = None,
) -> tuple[dict, list[dict]]:
    """최종 JSON 의 observations[].issues[].basis 를 검증하고 환각을 drop.

    Returns:
        (final_json 수정본, warnings 리스트)
    """
    warnings: list[dict] = []
    if not isinstance(final_json, dict):
        return final_json, warnings

    hay = _chunk_text_hay(retrieved_chunks)
    hay_norm = _normalize(hay)

    conn: sqlite3.Connection | None = None
    try:
        path = db_path or _get_db_path()
        if path.exists():
            conn = sqlite3.connect(str(path))
    except Exception:
        conn = None

    total_dropped = 0

    def _drop(kind: str, value: Any, reason: str) -> None:
        nonlocal total_dropped
        total_dropped += 1
        warnings.append({
            "kind": kind,
            "value": value,
            "reason": reason,
        })

    def _verify_basis_obj(basis: Any) -> Any:
        """basis dict 를 in-place 검증하여 실패 항목 제거."""
        if not isinstance(basis, dict):
            return basis

        # --- statutes ---
        statutes = basis.get("statutes") or []
        kept_statutes = []
        for s in statutes:
            if not isinstance(s, dict):
                _drop("statute_malformed", s, "dict 가 아님"); continue
            name = s.get("name") or ""
            article = s.get("article") or ""
            if not name and not article:
                _drop("statute_empty", s, "name/article 모두 비어 있음"); continue
            nname = _normalize(name)
            narticle = _normalize(article)
            if nname and nname not in hay_norm:
                _drop("statute_name_not_in_hay", s, f"법령명 '{name}' 이(가) retrieve 원문에 없음"); continue
            if narticle and narticle not in hay_norm:
                _drop("statute_article_not_in_hay", s, f"조문 '{article}' 이(가) retrieve 원문에 없음"); continue
            raw = s.get("raw") or ""
            if raw and _normalize(raw) not in hay_norm:
                # raw 는 강한 요구 — substring 이어야 함
                _drop("statute_raw_not_substring", s, f"raw 인용이 원문의 부분문자열이 아님"); continue
            kept_statutes.append(s)
        basis["statutes"] = kept_statutes

        # --- cases (판례) ---
        # 원칙: 판례는 law.go.kr API 에서 질의 시점에 가져온 evidence 청크에 실존해야 한다.
        #       DB 캐싱 없이 evidence 본문만으로 검증 (API 응답이 신뢰 원천).
        cases = basis.get("cases") or []
        kept_cases = []
        for c in cases:
            if not isinstance(c, dict):
                _drop("case_malformed", c, "dict 가 아님"); continue
            case_no = c.get("case_no") or ""
            if not case_no:
                _drop("case_no_empty", c, "case_no 비어 있음"); continue
            ncase = _normalize(case_no)
            if ncase not in hay_norm:
                _drop("case_not_in_evidence", c,
                      f"사건번호 '{case_no}' 이(가) evidence 에 없음"); continue
            holding = c.get("holding_excerpt") or ""
            if holding and _normalize(holding) not in hay_norm:
                _drop("case_holding_not_substring", c, "holding_excerpt 가 evidence 부분문자열이 아님"); continue
            kept_cases.append(c)
        basis["cases"] = kept_cases

        # --- ordinances (행정규칙·고시) ---
        ordinances = basis.get("ordinances") or []
        kept_ord = []
        for o in ordinances:
            if not isinstance(o, dict):
                _drop("ordinance_malformed", o, "dict 가 아님"); continue
            number = o.get("number") or ""
            title = o.get("title") or ""
            nnumber = _normalize(number)
            ntitle = _normalize(title)
            ok = False
            # 1차: evidence 청크에 제목 또는 번호가 있는가
            if ntitle and ntitle in hay_norm:
                ok = True
            elif nnumber and nnumber in hay_norm:
                ok = True
            # 2차: DB 에 실존하는가 (양방향 매칭)
            if not ok and conn is not None:
                try:
                    if nnumber:
                        variants = {
                            nnumber,
                            nnumber.lstrip("제").rstrip("호"),
                            nnumber.lstrip("제"),
                            nnumber.rstrip("호"),
                        }
                        placeholders = ",".join("?" for _ in variants)
                        row = conn.execute(
                            f"SELECT 1 FROM law_documents "
                            f"WHERE document_type = 'administrative_rule' "
                            f"AND REPLACE(promulgation_no, ' ', '') IN ({placeholders}) LIMIT 1",
                            tuple(variants),
                        ).fetchone()
                        if row:
                            ok = True
                    if not ok and ntitle:
                        row = conn.execute(
                            "SELECT 1 FROM law_documents "
                            "WHERE document_type = 'administrative_rule' "
                            "AND REPLACE(title, ' ', '') LIKE ? LIMIT 1",
                            (f"%{ntitle}%",),
                        ).fetchone()
                        if row:
                            ok = True
                except Exception:
                    pass
            if not ok:
                _drop("ordinance_not_verified", o, f"행정규칙/고시 '{title} {number}' 검증 실패"); continue
            kept_ord.append(o)
        basis["ordinances"] = kept_ord

        # --- voluntary_codes ---
        vcodes = basis.get("voluntary_codes") or []
        kept_v = []
        for v in vcodes:
            if not isinstance(v, dict):
                _drop("voluntary_malformed", v, "dict 가 아님"); continue
            title = v.get("title") or ""
            issuer = v.get("issuer") or ""
            ntitle = _normalize(title)
            nissuer = _normalize(issuer)
            ok = False
            if conn is not None:
                try:
                    row = conn.execute(
                        "SELECT 1 FROM law_documents "
                        "WHERE document_type = 'voluntary_code' "
                        "AND REPLACE(title, ' ', '') LIKE ? LIMIT 1",
                        (f"%{ntitle}%",),
                    ).fetchone()
                    if row:
                        ok = True
                except Exception:
                    pass
            if not ok and ntitle and ntitle in hay_norm:
                ok = True
            if not ok:
                _drop("voluntary_not_verified", v, f"자율규약 '{title}' 검증 실패"); continue
            kept_v.append(v)
        basis["voluntary_codes"] = kept_v

        return basis

    # observations[].issues[].basis 순회
    for obs in (final_json.get("observations") or []):
        issues = obs.get("issues") or []
        for issue in issues:
            if isinstance(issue.get("basis"), dict):
                issue["basis"] = _verify_basis_obj(issue["basis"])

    # risk_scenarios 도 basis 가 있을 수 있음
    for rs in (final_json.get("risk_scenarios") or []):
        if isinstance(rs.get("basis"), dict):
            rs["basis"] = _verify_basis_obj(rs["basis"])

    # judgment.key_authorities 는 구조가 다르므로 별도 처리 없음 (해당 경로가 review_document 밖이면 스킵)

    # confidence 필드 추가
    final_json["confidence"] = {
        "grounded": total_dropped == 0,
        "dropped_count": total_dropped,
    }
    if warnings:
        # warnings 를 final_json 에 병합
        existing_warnings = final_json.get("warnings") or {}
        if not isinstance(existing_warnings, dict):
            existing_warnings = {}
        existing_warnings["dropped_basis"] = warnings
        final_json["warnings"] = existing_warnings

    if conn is not None:
        conn.close()

    return final_json, warnings
