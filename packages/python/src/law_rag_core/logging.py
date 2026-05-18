from __future__ import annotations

import logging
import sys

import structlog


def configure_logging() -> None:
    """구조화 로깅 설정. 프로덕션(Cloud Run)에서는 JSON, 로컬에서는 사람이 읽기 쉬운 포맷."""
    import os

    is_production = os.environ.get("APP_ENV", "local") != "local"

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            # 프로덕션: JSON 로그 (Cloud Run 로그 탐색기에서 파싱 가능)
            # 로컬: 색상 있는 콘솔 출력
            structlog.processors.JSONRenderer(ensure_ascii=False)
            if is_production
            else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # stdlib logging도 structlog로 라우팅
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    configure_logging()
    return structlog.get_logger(name)
