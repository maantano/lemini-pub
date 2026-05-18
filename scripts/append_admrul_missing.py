"""누락된 admrul 만 추려서 append.

staging DB 에 이미 들어간 law_id 를 제외하고, 마크다운 중 누락된 것만
append_admrul_bulk.py 로직으로 처리. 타임아웃/재시도 포함.

사용:
  ARTIFACT_DIR=$(pwd)/data/staging-artifacts \\
  PYTHONPATH=packages/python/src \\
  python scripts/append_admrul_missing.py data/sync/kr/행정규칙
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/python/src"))

from law_rag_core.settings import get_settings

from append_admrul_bulk import append_one  # type: ignore
from law_rag_core.ai import GeminiService


# 파일에서 법령ID 추출 (front-matter)
_LAW_ID_RE = re.compile(r"^법령ID:\s*'?([^'\n]+)'?\s*$", re.MULTILINE)


def extract_law_id(md_path: Path) -> str | None:
    try:
        text = md_path.read_text(encoding="utf-8")[:2000]
        m = _LAW_ID_RE.search(text)
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return None


def main(input_dir: str) -> int:
    root = Path(input_dir)
    settings = get_settings()

    conn = sqlite3.connect(settings.law_db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    already = {r[0] for r in conn.execute(
        "SELECT law_id FROM law_documents WHERE document_type='administrative_rule'"
    ).fetchall()}
    print(f"이미 적재된: {len(already)}개")

    # 누락분 선별
    all_md = sorted(root.rglob("*.md"))
    missing = []
    for p in all_md:
        lid = extract_law_id(p)
        if lid and lid not in already:
            missing.append((p, lid))
    print(f"총 {len(all_md)}개 중 누락 {len(missing)}개")

    if not missing:
        print("✓ 누락 없음 — 끝")
        conn.close()
        return 0

    gemini = GeminiService()
    success = fail = 0
    import numpy as np
    import json as _json

    all_new_embeddings: list[tuple[str, np.ndarray]] = []

    try:
        for i, (md, lid) in enumerate(missing, 1):
            started = time.time()
            try:
                r = append_one(md, conn, gemini)
                all_new_embeddings.extend(r["new_embeddings"])
                success += 1
                elapsed = time.time() - started
                print(f"  [{i}/{len(missing)}] ✓ {lid} {md.name[:40]} ({elapsed:.1f}s, {r['chunks']} chunks)")
                conn.commit()
            except Exception as e:
                fail += 1
                print(f"  [{i}/{len(missing)}] ✗ {lid} {md.name[:40]}: {type(e).__name__}: {e}")
    finally:
        conn.commit()
        conn.close()

    # 임베딩 append
    vec_path = settings.vector_matrix_path
    ids_path = settings.vector_ids_path
    existing_mat = np.load(vec_path) if vec_path.exists() and vec_path.stat().st_size > 128 else np.empty((0, settings.embedding_dim), dtype=np.float32)
    existing_ids = _json.loads(ids_path.read_text(encoding="utf-8")) if ids_path.exists() else []
    ids_set = set(existing_ids)
    filtered = [(cid, arr) for cid, arr in all_new_embeddings if cid not in ids_set]
    if filtered:
        new_mat = np.vstack([existing_mat] + [arr[np.newaxis, :] for _, arr in filtered])
        new_ids = existing_ids + [cid for cid, _ in filtered]
        np.save(vec_path, new_mat)
        ids_path.write_text(_json.dumps(new_ids, ensure_ascii=False), encoding="utf-8")
        print(f"embeddings append: +{len(filtered)} (total {len(new_ids)})")

    print()
    print(f"✓ 누락분 처리 완료: success={success} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "data/sync/kr/행정규칙"))
