"""Slack 스레드에서 **변호사 답변만** 수집.

⚠️ 이 스크립트는 시스템 응답을 생성하는 단계와 분리된 별도 호출이다.
   eval-questions.json에 있는 thread_ts를 기준으로 같은 스레드의 답변을 가져와
   eval-answers.json에 저장한다. 두 파일은 score_judge.py에서만 합쳐진다.

답변/인사 분류는 LLM이 수행. 인사·ack·후속 질문은 제외하고
실질 답변(법적 결론·근거·구체 권고·문서 첨삭)만 남김.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "python" / "src"))

SLACK_TOKEN = os.environ.get("SLACK_TOKEN") or (sys.argv[1] if len(sys.argv) > 1 else None)
QUESTIONS_PATH = ROOT / ".local-data" / "eval-questions.json"
ANSWERS_PATH = ROOT / ".local-data" / "eval-answers.json"
FILES_ROOT = ROOT / ".local-data" / "slack-files"


def slack(method: str, **params) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(
        f"https://slack.com/api/{method}?{qs}",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


_user_cache: dict[str, str] = {}


def user_name(uid: str | None) -> str:
    if not uid:
        return ""
    if uid in _user_cache:
        return _user_cache[uid]
    try:
        u = slack("users.info", user=uid)
        info = u.get("user", {})
        n = info.get("real_name") or info.get("name") or uid
    except Exception:
        n = uid
    _user_cache[uid] = n
    return n


def normalize_mentions(text: str) -> str:
    def repl(m):
        return f"@{user_name(m.group(1))}"
    return re.sub(r"<@([A-Z0-9]+)>", repl, text or "")


from common import download_slack_file


def collect_files(message: dict, save_dir: Path) -> list[dict]:
    out: list[dict] = []
    for f in message.get("files") or []:
        url = f.get("url_private_download") or f.get("url_private")
        name = f.get("name") or f.get("title") or f.get("id")
        if not url or not name:
            continue
        safe = re.sub(r"[\\/:*?\"<>|]", "_", name)
        dest = save_dir / safe
        ok = download_slack_file(url, dest, SLACK_TOKEN)
        out.append({
            "name": name,
            "mimetype": f.get("mimetype"),
            "size": f.get("size"),
            "downloaded": ok,
            "local_path": str(dest.relative_to(ROOT)) if ok else None,
        })
    return out


CLASSIFY_PROMPT = """당신은 Slack 스레드에서 '실질적인 법률 검토 답변'에 해당하는 메시지만 골라내는 분류자다.

원칙:
- '실질 답변' = 법적 결론·근거·구체 권고·문서 첨삭이 담긴 메시지.
- '실질 답변 아님' = 단순 인사("안녕하세요"), 수신 확인("확인했습니다"), 일정 안내("내일까지 보내드릴게요"), 후속 질문, 잡담.
- 첨부 파일을 '검토 결과'로 보내는 메시지는 본문이 짧아도 실질 답변으로 본다.
- 사용자(질문자) 본인의 보충 메시지는 답변이 아니라 question_followup으로 분류.
- 변호사가 보낸 메시지여도 인사·ack면 답변 아님.

질문자(스레드 루트 작성자): {root_author}

메시지:
{messages_block}

JSON으로만 출력:
{{
  "items": [
    {{"index": 0, "role": "answer | ack | question_followup | other", "is_substantive_answer": true | false}}
  ]
}}
"""


def classify_replies(root_author: str, replies: list[dict]) -> list[dict]:
    if not replies:
        return []
    msgs_block = "\n".join(
        f"[{i}] author={r.get('author','')}\n{(r.get('text') or '')[:600]}\n---"
        for i, r in enumerate(replies)
    )
    from law_rag_core.ai import GeminiService
    from google.genai import types as gtypes
    g = GeminiService()
    prompt = CLASSIFY_PROMPT.format(root_author=root_author, messages_block=msgs_block)
    try:
        resp = g.client.models.generate_content(
            model=g.settings.gemini_model,
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        data = json.loads(resp.text or "{}")
        items = data.get("items") or []
        by_idx = {int(it.get("index", -1)): it for it in items if isinstance(it, dict)}
        out = []
        for i, r in enumerate(replies):
            it = by_idx.get(i, {})
            r2 = dict(r)
            r2["role"] = it.get("role", "other")
            r2["is_substantive_answer"] = bool(it.get("is_substantive_answer", False))
            out.append(r2)
        return out
    except Exception as exc:
        print(f"  ! classify failed: {exc} — fallback length heuristic", file=sys.stderr)
        out = []
        for r in replies:
            txt = r.get("text") or ""
            r2 = dict(r)
            is_ans = len(txt) >= 120 or bool(r.get("files_meta"))
            r2["role"] = "answer" if is_ans else "ack"
            r2["is_substantive_answer"] = is_ans
            out.append(r2)
        return out


def main() -> None:
    if not SLACK_TOKEN:
        print("ERROR: SLACK_TOKEN env var or first arg required", file=sys.stderr)
        sys.exit(1)
    if not QUESTIONS_PATH.exists():
        print(f"questions file missing: {QUESTIONS_PATH} (run collect_questions.py first)", file=sys.stderr)
        sys.exit(1)

    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    print(f"# Collecting answers for {len(questions)} threads")

    answers: list[dict] = []
    for idx, q in enumerate(questions):
        ts = q["thread_ts"]
        ch = q["channel_id"]
        root_author = q["question"]["author"]
        thread_dir = FILES_ROOT / ts.replace(".", "_")
        print(f"[{idx+1}/{len(questions)}] ts={ts}")

        try:
            reps = slack("conversations.replies", channel=ch, ts=ts, limit=200)
            raw = (reps.get("messages") or [])[1:]  # 첫 메시지는 루트
        except Exception as exc:
            print(f"  ! replies failed: {exc}")
            raw = []

        norm = []
        for rm in raw:
            norm.append({
                "ts": rm.get("ts"),
                "author": user_name(rm.get("user")),
                "user_id": rm.get("user"),
                "text": normalize_mentions(rm.get("text") or ""),
                "_raw": rm,
            })

        classified = classify_replies(root_author, norm)

        substantive = []
        for i, r in enumerate(classified):
            if not r.get("is_substantive_answer"):
                continue
            raw_msg = r["_raw"]
            files_dir = thread_dir / f"answer_{i}"
            ans_files = collect_files(raw_msg, files_dir)
            substantive.append({
                "ts": r.get("ts"),
                "author": r.get("author"),
                "text": r.get("text"),
                "files": ans_files,
            })

        answers.append({
            "thread_ts": ts,
            "lawyer_answers": substantive,
            "all_replies_classification": [
                {"ts": r.get("ts"), "author": r.get("author"), "role": r.get("role"),
                 "is_substantive_answer": r.get("is_substantive_answer")}
                for r in classified
            ],
        })
        time.sleep(0.4)

    ANSWERS_PATH.write_text(json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ saved answers: {ANSWERS_PATH.relative_to(ROOT)} ({len(answers)} cases)")
    print("  → 다음: python scripts/eval/score_judge.py")


if __name__ == "__main__":
    main()
