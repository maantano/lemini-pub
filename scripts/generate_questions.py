"""
조문별 질문 생성 스크립트.
각 조문에 대해 "이 조문으로 답할 수 있는 일상적인 질문 3개"를 생성하고,
질문을 조문 텍스트 앞에 붙여서 임베딩 품질을 향상시킵니다.

사용법:
  .venv/bin/python scripts/generate_questions.py
  .venv/bin/python scripts/generate_questions.py --limit 100  # 테스트용

결과: data/artifacts/chunk_questions.json 에 저장
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "python" / "src"))

from google import genai
from google.genai import types
from law_rag_core.settings import get_settings


QUESTION_PROMPT = """당신은 대한민국 법률 조문을 일반인이 이해할 수 있는 질문으로 변환하는 전문가입니다.

다음 법률 조문을 읽고, 이 조문의 내용으로 답할 수 있는 일상적인 질문 3개를 생성하세요.
질문은 법률 용어가 아닌 일반인이 실제로 검색할 만한 표현으로 작성하세요.

법령: {law_title}
조문: {article_no} {article_title}
내용: {text}

반드시 JSON 배열로만 응답하세요. 예: ["질문1", "질문2", "질문3"]"""


QUESTION_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
}


def load_article_chunks(limit: int | None = None) -> list[dict]:
    settings = get_settings()
    conn = sqlite3.connect(settings.law_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = """
        SELECT lc.id, lc.law_id, lc.article_no, lc.article_title, lc.text,
               ld.title as law_title
        FROM law_chunks lc
        JOIN law_documents ld ON ld.law_id = lc.law_id
        WHERE lc.chunk_type = 'article'
        ORDER BY ld.title, lc.order_index
    """
    if limit:
        query += f" LIMIT {limit}"

    cursor.execute(query)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def generate_questions_batch(
    client: genai.Client,
    chunks: list[dict],
    model: str,
    batch_size: int = 5,
) -> dict[str, list[str]]:
    """Generate questions for chunks, returns {chunk_id: [q1, q2, q3]}."""
    result: dict[str, list[str]] = {}
    total = len(chunks)
    errors = 0

    for i in range(0, total, batch_size):
        batch = chunks[i:i + batch_size]

        for chunk in batch:
            chunk_id = chunk["id"]
            prompt = QUESTION_PROMPT.format(
                law_title=chunk["law_title"],
                article_no=chunk["article_no"] or "",
                article_title=chunk["article_title"] or "",
                text=chunk["text"][:500],  # Truncate long articles
            )

            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.3,
                        response_mime_type="application/json",
                        response_schema=QUESTION_SCHEMA,
                    ),
                )
                if response.text:
                    questions = json.loads(response.text)
                    if isinstance(questions, list):
                        result[chunk_id] = questions[:3]
                    else:
                        result[chunk_id] = []
                else:
                    result[chunk_id] = []
            except Exception as e:
                errors += 1
                result[chunk_id] = []
                if "429" in str(e) or "quota" in str(e).lower():
                    print(f"  Rate limit hit at {i+1}/{total}, waiting 30s...", flush=True)
                    time.sleep(30)
                elif errors > 50:
                    print(f"  Too many errors ({errors}), stopping.", flush=True)
                    return result

        done = min(i + batch_size, total)
        if done % 500 == 0 or done == total:
            print(f"  Progress: {done}/{total} ({done*100//total}%) - {len([v for v in result.values() if v])} with questions", flush=True)

    return result


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    settings = get_settings()
    output_path = settings.artifact_dir / "chunk_questions.json"

    print(f"Loading article chunks...", flush=True)
    chunks = load_article_chunks(limit=args.limit)
    print(f"  {len(chunks)} chunks to process", flush=True)

    # Load existing progress if any
    existing: dict[str, list[str]] = {}
    if output_path.exists():
        existing = json.loads(output_path.read_text(encoding="utf-8"))
        print(f"  {len(existing)} already processed, resuming...", flush=True)

    # Filter out already processed
    remaining = [c for c in chunks if c["id"] not in existing]
    print(f"  {len(remaining)} remaining", flush=True)

    if not remaining:
        print("All chunks already processed!", flush=True)
        return

    client = genai.Client(api_key=settings.gemini_api_key)
    model = settings.gemini_model

    print(f"Generating questions with {model}...", flush=True)
    start = time.time()
    new_questions = generate_questions_batch(client, remaining, model)
    elapsed = time.time() - start

    # Merge
    all_questions = {**existing, **new_questions}
    output_path.write_text(
        json.dumps(all_questions, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )

    with_q = len([v for v in all_questions.values() if v])
    print(f"\nDone in {elapsed/60:.1f} min", flush=True)
    print(f"  Total: {len(all_questions)}", flush=True)
    print(f"  With questions: {with_q}", flush=True)
    print(f"  Saved to: {output_path}", flush=True)


if __name__ == "__main__":
    main()
