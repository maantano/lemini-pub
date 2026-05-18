"""Slack 알림 — sync 파이프라인 실패/완료 통지.

환경변수:
  SLACK_BOT_TOKEN      xoxb-... (Secret Manager lemini-slack-bot-token)
  SLACK_ALERT_CHANNEL  #lemini-sync-alerts 또는 채널 ID

둘 중 하나라도 비면 알림은 skip (로컬 개발·테스트 환경 안전). 실패 재전파를
막기 위해 알림 자체의 예외는 swallow 하고 stderr 로만 남긴다.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from typing import Any

import requests

logger = logging.getLogger(__name__)

_SLACK_API = "https://slack.com/api/chat.postMessage"


def _enabled() -> tuple[str, str] | None:
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_ALERT_CHANNEL", "").strip()
    if not token or not channel:
        return None
    return token, channel


def send(text: str, *, blocks: list[dict] | None = None) -> bool:
    """Slack 메시지 전송. 성공 시 True, 미구성/실패 시 False."""
    creds = _enabled()
    if not creds:
        logger.debug("Slack alerts disabled (token/channel not set)")
        return False

    token, channel = creds
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks

    try:
        resp = requests.post(
            _SLACK_API,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"[alerts] Slack API error: {data}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"[alerts] Slack send failed: {e}", file=sys.stderr)
        return False


def success(job_name: str, summary: dict[str, Any]) -> None:
    """완료 알림. summary 예시: {'laws': 12, 'precedents': 847, 'duration_s': 1440}."""
    lines = [f"*✅ {job_name} 완료*"]
    for k, v in summary.items():
        lines.append(f"• {k}: {v}")
    send("\n".join(lines))


def failure(
    job_name: str,
    step: str,
    exc: BaseException,
    *,
    partial: dict[str, Any] | None = None,
    docs_url: str | None = None,
) -> None:
    """실패 알림. Cloud Logging 링크와 재시도 방법 포함."""
    lines = [f"*🚨 {job_name} 실패* (step: `{step}`)"]
    lines.append(f"• 에러: `{type(exc).__name__}: {str(exc)[:200]}`")
    if partial:
        lines.append("• 부분 수집: " + ", ".join(f"{k}={v}" for k, v in partial.items()))
    lines.append("• 자동 재시도: 다음 실행 주기 (주 1회 일요일)")
    lines.append(f"• 수동 재실행: `gcloud run jobs execute {job_name} --region asia-northeast3`")
    if docs_url:
        lines.append(f"• 로그: {docs_url}")
    send("\n".join(lines))

    # 디버깅 용이성 위해 stderr 에도 풀 스택트레이스 출력 (Cloud Logging 이 capture)
    traceback.print_exception(type(exc), exc, exc.__traceback__)
