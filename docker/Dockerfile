FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY patchfrog ./patchfrog
COPY apps ./apps
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir . && \
    groupadd --system patchfrog && \
    useradd --system --gid patchfrog --home-dir /app --no-create-home patchfrog && \
    chown -R patchfrog:patchfrog /app

USER patchfrog

FROM base AS api
EXPOSE 8000
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS worker
CMD ["celery", "-A", "apps.worker.celery_app", "worker", "--loglevel=INFO"]
