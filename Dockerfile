FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY packages/python/src ./packages/python/src
RUN pip install --upgrade pip && pip install .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/packages/python/src \
    ARTIFACT_DIR=/app/data/artifacts \
    PORT=8080

WORKDIR /app

# GCS 동기화용 (state.sqlite 영속 저장)
RUN pip install --no-cache-dir google-cloud-storage

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY apps/__init__.py ./apps/__init__.py
COPY apps/api ./apps/api
COPY packages/python/src ./packages/python/src
COPY data/artifacts ./data/artifacts
COPY scripts/entrypoint.sh ./scripts/entrypoint.sh

EXPOSE 8080

CMD ["bash", "scripts/entrypoint.sh"]
