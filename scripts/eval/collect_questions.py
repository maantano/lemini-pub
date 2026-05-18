"""Slack #02-법률검토 채널에서 **질문(스레드 루트 + 첨부)만** 수집.

⚠️ 이 스크립트는 변호사 답변·스레드 reply를 절대 수집하지 않는다.
   답변 데이터를 미리 파일에 남기면 평가의 객관성이 훼손될 수 있어 분리.

흐름:
  1. conversations.history로 최근 N건
  2. 스레드 루트(reply_count>0) 상위 K개 — replies는 '몇 개 있는지'만 보고 본문은 안 가져옴
  3. 질문 본문 + 질문측 첨부 다운로드
  4. .local-data/eval-questions.json 저장

답변 수집은 collect_answers.py에서 '시스템 응답이 끝난 뒤' 별도로 실행.
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

SLACK_TOKEN = os.environ.get("SLACK_TOKEN") or (sys.argv[1] if len(sys.argv) > 1 else None)
CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C04EUJENPR7")
LIMIT_THREADS = int(os.environ.get("EVAL_THREAD_LIMIT", "10000"))  # 기본: 사실상 무제한 (채널 전체)
OFFSET_THREADS = int(os.environ.get("EVAL_THREAD_OFFSET", "0"))  # 건너뛸 스레드 수 (0=처음부터)
HISTORY_LIMIT = int(os.environ.get("EVAL_HISTORY_LIMIT", "200"))  # 페이지 당 (Slack 권장)
HISTORY_PAGES = int(os.environ.get("EVAL_HISTORY_PAGES", "50"))  # 최대 페이지 수 (200*50 = 1만개)
QUESTIONS_PATH = ROOT / ".local-data" / "eval-questions.json"
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


def main() -> None:
    if not SLACK_TOKEN:
        print("ERROR: SLACK_TOKEN env var or first arg required", file=sys.stderr)
        sys.exit(1)
    QUESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(f"# Collecting questions from {CHANNEL_ID} (offset={OFFSET_THREADS}, limit={LIMIT_THREADS})")
    # Slack conversations.history 는 기본 500 제한이라 cursor 기반 페이지네이션 필요.
    # EVAL_HISTORY_PAGES 만큼 순회해서 채널 전체 수집.
    msgs: list[dict] = []
    cursor: str | None = None
    for page_idx in range(HISTORY_PAGES):
        params = {"channel": CHANNEL_ID, "limit": HISTORY_LIMIT}
        if cursor:
            params["cursor"] = cursor
        hist = slack("conversations.history", **params)
        if not hist.get("ok"):
            raise RuntimeError(f"history failed: {hist.get('error')}")
        batch = hist.get("messages") or []
        msgs.extend(batch)
        print(f"    [page {page_idx+1}] +{len(batch)} msgs (누적 {len(msgs)})")
        if not hist.get("has_more"):
            break
        cursor = (hist.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.5)  # rate limit 배려

    roots = [
        m for m in msgs
        if not m.get("subtype")
        and (m.get("thread_ts") in (None, m.get("ts")))
        and (m.get("reply_count") or 0) > 0
    ]
    # 오래된 순서대로 정렬 → offset만큼 건너뛴 뒤 limit개 가져오기
    roots = sorted(roots, key=lambda m: float(m["ts"]))
    roots = roots[OFFSET_THREADS:OFFSET_THREADS + LIMIT_THREADS]
    print(f"  found {len(roots)} threads")

    cases: list[dict] = []
    for idx, root in enumerate(roots):
        ts = root["ts"]
        thread_dir = FILES_ROOT / ts.replace(".", "_") / "question"
        author = user_name(root.get("user"))
        print(f"[{idx+1}/{len(roots)}] ts={ts} author={author}")
        q_files = collect_files(root, thread_dir)
        cases.append({
            "thread_ts": ts,
            "channel_id": CHANNEL_ID,
            "posted_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))),
            "question": {
                "author": author,
                "text": normalize_mentions(root.get("text") or ""),
                "files": q_files,
            },
            "reply_count_at_collect": root.get("reply_count", 0),
        })
        time.sleep(0.3)

    QUESTIONS_PATH.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ saved questions: {QUESTIONS_PATH.relative_to(ROOT)} ({len(cases)} cases)")
    print("  → 다음: python scripts/eval/run_system.py")


if __name__ == "__main__":
    main()
