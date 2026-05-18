"""평가 파이프라인 단일 진입점.

한 명령으로 4단계 실행:
  1. 질문 수집 (collect_questions)
  2. 시스템 응답 생성 (run_system) — 답변 데이터 일절 안 봄
  3. 변호사 답변 수집 (collect_answers) — 시스템 응답 끝난 뒤
  4. 채점 (score_judge)

데이터 격리는 유지: 각 단계는 독립 subprocess로 실행돼 메모리 공유 없음.
2단계가 끝나기 전까지는 답변 파일이 생성되지 않음.

사용:
  SLACK_TOKEN=xoxp-... python scripts/eval/eval.py
  SLACK_TOKEN=xoxp-... EVAL_THREAD_LIMIT=5 python scripts/eval/eval.py
  python scripts/eval/eval.py --skip-collect       # 기존 질문/답변 재사용
  python scripts/eval/eval.py --only score         # 채점만 다시
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "scripts" / "eval"
PY = sys.executable

QUESTIONS = ROOT / ".local-data" / "eval-questions.json"
ANSWERS = ROOT / ".local-data" / "eval-answers.json"


def run_step(name: str, script: str, env_extra: dict | None = None) -> None:
    print(f"\n{'='*60}\n▶ {name}\n{'='*60}")
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    t0 = time.time()
    r = subprocess.run([PY, str(EVAL_DIR / script)], env=env)
    dt = time.time() - t0
    if r.returncode != 0:
        print(f"\n✗ {name} failed (exit {r.returncode})", file=sys.stderr)
        sys.exit(r.returncode)
    print(f"✓ {name} done ({dt:.1f}s)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-collect", action="store_true",
                    help="질문/답변 수집 건너뛰고 기존 .local-data 재사용")
    ap.add_argument("--only", choices=["questions", "run", "answers", "score"],
                    help="한 단계만 실행")
    args = ap.parse_args()

    needs_token = not (args.skip_collect or args.only in ("run", "score"))
    if needs_token and not os.environ.get("SLACK_TOKEN"):
        print("ERROR: SLACK_TOKEN env var required", file=sys.stderr)
        sys.exit(1)

    if args.only:
        if args.only == "questions":
            run_step("1) 질문 수집", "collect_questions.py")
        elif args.only == "run":
            run_step("2) 시스템 응답", "run_system.py")
        elif args.only == "answers":
            run_step("3) 변호사 답변 수집", "collect_answers.py")
        elif args.only == "score":
            run_step("4) 채점", "score_judge.py")
        return

    # 1) 질문 수집
    if args.skip_collect:
        if not QUESTIONS.exists():
            print(f"ERROR: --skip-collect 사용했지만 {QUESTIONS.relative_to(ROOT)} 없음", file=sys.stderr)
            sys.exit(1)
        print(f"⏭ 질문 수집 스킵 (기존 사용: {QUESTIONS.relative_to(ROOT)})")
    else:
        # 답변 파일이 남아있으면 삭제 (이번 회차 대상 일관성 유지)
        if ANSWERS.exists():
            ANSWERS.unlink()
            print(f"  (이전 {ANSWERS.relative_to(ROOT)} 삭제 — 이번 회차에서 새로 수집)")
        run_step("1) 질문 수집 (답변 데이터 안 가져옴)", "collect_questions.py")

    # 2) 시스템 응답 — 답변 파일 존재 여부 확인 (혹시라도 있으면 경고)
    if ANSWERS.exists():
        print("\n⚠ 답변 파일이 이미 존재합니다. 시스템 응답 단계는 이 파일을 읽지 않지만,", file=sys.stderr)
        print("  공정성을 위해 새 회차에서는 --skip-collect 없이 실행하길 권장합니다.\n", file=sys.stderr)
    run_env = {"EVAL_ALLOW_EXISTING_ANSWERS": "1"} if args.skip_collect else None
    run_step("2) 시스템 응답 생성 (답변 데이터 안 봄)", "run_system.py", env_extra=run_env)

    # 3) 변호사 답변 수집
    if args.skip_collect and ANSWERS.exists():
        print(f"⏭ 답변 수집 스킵 (기존 사용: {ANSWERS.relative_to(ROOT)})")
    else:
        run_step("3) 변호사 답변 수집", "collect_answers.py")

    # 4) 채점
    run_step("4) 채점", "score_judge.py")

    print("\n" + "="*60)
    print("✅ 전체 파이프라인 완료")
    print("="*60)
    print(f"  요약: eval-runs/$(cat eval-runs/latest.txt)/summary.json")
    print(f"  히스토리: eval-history.csv")


if __name__ == "__main__":
    main()
