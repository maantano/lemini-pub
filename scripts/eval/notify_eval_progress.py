"""eval 실행 중 5분마다 Slack DM 으로 진행상황 알림, 완료 시 최종 알림.

사용:
  python scripts/eval/notify_eval_progress.py <run_id> [--interval 300] [--user U071ZMPK22V]

동작:
  - run_id 의 cases 디렉토리에 저장된 케이스 수를 주기적으로 체크
  - 5분마다 "진행: X/77, 최근 Y초, ETA Z분" DM 전송
  - 완료(77개 도달) 감지 시 최종 알림 + 구조 지표 간단 요약
  - 1시간 동안 진전 없으면 "stalled" 알림 후 종료
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_RUNS = ROOT / "eval-runs"


def slack(method: str, token: str, **params) -> dict:
    # Slack API 는 chat.postMessage 같은 건 POST 로 보내야 함
    if method in ("chat.postMessage", "conversations.open"):
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            f"https://slack.com/api/{method}",
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    else:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(
            f"https://slack.com/api/{method}?{qs}",
            headers={"Authorization": f"Bearer {token}"},
        )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def open_dm(token: str, user_id: str) -> str | None:
    try:
        r = slack("conversations.open", token, users=user_id)
        if r.get("ok"):
            return r.get("channel", {}).get("id")
    except Exception as e:
        print(f"open_dm failed: {e}", file=sys.stderr)
    return None


def post(token: str, channel: str, text: str) -> None:
    try:
        r = slack("chat.postMessage", token, channel=channel, text=text)
        if not r.get("ok"):
            print(f"post failed: {r.get('error')}", file=sys.stderr)
    except Exception as e:
        print(f"post failed: {e}", file=sys.stderr)


def count_cases(run_dir: Path) -> tuple[int, float, list[float]]:
    """(완료 수, 최신 mtime, elapsed 리스트)"""
    cases_dir = run_dir / "cases"
    if not cases_dir.exists():
        return 0, 0.0, []
    latest_mtime = 0.0
    elapsed_list: list[float] = []
    count = 0
    for p in cases_dir.glob("*.json"):
        if p.stem.endswith(".scored"):
            continue
        count += 1
        mt = p.stat().st_mtime
        if mt > latest_mtime:
            latest_mtime = mt
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if "elapsed_sec" in d:
                elapsed_list.append(float(d["elapsed_sec"]))
        except Exception:
            pass
    return count, latest_mtime, elapsed_list


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: notify_eval_progress.py <run_id> [--interval SEC] [--user U_ID]", file=sys.stderr)
        sys.exit(1)
    run_id = sys.argv[1]
    interval = 300
    user_id = "U071ZMPK22V"
    total = 77
    for i, a in enumerate(sys.argv[2:], start=2):
        if a == "--interval" and i + 1 < len(sys.argv):
            interval = int(sys.argv[i + 1])
        elif a == "--user" and i + 1 < len(sys.argv):
            user_id = sys.argv[i + 1]
        elif a == "--total" and i + 1 < len(sys.argv):
            total = int(sys.argv[i + 1])

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        # .env 읽기
        env_path = ROOT / ".env"
        if env_path.exists():
            for ln in env_path.read_text().splitlines():
                if ln.startswith("SLACK_BOT_TOKEN="):
                    token = ln.split("=", 1)[1].strip()
                    break
    if not token:
        print("SLACK_BOT_TOKEN not found", file=sys.stderr)
        sys.exit(1)

    # chat.postMessage 는 channel 파라미터에 user ID 를 그대로 넣어도 DM 으로 전송됨 (im:write scope 불필요)
    channel = user_id

    run_dir = EVAL_RUNS / run_id
    post(token, channel,
         f":rocket: eval-v3 모니터링 시작\nrun_id: `{run_id}`\ntotal: {total}건\n주기: {interval}초")

    start = time.time()
    last_count = -1
    last_post = start
    stall_since: float | None = None

    while True:
        now = time.time()
        n, mtime, elapsed_list = count_cases(run_dir)
        avg_elapsed = sum(elapsed_list) / len(elapsed_list) if elapsed_list else 0.0

        # 진전 감지
        if n != last_count:
            last_count = n
            stall_since = None
        else:
            if stall_since is None:
                stall_since = now
            elif now - stall_since > 3600:
                post(token, channel,
                     f":warning: stalled — 1시간 동안 진전 없음 ({n}/{total}). 모니터 종료.")
                break

        # 완료 감지
        if n >= total:
            wall = now - start
            metrics = ""
            sm = run_dir / "structural_metrics.json"
            if sm.exists():
                m = json.loads(sm.read_text())
                metrics = (f"\n\n구조 지표:\n"
                           f"• 사건번호 verified: {m.get('case_no_verified')}/{m.get('case_no_unique')}\n"
                           f"• 고시번호 verified: {m.get('ordinance_no_verified')}/{m.get('ordinance_no_unique')}\n"
                           f"• 금지어: {sum((m.get('forbidden_phrases') or {}).values())}회")
            post(token, channel,
                 f":white_check_mark: *eval 완료*\n"
                 f"run_id: `{run_id}`\n"
                 f"cases: {n}/{total}\n"
                 f"총 경과(모니터): {wall/60:.1f}분\n"
                 f"케이스 평균: {avg_elapsed:.1f}초"
                 f"{metrics}")
            break

        # 주기적 보고
        if now - last_post >= interval:
            remaining = total - n
            eta_min = (remaining * avg_elapsed / 60) if avg_elapsed else 0
            post(token, channel,
                 f":hourglass_flowing_sand: 진행: {n}/{total} "
                 f"(평균 {avg_elapsed:.1f}초/건, ETA {eta_min:.0f}분)")
            last_post = now

        time.sleep(15)


if __name__ == "__main__":
    main()
