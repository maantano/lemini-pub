"""
expand_query 없이 RAG 성능 측정.
expand_query ON vs OFF 비교.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "python" / "src"))

from law_rag_core.retrieval.service import RetrievalService


def normalize_law_name(name: str) -> str:
    return name.replace(" ", "").replace("·", "").lower()


def evaluate(dataset: list[dict], label: str, disable_expand: bool = False) -> dict:
    rs = RetrievalService()
    results = []

    for item in dataset:
        question = item["question"]
        expected_laws = [normalize_law_name(l) for l in item["expected_laws"]]

        start = time.time()
        if disable_expand:
            with patch.object(rs.gemini, "expand_query", return_value=[]):
                chunks = rs.retrieve(question)
        else:
            chunks = rs.retrieve(question)
        elapsed = time.time() - start

        retrieved_laws = set()
        for c in chunks:
            retrieved_laws.add(normalize_law_name(c.law_title))

        hits = sum(
            1 for el in expected_laws
            if any(el == rl or rl.endswith(el) for rl in retrieved_laws)
        )
        recall = hits / len(expected_laws) if expected_laws else 0

        results.append({
            "id": item["id"],
            "question": question[:35],
            "recall": recall,
            "chunks": len(chunks),
            "ms": int(elapsed * 1000),
        })

    avg_recall = sum(r["recall"] for r in results) / len(results)
    avg_ms = sum(r["ms"] for r in results) / len(results)
    perfect = sum(1 for r in results if r["recall"] == 1.0)
    zero = sum(1 for r in results if r["recall"] == 0)

    print(f"\n  [{label}] ({len(dataset)} questions)")
    print(f"    Law Recall:   {avg_recall:.1%}")
    print(f"    Perfect:      {perfect}/{len(dataset)}")
    print(f"    Zero:         {zero}/{len(dataset)}")
    print(f"    Avg Latency:  {int(avg_ms)}ms")

    # 실패 케이스
    if zero > 0:
        print(f"    MISS:")
        for r in results:
            if r["recall"] == 0:
                item = next(i for i in dataset if i["id"] == r["id"])
                print(f"      Q{r['id']:02d}: {r['question']} → {item['expected_laws']}")

    return {
        "label": label,
        "avg_recall": avg_recall,
        "perfect": perfect,
        "zero": zero,
        "avg_ms": int(avg_ms),
        "details": results,
    }


if __name__ == "__main__":
    dataset = json.loads(
        (REPO_ROOT / "scripts" / "eval_dataset.json").read_text(encoding="utf-8")
    )

    print("=" * 60)
    print("  expand_query ON vs OFF 비교")
    print("=" * 60)

    # 1. expand_query OFF
    off = evaluate(dataset, "expand_query OFF", disable_expand=True)

    # 2. expand_query ON
    on = evaluate(dataset, "expand_query ON", disable_expand=False)

    # 비교
    print("\n" + "=" * 60)
    print("  비교 결과")
    print("=" * 60)
    print(f"\n  {'':>20} {'OFF':>10} {'ON':>10} {'차이':>10}")
    print(f"  {'Law Recall':>20} {off['avg_recall']:>9.1%} {on['avg_recall']:>9.1%} {on['avg_recall'] - off['avg_recall']:>+9.1%}")
    print(f"  {'Perfect':>20} {off['perfect']:>10} {on['perfect']:>10} {on['perfect'] - off['perfect']:>+10}")
    print(f"  {'Zero':>20} {off['zero']:>10} {on['zero']:>10} {on['zero'] - off['zero']:>+10}")
    print(f"  {'Avg Latency':>20} {off['avg_ms']:>8}ms {on['avg_ms']:>8}ms {on['avg_ms'] - off['avg_ms']:>+8}ms")

    # 판정
    recall_diff = on["avg_recall"] - off["avg_recall"]
    time_diff = on["avg_ms"] - off["avg_ms"]

    print(f"\n  판정:")
    if recall_diff <= 0.03:
        print(f"    → expand_query 기여도 미미 ({recall_diff:+.1%})")
        print(f"    → 제거 시 {time_diff}ms 절약 가능")
        print(f"    → 권장: expand_query 제거")
    elif recall_diff <= 0.10:
        print(f"    → expand_query 기여도 보통 ({recall_diff:+.1%})")
        print(f"    → 제거 시 {time_diff}ms 절약")
        print(f"    → 권장: 캐싱 적용 또는 비동기화")
    else:
        print(f"    → expand_query 기여도 높음 ({recall_diff:+.1%})")
        print(f"    → 권장: 유지하되 캐싱 적용")

    # 개별 차이 나는 케이스
    print(f"\n  [expand_query로 개선된 케이스]")
    for off_r, on_r in zip(off["details"], on["details"]):
        if on_r["recall"] > off_r["recall"]:
            item = next(i for i in dataset if i["id"] == on_r["id"])
            print(f"    Q{on_r['id']:02d}: {off_r['recall']:.0%} → {on_r['recall']:.0%} — {on_r['question']}")

    print(f"\n  [expand_query로 악화된 케이스]")
    found = False
    for off_r, on_r in zip(off["details"], on["details"]):
        if on_r["recall"] < off_r["recall"]:
            print(f"    Q{on_r['id']:02d}: {off_r['recall']:.0%} → {on_r['recall']:.0%} — {on_r['question']}")
            found = True
    if not found:
        print(f"    (없음)")
