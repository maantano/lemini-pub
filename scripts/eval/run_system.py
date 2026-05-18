"""골드셋의 각 케이스를 우리 프로젝트 파이프라인에 돌려 시스템 답변 생성.

흐름:
  1. eval-goldset.json 로드
  2. 각 케이스에 대해:
     - 첨부 파일이 있으면 review_documents_orchestrated 호출
     - 없으면 review_document(text=question.text)로 텍스트 본문 검토
  3. eval-runs/<timestamp>/cases/<thread_ts>.json 에 시스템 응답 저장
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "python" / "src"))

from common import extract_text

QUESTIONS = ROOT / ".local-data" / "eval-questions.json"
ANSWERS = ROOT / ".local-data" / "eval-answers.json"
EVAL_RUNS = ROOT / "eval-runs"


def main() -> None:
    if ANSWERS.exists() and not os.environ.get("EVAL_ALLOW_EXISTING_ANSWERS"):
        print(
            "❌ eval-answers.json이 이미 존재합니다.\n"
            "   변호사 답변이 존재하는 상태에서 시스템 응답을 생성하면 공정성이 손상될 수 있습니다.\n"
            "   새 회차: eval.py를 --skip-collect 없이 실행하세요.\n"
            "   기존 재사용: eval.py --skip-collect 를 사용하세요.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not QUESTIONS.exists():
        print(f"questions not found: {QUESTIONS} (run collect_questions.py first)", file=sys.stderr)
        sys.exit(1)

    cases = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    run_id = time.strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = EVAL_RUNS / run_id
    cases_dir = run_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    from law_rag_core.ai import GeminiService
    from law_rag_core.retrieval import RetrievalService
    gemini = GeminiService()
    retrieval = RetrievalService()

    print(f"# Running system on {len(cases)} cases → {run_dir.relative_to(ROOT)}")

    for i, c in enumerate(cases):
        ts = c["thread_ts"]
        out_path = cases_dir / f"{ts.replace('.', '_')}.json"
        if out_path.exists():
            print(f"[{i+1}/{len(cases)}] {ts} — skip (already)")
            continue

        q_text = c["question"]["text"]
        q_files = [
            ROOT / f["local_path"]
            for f in (c["question"].get("files") or [])
            if f.get("downloaded") and f.get("local_path")
        ]

        # 첨부 텍스트 합치기 (v3: 5만자 절단 제거, 전체 포함)
        attachment_text = ""
        for fp in q_files:
            t = extract_text(fp)
            if t:
                attachment_text += f"\n\n=== 첨부: {fp.name} ===\n{t}\n"

        combined_text = q_text
        if attachment_text:
            combined_text = f"{q_text}\n\n{attachment_text}"

        print(f"[{i+1}/{len(cases)}] {ts} — running... (text {len(combined_text)} chars, files {len(q_files)})", flush=True)
        t0 = time.time()
        # 케이스당 최대 180초 타임아웃 (SIGALRM 기반)
        import signal

        class _Timeout(Exception):
            pass

        def _handler(signum, frame):
            raise _Timeout(f"case {ts} exceeded 180s")

        signal.signal(signal.SIGALRM, _handler)
        signal.alarm(180)
        try:
            # v3: retrieve 쿼리 200자 제한 제거 — 긴 법률 질문의 의도 왜곡 방지
            chunks = retrieval.retrieve(q_text)
            result = gemini.review_document(
                document_text=combined_text,
                question=q_text or "법적 검토를 부탁드립니다.",
                chunks=chunks,
            )
            signal.alarm(0)
            elapsed = time.time() - t0
            out_path.write_text(json.dumps({
                "thread_ts": ts,
                "elapsed_sec": round(elapsed, 2),
                "input_chars": len(combined_text),
                "attachments": [str(p.relative_to(ROOT)) for p in q_files],
                "system_result": result,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  ✓ saved ({elapsed:.1f}s)", flush=True)
        except _Timeout as exc:
            signal.alarm(0)
            print(f"  ✗ TIMEOUT 180s: {ts}", flush=True)
            out_path.write_text(json.dumps({
                "thread_ts": ts,
                "error": "timeout_180s",
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            signal.alarm(0)
            print(f"  ✗ failed: {exc}", flush=True)
            out_path.write_text(json.dumps({
                "thread_ts": ts,
                "error": str(exc),
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    # run summary skeleton
    (run_dir / "summary.json").write_text(json.dumps({
        "run_id": run_id,
        "total_cases": len(cases),
        "started_at": time.time(),
        "scored": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 가장 최근 run 포인터
    (EVAL_RUNS / "latest.txt").write_text(run_id, encoding="utf-8")
    print(f"\n✅ run done: {run_dir.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
